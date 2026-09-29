"""Client rule packages. One package per client; packages never share state.

Rule resolution order (blueprint section 7): global rules (eod.py, ledger.py)
→ client package (here) → SKU mapping. A package marked PROPOSED can produce
numbers, but the ledger never marks them VERIFIED until the package is
APPROVED by ShipMode.
"""
from __future__ import annotations

import re

import muravai_rules
from eod import OrderResult, parse_items

CODE_IN_PARENS = re.compile(r"\(([^()]+)\)\s*$")


class AliasRules:
    """Single-product clients: each ShipSidekick product code maps to one internal SKU,
    and the CSV quantity is counted directly (1x = 1 unit)."""

    def __init__(self, client_id, organization, aliases, labels, status, source, pending=None):
        self.client_id = client_id
        self.organization = organization
        self.aliases = {code.upper(): sku for code, sku in aliases.items()}
        # Codes seen in exports whose unit count is an open business rule. They are named, never counted.
        self.pending = {code.upper(): reason for code, reason in (pending or {}).items()}
        self.skus = tuple(dict.fromkeys(aliases.values()))
        self.labels = labels
        self.status = status
        self.source = source

    def order_usage(self, items: str) -> OrderResult:
        usage = {sku: 0 for sku in self.skus}
        unknown = []
        for qty, desc in parse_items(items):
            match = CODE_IN_PARENS.search(desc)
            code = match.group(1).strip().upper() if match else ""
            if code in self.pending:
                unknown.append(f"{desc}: {self.pending[code]}")
                continue
            sku = self.aliases.get(code)
            if not sku or qty == 0:
                unknown.append(desc)
                continue
            usage[sku] += qty
        return OrderResult(usage=usage, raw=dict(usage), unknown_items=unknown)


PROPOSED = "PROPOSED — mapping taken from ShipSidekick exports and the client Sheet; needs ShipMode approval"

PACKAGES = {
    "muravai": muravai_rules.RULES,
    "fascial-labs": AliasRules(
        "fascial-labs", "Fascial Labs", {"FASCSUPP-1": "FAS001"},
        {"FAS001": "TrueForm Fascial Release Support"}, "PROPOSED", PROPOSED,
        pending={
            # TODO(issue #9, open rule 1): units per line not approved; held, never guessed.
            "FASCSUPPx2": "units per line not approved yet (Issue #9, open rule 1)",
            "FAC3XBDL": "bundle contents not approved yet (Issue #9, open rule 1)",
        }),
    "puravita": AliasRules(
        "puravita", "PuraVita", {"CAP-MAGNESIUM-360": "PVT001"},
        {"PVT001": "Magnesium Performance Capsules"}, "PROPOSED", PROPOSED),
    # Each item line is counted from its own quantity, so a two-product order counts 1 per item,
    # never the order's total item count (Sep 21–25 overstatement).
    "nuerosmile": AliasRules(
        "nuerosmile", "NeuroSmile", {"NEURO-120": "NEU001", "MAG-SPRAY-360": "NEU002"},
        {"NEU001": "Nerve Support", "NEU002": "Magnesium Spray"}, "PROPOSED", PROPOSED,
        pending={
            # TODO(issue #9, open rule 2): is the Pill Carrier tracked inventory?
            "PILL-CARRIER-360": "not decided whether this item is tracked (Issue #9, open rule 2)",
        }),
}

# Dashboard product name (as the Sheet shows it) → internal SKU, per client. Used only to line up the
# Sheet's "shipped on date" with the CSV recount; a Dashboard row missing here holds the report.
DASHBOARD_PRODUCTS = {
    "muravai": {"Replacement Filters, 3-Pack": "MUR001", "Filtered Showerhead": "MUR002",
                "Connector Kit Box": "MUR003", "Shower Hose": "MUR004", "Bracket / Connector": "MUR005"},
    "fascial-labs": {"TrueForm Fascial Release Support": "FAS001"},
    "puravita": {"Magnesium Performance Capsules": "PVT001"},
    "nuerosmile": {"Nerve Support": "NEU001", "Magnesium Spray": "NEU002"},
}


def _product_key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").replace("*", "")).strip().lower()


def dashboard_sku(client_id: str, product: str) -> str | None:
    """Look up one client's Dashboard product. Other clients' names never match."""
    names = {_product_key(name): sku for name, sku in DASHBOARD_PRODUCTS.get(client_id, {}).items()}
    return names.get(_product_key(product))


def package(client_id: str):
    """Load exactly one client's rules. Unknown clients get no rules, never a default."""
    return PACKAGES.get(client_id)
