"""The standard end-of-day inventory report: one structure for every client.

Every value is restated from that client's Sheet Dashboard (the official number) and
Incoming Stocks tab. Nothing is recalculated. The report is ready to send only when the
cross-check in eod_check.py is VERIFIED; otherwise it is held with the reasons first.
Nothing here posts anywhere.
"""
from __future__ import annotations

from eod_check import VERIFIED, parse_as_of
from daily_update import _cover_text, _number, incoming_lines, usable_lines
from inventory import ERROR_VALUE

SECTIONS = ("Inventory", "Forecast", "Incoming", "Alerts", "Actions needed", "Data status")
ORDER_STATUSES = {"REORDER NOW", "OUT OF STOCK"}
ATTENTION = {"past_expected": "past its expected date", "needs_transfer": "needs a transfer to Miami",
             "missing_boxes": "arrived with missing boxes"}


def _value(row: dict, key: str) -> str:
    # A pending or formula-error cell (e.g. #N/A in Order By) is never restated as if it were a value.
    value = row.get(key) or ""
    return "not shown" if not value or ERROR_VALUE.search(value) else value


def _client_flags(incoming: dict | None):
    """(PO, flags) for the shipments the client text includes. Lines held back for internal review
    (unverified SKU or quantity) never surface in Alerts or Actions either."""
    for group in (incoming or {}).get("shipments") or []:
        usable = usable_lines(group)
        if usable:
            yield group["po"], sorted({flag for line in usable for flag in line["flags"]})


def _inventory(rows: list[dict]) -> list[str]:
    return [f"• {row['product']}: starting {_value(row, 'starting')} | shipped {_value(row, 'shipped')} | "
            f"remaining {_value(row, 'remaining')}" for row in rows] or ["• No products are listed on the Dashboard."]


def _forecast(rows: list[dict]) -> list[str]:
    lines = []
    for row in rows:
        parts = [f"{_value(row, 'demand')}/day", _cover_text(row.get("cover", ""))]
        if _value(row, "run_out") != "not shown":
            parts.append(f"runs out {row['run_out']}")
        parts += [f"order by {_value(row, 'order_by')}", f"suggested order {_value(row, 'suggested')}"]
        lines.append(f"• {row['product']}: " + " | ".join(parts))
    return lines or ["• No forecast is shown on the Dashboard."]


def _order_due(row: dict, as_of) -> bool:
    order_by = (row.get("order_by") or "").strip()
    if order_by.lower() == "now":
        return True
    day = parse_as_of(order_by)
    return bool(day and as_of and day <= as_of)


def _needs_order(row: dict, as_of) -> bool:
    remaining = _number(row.get("remaining", ""))
    return (row.get("status", "").upper() in ORDER_STATUSES or _order_due(row, as_of)
            or (remaining is not None and remaining <= 0))


def _alerts(rows: list[dict], as_of, incoming: dict | None) -> list[str]:
    alerts = []
    for row in rows:
        remaining = _number(row.get("remaining", ""))
        status = row.get("status", "").upper()
        if status == "OUT OF STOCK" or (remaining is not None and remaining <= 0):
            alerts.append(f"• {row['product']} is out of stock.")
        elif status == "REORDER NOW":
            alerts.append(f"• {row['product']}: reorder now ({_cover_text(row.get('cover', ''))}).")
        if _order_due(row, as_of) and (row.get("order_by") or "").lower() != "now":
            alerts.append(f"• {row['product']}: order-by date {row['order_by']} has been reached.")
        if row.get("flags"):
            alerts.append(f"• {row['product']}: the Sheet shows a pending or error value.")
    for po, flags in _client_flags(incoming):
        for flag in flags:
            if flag in ATTENTION:
                alerts.append(f"• {po} {ATTENTION[flag]}.")
    return alerts or ["• None."]


def _actions(rows: list[dict], as_of, incoming: dict | None) -> list[str]:
    actions = []
    for row in rows:
        if _needs_order(row, as_of):
            suggested = _number(row.get("suggested", ""))
            amount = f" (suggested {row['suggested']} units)" if suggested and suggested > 0 else ""
            actions.append(f"• Confirm a purchase order for {row['product']}{amount}.")
    for po, flags in _client_flags(incoming):
        if "needs_transfer" in flags:
            actions.append(f"• {po}: please book the transfer to our Miami warehouse.")
    actions.append("• Share tracking and quantities for any new shipment to our warehouse.")
    return actions


def _data_status(check: dict) -> list[str]:
    date_text = check.get("report_date") or "the report date"
    day = parse_as_of(date_text)
    if day:
        date_text = f"{day:%d %b %Y}"
    if check["status"] == VERIFIED:
        if check.get("no_shipments_confirmed"):
            return [f"• VERIFIED: no shipments on {date_text} (confirmed)."]
        return [f"• VERIFIED: shipments on {date_text} match our ShipSidekick recount "
                f"({check.get('orders', 0):,} orders)."]
    return [f"• {check['status']}: this report is being checked."]


def build_report(client_id: str, client_name: str, source: dict, check: dict,
                 incoming: dict | None = None, incoming_error: str | None = None) -> dict:
    """Same six sections, same order, same fields for every client."""
    rows = source.get("rows") or []
    as_of = parse_as_of(source.get("as_of", ""))
    listed, held_back = incoming_lines(incoming)
    if not listed:
        if incoming_error:
            listed = ["• Not included today."]
        elif incoming is not None and not incoming.get("available"):
            listed = ["• Not tracked in this report."]  # the workbook has no Incoming Stocks tab
        else:
            listed = ["• None logged."]

    sections = [
        {"title": "Inventory", "lines": _inventory(rows)},
        {"title": "Forecast", "lines": _forecast(rows)},
        {"title": "Incoming", "lines": listed},
        {"title": "Alerts", "lines": _alerts(rows, as_of, incoming)},
        {"title": "Actions needed", "lines": _actions(rows, as_of, incoming)},
        {"title": "Data status", "lines": _data_status(check)},
    ]
    hold = list(check.get("reasons") or [])
    if incoming_error:
        hold.append(f"Incoming shipments were not read: {incoming_error}")
    notes = [f"Left out of Incoming until SKU and quantity are verified: {', '.join(held_back)}."] if held_back else []
    ready = check["status"] == VERIFIED and not hold

    lines = []
    if not ready:
        lines += [f"HOLD, DO NOT SEND ({check['status']}): " + " ".join(hold or ["Not verified."]), ""]
    lines.append(f"Hi @channel! Here is the {client_name} End-of-Day Inventory Report "
                 f"as of {source.get('as_of') or 'an unknown date'}.")
    for number, section in enumerate(sections, start=1):
        lines += ["", f"*{number}. {section['title']}*", *section["lines"]]
    lines += ["", "Thank you!"]
    return {"client_id": client_id, "as_of": source.get("as_of", ""), "status": check["status"],
            "ready_to_send": ready, "hold_reasons": hold, "notes": notes, "held_back": held_back,
            "sections": sections, "check": check, "text": "\n".join(lines)}
