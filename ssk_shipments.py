"""ShipSidekick shipments -> No Movement queue rows (Issue #17).

Pure mapping. Only the fields the queue needs are copied; return and ship-to
addresses, prices and label files are never read into a row, so they cannot
reach the browser, the CSV export or the logs. Line items keep only SKU,
product name and quantity (for the Shopify order cross-check, #13). Field names come
from staging's read-only field probe of real Muravai shipments (6 Oct 2026).
"""
from __future__ import annotations

from datetime import timedelta

from tracking import STATUS, is_physical, parse_date, utcnow


EVENTS_KEPT = 15  # the detail view's scan history; older scans never change the movement clock


def status(value):
    """ShipSidekick writes "pre-transit"; the queue uses "pre_transit". Anything unrecognised is unknown."""
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return text if text in STATUS else "unknown"


def _date(value, now):
    try:
        stamp = parse_date(value)
    except (TypeError, ValueError):
        return None
    return stamp if stamp and stamp <= now + timedelta(minutes=5) else None


def _dict(value):
    return value if isinstance(value, dict) else {}


def to_row(shipment, client_id, now=None):
    now = now or utcnow()
    if not isinstance(shipment, dict):
        return None
    tracker = _dict(shipment.get("tracker"))
    events = []
    for detail in tracker.get("trackingDetails") or []:
        if not isinstance(detail, dict):
            continue
        at = _date(detail.get("createdAt"), now)
        if not at:
            continue
        state = status(detail.get("status"))
        description = str(detail.get("message") or detail.get("statusDetail") or state)[:200]
        events.append({"at": at.isoformat(), "status": state, "description": description,
                       "movement": is_physical(state, description)})
    events.sort(key=lambda e: e["at"])
    movement = [e["at"] for e in events if e["movement"]]
    labels = [_date(_dict(p.get("shippingLabel")).get("createdAt"), now)
              for p in shipment.get("packages") or [] if isinstance(p, dict)]
    labels = [d for d in labels if d] or [d for d in [_date(shipment.get("createdAt"), now)] if d]
    received = _date(shipment.get("updatedAt"), now)
    items = {}
    for package in shipment.get("packages") or []:
        for line in _dict(package).get("lineItems") or []:
            variant = _dict(_dict(line).get("productVariant"))
            qty = line.get("quantity") if isinstance(line, dict) else None
            if not isinstance(qty, int) or isinstance(qty, bool):
                continue
            sku = str(variant.get("sku") or "").strip()[:60]
            name = str(_dict(variant.get("product")).get("name") or variant.get("title") or "").strip()[:120]
            item = items.setdefault((sku, name), {"sku": sku, "name": name, "qty": 0})
            item["qty"] += qty
    carrier_status = "cancelled" if shipment.get("voidStatus") else status(tracker.get("status"))
    url = str(tracker.get("trackingUrl") or "")
    return {
        "client_id": client_id,
        "ssk_id": str(shipment.get("id") or "")[:64],
        "order_number": str(_dict(shipment.get("order")).get("name") or "")[:100] or None,
        "tracking_number": str(shipment.get("trackingCode") or tracker.get("trackingCode") or "")[:120],
        "carrier": str(tracker.get("carrierCode") or _dict(shipment.get("carrierAccount")).get("carrierCode")
                       or "unknown").lower()[:40],
        "fulfillment_status": "In carrier network" if movement else "Label created",
        "carrier_status": carrier_status,
        "shipped_at": None,
        "label_created_at": min(labels).isoformat() if labels else None,
        "last_movement_at": max(movement) if movement else None,
        "last_received_at": received.isoformat() if received else None,
        "tracking_url": url[:500] if url.startswith("https://") else None,
        "source": "ShipSidekick API",
        "case_status": "open",
        "notes": "",
        "events": events[-EVENTS_KEPT:],
        "items": list(items.values()),  # all of them: a cut list would make a false "items differ"
    }
