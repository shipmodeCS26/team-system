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
from eod import REQUIRED_COLUMNS, build_eod, is_voided, parse_created_date
from incoming import whole_units
from ledger_sources import parse_int

VERIFIED, REVIEW, INCOMPLETE = "VERIFIED", "REVIEW", "INCOMPLETE"
SHEET_OK = {"", "SOURCE VALUES", VERIFIED}  # any other Dashboard report status (REVIEW, INCOMPLETE…) holds


def parse_as_of(value: str) -> date | None:
    return sheet_day(value)


def check(client_id: str, source: dict, csv_rows: list[dict] | None, *,
          csv_name: str = "", no_shipments_confirmed: bool = False, missing_reason: str = "",
          row_numbers: list[int] | None = None, columns: list[str] | None = None) -> dict:
    """Compare one client's Dashboard with its own CSV. `source` is parse_dashboard output.
    `row_numbers` are the rows' physical Sheet rows (default: CSV order from row 2); `columns` the
    CSV header, checked for every audit column (default: the first row's keys)."""
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
    header = columns if columns is not None else (list(csv_rows[0]) if csv_rows else None)
    absent = [name for name in REQUIRED_COLUMNS if header is not None and name not in header]
    if csv_rows is not None and absent:
        # Without Voided or Tracking Code the exclusions and duplicate audit can't run: never a recount.
        result["reasons"].append(f"{csv_name or 'Shipments'} is missing column(s): {', '.join(absent)}.")
        if no_shipments_confirmed:
            result["reasons"].append("'No shipments confirmed' was not applied: the shipments could not be read.")
        return result
    if csv_rows is not None:
        # Only the report date's rows are recounted. A row whose Created Date can't be read could be
        # one of them, so it is named (Sheet row number) and holds the report; it never stops the check.
        dated, numbers, seen = [], [], {}
        mine = lambda row: (str(row.get("Organization") or "").strip().lower() == rules.organization.lower()
                            and not is_voided(row.get("Voided", "")))
        for number, row in zip(row_numbers or range(2, len(csv_rows) + 2), csv_rows):
            try:
                created = parse_created_date(row.get("Created Date", ""))
            except ValueError:
                # A voided row is excluded whatever its date, so an unreadable date there holds nothing.
                if not is_voided(row.get("Voided", "")) and any(str(value or "").strip() for value in row.values()):
                    result["reasons"].append(f"{csv_name or 'Shipments'} row {number}: Created Date "
                                             f"{row.get('Created Date', '')!r} can't be read; it may belong to this date.")
                continue
            tracking = str(row.get("Tracking Code") or "").strip()
            if tracking and mine(row):
                seen.setdefault(tracking, []).append((number, created))
            if created == report_date:
                dated.append(row)
                numbers.append(number)
                if mine(row) and not str(row.get("Items") or "").strip():
                    # Contents can't be recounted, so a zero here would not be proof of zero units.
                    result["reasons"].append(f"{csv_name or 'Shipments'} row {number}: a shipment on this date has no Items.")
        # The same tracking code on another date (a duplicated or re-dated row) is held, never counted twice.
        for tracking, uses in seen.items():
            days = {created for _, created in uses}
            if report_date in days and len(days) > 1:
                rows_text = ", ".join(str(number) for number, _ in uses)
                result["reasons"].append(f"Tracking {tracking} appears on more than one date (rows {rows_text}).")
        try:
            day = build_eod(dated, rules, numbers).get(report_date)
        except ValueError as error:
            result["reasons"].append(f"The shipments could not be read: {error}.")
            day = None
            missing = "unreadable"
        if day is not None and day.orders == 0:
            # Other clients' rows, unknown items or duplicates still hold the report, even when the
            # warehouse confirms no shipments.
            result["reasons"] += day.flags + day.unknown_items + day.duplicate_tracking_excluded
            day = None  # only voided or other clients' rows: an empty day needs the warehouse confirmation
            missing = f"The shipments ({csv_name or 'export'}) have no counted shipments dated {report_date:%d %b %Y}."
        if day is not None:
            usage, orders = day.usage, day.orders
            result["reasons"] += day.flags + day.unknown_items + day.duplicate_tracking_excluded
            other = [origin for origin in day.usage_by_origin if origin != "Miami"]
            if other and len(day.usage_by_origin) == 1:  # more than one origin is already flagged
                result["reasons"].append(f"Shipments did not originate in Miami: {', '.join(other)}.")
            result["voided_excluded"] = day.voided_excluded
        elif missing is None:
            missing = f"The shipments ({csv_name or 'export'}) have no rows dated {report_date:%d %b %Y}."
    else:
        missing = missing_reason or "No ShipSidekick shipments were provided for this date."
    # "No shipments confirmed" only replaces an empty day, never a failed read of ShipSidekick data.
    if usage is None and no_shipments_confirmed and missing != "unreadable" and not missing_reason:
        usage = {sku: 0 for sku in rules.skus}
        result["no_shipments_confirmed"] = True
    elif usage is None:
        if missing != "unreadable":
            result["reasons"].append(missing)
        if no_shipments_confirmed and missing_reason:
            result["reasons"].append("'No shipments confirmed' was not applied: the shipments could not be read.")
        return result
    result["orders"] = orders

    mapped = {}
    for row in source.get("rows") or []:
        sku = ssk_check.match_sheet_row(rules, row.get("product", ""))
        if sku:
            mapped.setdefault(sku, []).append(row.get("product", "").strip())
    for sku, products in mapped.items():
        if len(products) > 1:
            # Each row would be compared with the same total, so two rows of 10 could "match" a recount of 10.
            result["reasons"].append(f"More than one Dashboard row maps to {sku}: {', '.join(products)}.")
    for row in source.get("rows") or []:
        # The same Dashboard-name matching the rest of the workspace uses (one client's rules only).
        sku = ssk_check.match_sheet_row(rules, row.get("product", ""))
        sheet = whole_units(row.get("shipped"))  # 2.5 or -1 is never read as a whole number of units
        item = {"product": row.get("product", "").strip(), "sku": sku or "", "sheet": row.get("shipped", ""),
                "csv": usage.get(sku) if sku else None}
        if sku is None:
            result["reasons"].append(f"Dashboard product {item['product']!r} has no SKU mapping for this client.")
        elif sheet is None:
            result["reasons"].append(f"{item['product']}: Sheet shipped value {row.get('shipped')!r} is not a whole number.")
        elif sheet != usage[sku]:
            item["gap"] = sheet - usage[sku]
            result["reasons"].append(f"{item['product']} ({sku}): Sheet shows {sheet:,} shipped, "
                                     f"ShipSidekick recount is {usage[sku]:,} (gap {item['gap']:+,}).")
        if parse_int(row.get("remaining")) is None:
            # Without a usable balance the Inventory, Alerts and Actions can't be stated.
            result["reasons"].append(f"{item['product']}: Sheet remaining value {row.get('remaining')!r} is not a number.")
        result["checks"].append(item)
    listed = {item["sku"] for item in result["checks"]}
    for sku in rules.skus:
        if usage.get(sku) and sku not in listed:
            result["reasons"].append(f"ShipSidekick shows {usage[sku]:,} units of {sku}, which is not on the Dashboard.")
    if not result["checks"]:
        result["reasons"].append("The Dashboard lists no products to check.")

    sheet_status = str(source.get("report_status", "")).strip().upper()
    if sheet_status not in SHEET_OK:
        result["reasons"].append(f"The Sheet marks this report {sheet_status}.")
    result["reasons"] += [f"Dashboard: {warning}." for warning in source.get("warnings") or []]
    result["status"] = REVIEW if result["reasons"] else VERIFIED
    return result
