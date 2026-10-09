"""Cross-check a client's Sheet Dashboard against a recount of its ShipSidekick shipments.

The recount reads the client's own Daily Sales tab (the ShipSidekick export already pasted into the
Sheet), or a CSV uploaded for the report.

The Sheet is the official number (Gly, 2026-09-29). This module only checks it: it never
changes a Sheet value and never replaces one with the recount. Result:

- VERIFIED: every Dashboard product's "shipped on date" equals the CSV recount for that date.
- REVIEW: any gap, unknown item, duplicate, other client's row, or Dashboard warning.
- INCOMPLETE: no CSV for the date (unless the day is confirmed as no shipments), or no rules.
"""
from __future__ import annotations

from datetime import date

import client_rules
import ssk_check
from daily_orders import sheet_day
from eod import build_eod
from ledger_sources import parse_int

VERIFIED, REVIEW, INCOMPLETE = "VERIFIED", "REVIEW", "INCOMPLETE"


def parse_as_of(value: str) -> date | None:
    return sheet_day(value)


def check(client_id: str, source: dict, csv_rows: list[dict] | None, *,
          csv_name: str = "", no_shipments_confirmed: bool = False, missing_reason: str = "") -> dict:
    """Compare one client's Dashboard with its own CSV. `source` is parse_dashboard output."""
    rules = client_rules.package(client_id)
    report_date = parse_as_of(source.get("as_of", ""))
    result = {"status": INCOMPLETE, "report_date": report_date.isoformat() if report_date else "",
              "csv": csv_name, "rules": getattr(rules, "status", "none"), "checks": [], "reasons": [],
              "orders": 0}
    if rules is None:
        result["reasons"].append("No rule package exists for this client.")
        return result
    if report_date is None:
        result["reasons"].append("The Dashboard as-of date is blank or unreadable.")
        return result

    usage, orders, missing = None, 0, None
    if csv_rows is not None:
        try:
            day = build_eod(csv_rows, rules).get(report_date)
        except ValueError as error:
            result["reasons"].append(f"The shipments could not be read: {error}.")
            day = None
            missing = "unreadable"
        if day is not None:
            usage, orders = day.usage, day.orders
            result["reasons"] += day.flags + day.unknown_items + day.duplicate_tracking_excluded
            result["voided_excluded"] = day.voided_excluded
        elif missing is None:
            missing = f"The shipments ({csv_name or 'export'}) have no rows dated {report_date:%d %b %Y}."
    else:
        missing = missing_reason or "No ShipSidekick shipments were provided for this date."
    if usage is None and no_shipments_confirmed and missing != "unreadable":
        usage = {sku: 0 for sku in rules.skus}
        result["no_shipments_confirmed"] = True
    elif usage is None:
        if missing != "unreadable":
            result["reasons"].append(missing)
        return result
    result["orders"] = orders

    for row in source.get("rows") or []:
        # The same Dashboard-name matching the rest of the workspace uses (one client's rules only).
        sku = ssk_check.match_sheet_row(rules, row.get("product", ""))
        sheet = parse_int(row.get("shipped"))
        item = {"product": row.get("product", "").strip(), "sku": sku or "", "sheet": row.get("shipped", ""),
                "csv": usage.get(sku) if sku else None}
        if sku is None:
            result["reasons"].append(f"Dashboard product {item['product']!r} has no SKU mapping for this client.")
        elif sheet is None:
            result["reasons"].append(f"{item['product']}: Sheet shipped value {row.get('shipped')!r} is not a number.")
        elif sheet != usage[sku]:
            item["gap"] = sheet - usage[sku]
            result["reasons"].append(f"{item['product']} ({sku}): Sheet shows {sheet:,} shipped, "
                                     f"ShipSidekick recount is {usage[sku]:,} (gap {item['gap']:+,}).")
        result["checks"].append(item)
    listed = {item["sku"] for item in result["checks"]}
    for sku in rules.skus:
        if usage.get(sku) and sku not in listed:
            result["reasons"].append(f"ShipSidekick shows {usage[sku]:,} units of {sku}, which is not on the Dashboard.")
    if not result["checks"]:
        result["reasons"].append("The Dashboard lists no products to check.")

    if str(source.get("report_status", "")).upper() == REVIEW:
        result["reasons"].append("The Sheet marks this report REVIEW.")
    result["reasons"] += [f"Dashboard: {warning}." for warning in source.get("warnings") or []]
    result["status"] = REVIEW if result["reasons"] else VERIFIED
    return result
