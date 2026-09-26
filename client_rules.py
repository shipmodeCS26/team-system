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

    def __init__(self, client_id, organization, aliases, labels, status, source):
        self.client_id = client_id
        self.organization = organization
        self.aliases = {code.upper(): sku for code, sku in aliases.items()}
        self.skus = tuple(dict.fromkeys(aliases.values()))
        self.labels = labels
        self.status = status
        self.source = source

    def order_usage(self, items: str) -> OrderResult:
        usage = {sku: 0 for sku in self.skus}
        unknown = []
        for qty, desc in parse_items(items):
            match = CODE_IN_PARENS.search(desc)
            sku = self.aliases.get(match.group(1).strip().upper()) if match else None
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
        {"FAS001": "TrueForm Fascial Release Support"}, "PROPOSED", PROPOSED),
    "puravita": AliasRules(
        "puravita", "PuraVita", {"CAP-MAGNESIUM-360": "PVT001"},
        {"PVT001": "Magnesium Performance Capsules"}, "PROPOSED", PROPOSED),
    # NEU002 (Magnesium Spray) has no confirmed ShipSidekick code yet; its sales show as unknown items.
    "nuerosmile": AliasRules(
        "nuerosmile", "NeuroSmile", {"NEURO-120": "NEU001"},
        {"NEU001": "Nerve Support"}, "PROPOSED", PROPOSED),
}


def package(client_id: str):
    """Load exactly one client's rules. Unknown clients get no rules, never a default."""
    return PACKAGES.get(client_id)
