"""Shopify order next to a No Movement shipment (#13). Pure functions, display-only flags.

Flags never change a shipment's age or exception tier. Items are compared by internal SKU using
the client's own rules; an item either side can't map makes the comparison inconclusive, never
a guessed match.
"""
from __future__ import annotations

from collections import Counter

import client_rules

FLAG_TEXT = {
    "unlinked": "ShipSidekick has no order number for this shipment, so Shopify was not searched",
    "not_found": "Order not found in Shopify",
    "not_found_recent": "Not found in Shopify's last 60 days of orders (older orders need read_all_orders access)",
    "recent_match_only": "Matched in Shopify's last 60 days only; an older order could have the same name",
    "search_incomplete": "Shopify returned too many similar order names to confirm a unique match",
    "several_orders": "More than one Shopify order has this name",
    "cancelled": "Cancelled in Shopify, but a label exists",
    "refunded": "Refunded in Shopify",
    "partially_refunded": "Partially refunded in Shopify",
    "items_differ": "Items differ between the Shopify order and this shipment",
    "items_unverified": "Items could not be compared (unmapped SKU, a very long order, or a split shipment)",
    "several_shipments": "More than one shipment for this order (possible reship or split)",
}


def _internal(rules, sku, name):
    if rules is None:
        return None
    match = rules.shopify_match({"product": name or "", "variant": "", "sku": sku or ""})
    return match.get("sku") if match else None


def _units(rules, items):
    counts, unmapped = Counter(), False
    for item in items:
        qty = item.get("qty")
        internal = _internal(rules, item.get("sku"), item.get("name"))
        if internal is None or not isinstance(qty, int):
            unmapped = True
            continue
        counts[internal] += qty
    return counts, unmapped


def check(client_id, shipment, orders, shipments_for_order=1, unlinked=False, complete=True, search_complete=True):
    if unlinked:
        return {"flags": ["unlinked"], "flag_text": [FLAG_TEXT["unlinked"]]}
    flags = []
    if not search_complete:
        flags.append("search_incomplete")  # an unchecked page could hold a duplicate: never pick one
    elif not orders:
        flags.append("not_found" if complete else "not_found_recent")
    elif len(orders) > 1:
        flags.append("several_orders")
    order = orders[0] if len(orders) == 1 and search_complete else None
    numbers = (order or {}).get("tracking_numbers") or []
    split = max(shipments_for_order, len(numbers), (order or {}).get("fulfillment_count") or 0) > 1
    # An unread or cut-off fulfillment list can hide a sibling parcel: never prove "not split" from it.
    split_unknown = bool(order) and (order.get("fulfillment_count") is None or order.get("fulfillments_truncated"))
    if order:
        if not complete:
            flags.append("recent_match_only")
        if order.get("cancelled_at"):
            flags.append("cancelled")
        financial = str(order.get("financial") or "").upper()
        if financial == "REFUNDED":
            flags.append("refunded")
        elif financial == "PARTIALLY_REFUNDED":
            flags.append("partially_refunded")
        rules = client_rules.package(client_id)
        shopify, unmapped_a = _units(rules, [i for i in order.get("items", []) if (i.get("qty") or 0) > 0])
        shipped, unmapped_b = _units(rules, shipment.get("items") or [])
        # A split shipment carries only part of the order, so a whole-order comparison would be wrong.
        # Partly fulfilled: the rest of the order hasn't shipped yet, so this parcel is only part of it.
        partial = str(order.get("fulfillment") or "").upper() == "PARTIALLY_FULFILLED"
        if unmapped_a or unmapped_b or not shipped or order.get("items_truncated") or split or split_unknown or partial \
                or shipment.get("items_truncated"):
            flags.append("items_unverified")
        elif shopify != shipped:
            flags.append("items_differ")
    # Shopify's own fulfillments also count: a sibling package may already be delivered or older
    # than the No Movement lookback, so it is not in the queue.
    if split:
        flags.append("several_shipments")
    return {"flags": flags, "flag_text": [FLAG_TEXT[f] for f in flags]}
