"""Muravai EOD usage rules.

Implements docs/clients/muravai/RULES.md exactly. Pure functions only: this
module never touches inventory balances. It turns ShipSidekick CSV rows into
a per-date EOD usage proposal plus audit findings. Posting usage to inventory
is a separate, approval-gated step (FACTORY_WORKFLOW.md, section 8).
"""
from __future__ import annotations

import re
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


# Product names exactly as they appear on the Muravai Dashboard tab ("*" footnote marks ignored).
SHEET_NAMES = {"replacement filters, 3-pack": "MUR001", "filtered showerhead": "MUR002",
               "connector kit box": "MUR003", "shower hose": "MUR004", "bracket / connector": "MUR005"}

PACK_SIZE = re.compile(r"\b(\d+)\s*[-x]?\s*(?:pack|pk|filters?)\b")

KIND_SKUS = {"filter": "MUR001", "showerhead": "MUR002", "kit": "MUR003", "hose": "MUR004", "connector": "MUR005"}


def shopify_match(variant: dict) -> dict | None:
    """Muravai's rules read product names, not codes, so Shopify listings are matched by name
    exactly as a ShipSidekick line would be. Teflon tape is a kit component, not a SKU."""
    text = f"{variant.get('product', '')} {variant.get('variant', '')}"
    kind = classify(text)
    # Sets/kits/bundles in ShipSidekick or Shopify (e.g. "Shower Hose & Connector Set", BOM items) contain
    # several products; matching them to one SKU by name would count a kit as a hose. Never compared.
    lowered = f" {text.lower()} "
    if kind != "kit" and any(word in lowered for word in (" set ", " kit ", " bundle ", " + ", " & ")):
        return {"sku": None, "how": "Set/kit of several products: not matched to one SKU"}
    if kind == "filter":
        # MUR001 is one retail box of three filters; other pack sizes are not the same unit.
        pack = PACK_SIZE.search(f"{text} {variant.get('sku', '')}".lower())
        if not pack or pack.group(1) != "3":
            return {"sku": None, "how": "Filter pack size is not the approved 3-pack: needs mapping review"}
    if kind == "teflon":
        return {"sku": None, "covers": "MUR003",
                "how": "Kit component: counted as MUR003 only with a hose and connector (RULES.md)"}
    return {"sku": KIND_SKUS[kind], "how": "Product name matches the Muravai rule"} if kind else None


STOCK_FIELDS = ("available", "committed", "incoming", "damaged", "reserved", "quality_control")
KIT_BASIS = "Per RULES.md: MUR003 = Teflon tape; MUR004 = hoses − tape; MUR005 = connectors − tape"


def decompose_stock(matched: dict, components: list) -> dict:
    """Split ShipSidekick's separately stocked kit parts the way RULES.md defines the SKUs.

    ShipSidekick holds hoses, connectors and Teflon tape as separate items; a kit is one of each,
    and the number of kits is the tape quantity. Only applied when exactly one tape, one hose and
    one connector item exist and no explicit kit item does; otherwise MUR003–005 go to review.
    Negative results are kept (they are discrepancies, never raised to zero).
    """
    tapes = [c for c in components if c.get("covers") == "MUR003"]
    if not tapes:
        return {}
    hoses, connectors = matched.get("MUR004", []), matched.get("MUR005", [])
    if len(tapes) != 1 or len(hoses) != 1 or len(connectors) != 1 or matched.get("MUR003"):
        review = "Kit parts (hose / connector / tape) could not be split per RULES.md; not compared"
        return {sku: {"quantities": None, "sources": [], "basis": "", "review": review}
                for sku in ("MUR003", "MUR004", "MUR005")}
    tape, hose, connector = tapes[0], hoses[0], connectors[0]
    return {
        "MUR003": {"quantities": {k: tape[k] for k in STOCK_FIELDS}, "sources": [tape["sku"]], "basis": KIT_BASIS},
        "MUR004": {"quantities": {k: hose[k] - tape[k] for k in STOCK_FIELDS},
                   "sources": [hose["sku"], tape["sku"]], "basis": KIT_BASIS},
        "MUR005": {"quantities": {k: connector[k] - tape[k] for k in STOCK_FIELDS},
                   "sources": [connector["sku"], tape["sku"]], "basis": KIT_BASIS},
    }


class _Rules:
    client_id = "muravai"
    organization = ORGANIZATION
    skus = SKUS
    labels = LABELS
    status = "APPROVED"
    source = "docs/clients/muravai/RULES.md"
    order_usage = staticmethod(lambda items: order_usage(items))
    shopify_match = staticmethod(lambda variant: shopify_match(variant))
    sheet_names = SHEET_NAMES
    decompose_stock = staticmethod(lambda matched, components: decompose_stock(matched, components))


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
