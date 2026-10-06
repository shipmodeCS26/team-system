"""Shopify ↔ ShipSidekick ↔ internal SKU mapping check (Issue #12).

Pure functions: compares one client's Shopify catalog with that client's own
rule package. It proposes nothing into the rules and changes nothing anywhere;
an unmapped Shopify SKU stays unmapped until ShipMode approves a rule change.
"""
from __future__ import annotations

from collections import Counter

import client_rules

# UNLISTED products are active in Shopify (sold by direct link), so they are not flagged.
ACTIVE = frozenset({"ACTIVE", "UNLISTED"})
STATUS_TEXT = {
    "mapped": "Mapped",
    "component": "Kit component",
    "unmapped": "Not mapped",
    "no_rules": "No rules yet",
}
FLAG_TEXT = {
    "blank_sku": "Blank SKU in Shopify",
    "duplicate_sku": "Same SKU on more than one Shopify variant",
    "inactive_product": "Product is draft or archived in Shopify",
    "untracked": "Shopify does not track inventory for this variant",
}


def check(client_id: str, variants: list[dict]) -> dict:
    rules = client_rules.package(client_id)
    sku_counts = Counter(v["sku"].upper() for v in variants if v["sku"])
    rows, covered_active, covered_any = [], set(), set()

    for variant in variants:
        active = variant["product_status"] in ACTIVE
        flags = []
        if not variant["sku"]:
            flags.append("blank_sku")
        elif sku_counts[variant["sku"].upper()] > 1:
            flags.append("duplicate_sku")
        if not active:
            flags.append("inactive_product")
        if not variant["tracked"]:
            flags.append("untracked")

        match = rules.shopify_match(variant) if rules else None
        if rules is None:
            status, internal, how = "no_rules", None, "No rule package for this client"
        elif match and match.get("sku"):
            status, internal, how = "mapped", match["sku"], match["how"]
        elif match and match.get("covers"):
            status, internal, how = "component", match["covers"], match["how"]
        elif match:
            # Sets/bundles and non-approved pack sizes: the rules looked at it and declined to map it.
            status, internal, how = "unmapped", None, match["how"]
        else:
            status, internal = "unmapped", None
            how = (f"Proposal: add ShipSidekick code {variant['sku']} to this client's rules (needs approval)"
                   if variant["sku"] else "No SKU to map; add one in Shopify first")
        if internal:
            covered_any.add(internal)
            if active:
                covered_active.add(internal)

        rows.append({**variant, "status": status, "status_text": STATUS_TEXT[status],
                     "internal_sku": internal, "label": rules.labels.get(internal, "") if rules and internal else "",
                     "how": how, "flags": flags, "flag_text": [FLAG_TEXT[f] for f in flags]})

    rule_rows = []
    for sku in (rules.skus if rules else ()):
        state = "found" if sku in covered_active else "inactive" if sku in covered_any else "missing"
        rule_rows.append({"internal_sku": sku, "label": rules.labels.get(sku, ""), "state": state})

    order = {"unmapped": 0, "no_rules": 1, "component": 2, "mapped": 3}
    rows.sort(key=lambda r: (order[r["status"]], not r["flags"], r["product"].lower(), r["variant"].lower()))
    summary = Counter(r["status"] for r in rows)
    flagged = sum(bool(r["flags"]) for r in rows)
    needs_review = (summary["unmapped"] + summary["no_rules"] + flagged
                    + sum(r["state"] != "found" for r in rule_rows))
    return {
        "client_id": client_id,
        "rule_status": rules.status if rules else "NONE",
        "rule_source": rules.source if rules else "",
        "variants": rows,
        "rules": rule_rows,
        "summary": {"variants": len(rows), "mapped": summary["mapped"], "component": summary["component"],
                    "unmapped": summary["unmapped"], "no_rules": summary["no_rules"], "flagged": flagged,
                    "rules_missing": sum(r["state"] == "missing" for r in rule_rows),
                    "rules_inactive": sum(r["state"] == "inactive" for r in rule_rows),
                    "needs_review": needs_review},
    }
