"""ShipSidekick stock vs. client Sheet, per internal SKU (Issue #16).

Pure functions: uses only the client's own rule package to match SKUs and
never changes the ledger, the Sheet, or the rules. Which ShipSidekick number
should equal the Sheet's "Remaining" is an open business rule, so both
"available" and "available + committed" differences are reported.
"""
from __future__ import annotations

from collections import defaultdict

import client_rules


def _int(value):
    text = str(value or "").replace(",", "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        return int(float(text))
    except ValueError:
        return None


def _match(rules, product, variant, sku):
    if rules is None:
        return None
    return rules.shopify_match({"product": product, "variant": variant, "sku": sku})


def match_level(rules, level):
    """Internal SKU for one ShipSidekick variant: its SKU, then its aliases, then its name."""
    for code in [level["sku"], *level["aliases"]]:
        if code:
            found = _match(rules, level["product"], level["title"] if level["product"] else "", code)
            if found:
                return found
    return _match(rules, level["product"] or level["title"], level["title"] if level["product"] else "", "")


def match_sheet_row(rules, product):
    if rules is None:
        return None
    found = _match(rules, product, "", "")
    if found and found.get("sku"):
        return found["sku"]
    wanted = product.strip().lower()
    return next((sku for sku, label in rules.labels.items() if label.strip().lower() == wanted), None)


def compare(client_id, levels, sheet):
    """`sheet` is a Dashboard read result from inventory.read_dashboards, or None/an error dict."""
    rules = client_rules.package(client_id)
    by_sku, unmatched, components = defaultdict(list), [], []
    for level in levels:
        found = match_level(rules, level)
        if found and found.get("sku"):
            by_sku[found["sku"]].append(level)
        elif found:
            components.append({**level, "note": found["how"]})
        else:
            unmatched.append(level)

    sheet_ok = isinstance(sheet, dict) and "rows" in sheet
    sheet_rows, sheet_unmatched = defaultdict(list), []
    for row in (sheet or {}).get("rows", []) if sheet_ok else []:
        sku = match_sheet_row(rules, row["product"])
        (sheet_rows[sku].append(row) if sku else sheet_unmatched.append(row["product"]))

    out = []
    for sku in (rules.skus if rules else ()):
        ssk, rows = by_sku.get(sku, []), sheet_rows.get(sku, [])
        notes = []
        sheet_value = None
        if not sheet_ok:
            notes.append("Sheet not loaded")
        elif len(rows) == 1:
            sheet_value = _int(rows[0]["remaining"])
            if sheet_value is None:
                notes.append(f"Sheet value is not a number: {rows[0]['remaining']}")
        elif len(rows) > 1:
            notes.append("Several Sheet rows match this SKU; not compared")
        else:
            notes.append("No Sheet row matches this SKU")
        quantities = None
        if len(ssk) == 1:
            quantities = {k: ssk[0][k] for k in ("available", "committed", "incoming", "damaged",
                                                  "reserved", "quality_control")}
        elif len(ssk) > 1:
            notes.append("Several ShipSidekick SKUs match (" + ", ".join(v["sku"] or v["title"] for v in ssk)
                         + "); not added together")
        else:
            notes.append("Not found in ShipSidekick")
        diffs = {"vs_available": None, "vs_available_committed": None}
        if quantities and sheet_value is not None:
            diffs = {"vs_available": quantities["available"] - sheet_value,
                     "vs_available_committed": quantities["available"] + quantities["committed"] - sheet_value}
        status = ("REVIEW" if notes else
                  "MATCH" if 0 in diffs.values() else "DIFFERENT")
        out.append({"sku": sku, "label": rules.labels.get(sku, ""), "ssk_skus": [v["sku"] for v in ssk],
                    "sheet_product": rows[0]["product"] if len(rows) == 1 else "",
                    "sheet_remaining": sheet_value, "ssk": quantities, **diffs,
                    "status": status, "notes": notes})

    duplicates = sorted({v["sku"] for v in levels if v["sku"]
                         and sum(1 for w in levels if w["sku"].upper() == v["sku"].upper()) > 1})
    return {
        "client_id": client_id,
        "rule_status": rules.status if rules else "NONE",
        "skus": out,
        "unmatched_ssk": [{"sku": v["sku"], "title": v["title"], "product": v["product"],
                           "available": v["available"], "committed": v["committed"]} for v in unmatched],
        "components": [{"sku": v["sku"], "title": v["title"], "available": v["available"], "note": v["note"]}
                       for v in components],
        "unmatched_sheet": sheet_unmatched,
        "blank_skus": sum(1 for v in levels if not v["sku"]),
        "duplicate_skus": duplicates,
        "sheet_as_of": sheet.get("as_of", "") if sheet_ok else "",
        "sheet_error": "" if sheet_ok else (sheet or {}).get("error", "Google Sheets is not connected."),
    }
