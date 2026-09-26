"""Muravai EOD usage rules.

Implements docs/clients/muravai/RULES.md exactly. Pure functions only: this
module never touches inventory balances. It turns ShipSidekick CSV rows into
a per-date EOD usage proposal plus audit findings. Posting usage to inventory
is a separate, approval-gated step (FACTORY_WORKFLOW.md, section 8).
"""
from __future__ import annotations

from collections import Counter

import eod
from eod import OrderResult, parse_items

ORGANIZATION = "Muravai"
SKUS = ("MUR001", "MUR002", "MUR003", "MUR004", "MUR005")
LABELS = {"MUR001": "Filter 3-packs", "MUR002": "Showerheads", "MUR003": "Connector kits",
          "MUR004": "Standalone hoses", "MUR005": "Standalone brackets"}

# Order matters: an explicit kit must be recognised before its components.
CLASSIFIERS = (
    ("connector kit", "kit"),
    ("replacement filters", "filter"),
    ("filtered showerhead", "showerhead"),
    ("teflon tape", "teflon"),
    ("shower hose", "hose"),
    ("shower connector", "connector"),
)


def classify(description: str) -> str | None:
    text = description.lower()
    for needle, kind in CLASSIFIERS:
        if needle in text:
            return kind
    return None


def order_usage(items: str) -> OrderResult:
    """Apply the kit rules to ONE order before any daily totals."""
    raw = Counter()
    unknown = []
    for qty, desc in parse_items(items):
        kind = classify(desc)
        if kind is None or qty == 0:
            unknown.append(desc)
            continue
        raw[kind] += qty

    flags = []
    if raw["kit"]:
        # Explicit kit SKU: count it directly, never its components again.
        kits, hoses, connectors = raw["kit"], raw["hose"], raw["connector"]
        if raw["teflon"] or raw["hose"] or raw["connector"]:
            flags.append("Explicit connector kit and separate kit components in the same order; "
                         "structure is ambiguous, confirm before finalizing.")
    else:
        kits = raw["teflon"]
        hoses = max(raw["hose"] - kits, 0)
        connectors = max(raw["connector"] - kits, 0)
        if raw["teflon"] > raw["hose"] or raw["teflon"] > raw["connector"]:
            flags.append("Teflon quantity exceeds hose or connector quantity: possible "
                         "incomplete kit or CSV problem.")
    return OrderResult(
        usage={"MUR001": raw["filter"], "MUR002": raw["showerhead"], "MUR003": kits,
               "MUR004": hoses, "MUR005": connectors},
        raw=dict(raw), flags=flags, unknown_items=unknown)


class _Rules:
    client_id = "muravai"
    organization = ORGANIZATION
    skus = SKUS
    labels = LABELS
    status = "APPROVED"
    source = "docs/clients/muravai/RULES.md"
    order_usage = staticmethod(lambda items: order_usage(items))


RULES = _Rules()


def build_eod(rows):
    return eod.build_eod(rows, RULES)


def eod_message(report):
    return eod.eod_message(report, prefix="MUR", labels=LABELS)


audit_statement = eod.audit_statement


def expected_balance(baseline: dict[str, int], receipts: dict[str, int],
                     usage_after_baseline: dict[str, int],
                     adjustments: dict[str, int] | None = None) -> dict[str, int]:
    """Baseline + confirmed receipts − usage after the baseline ± adjustments.

    Negative results are kept: they are discrepancies to verify, never
    silently raised to zero.
    """
    adjustments = adjustments or {}
    skus = set(baseline) | set(receipts) | set(usage_after_baseline) | set(adjustments)
    return {sku: baseline.get(sku, 0) + receipts.get(sku, 0)
            - usage_after_baseline.get(sku, 0) + adjustments.get(sku, 0)
            for sku in sorted(skus)}
