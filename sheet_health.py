"""Sheet watchdog (#28, slice 1): is each client's Sheet up to date and correct?

Pure functions: they take what the existing read-only readers already returned (Dashboard,
Incoming Stocks, ShipSidekick stock) and judge it. Nothing is read, written or sent here.

Accuracy rules:
- OK only when the sources agree exactly. A source that did not load is UNKNOWN, never OK.
- A source that is not connected for a client is OFF and is named next to the state.
- Reasons use fixed wording plus SKU, product, column, Sheet row, PO and dates only. Free-text
  cells (status, location, tracking) are never echoed, so customer details cannot leak.
- Which ShipSidekick number the Sheet should equal is undecided (#16), so a stock difference is a
  WARNING, never WRONG.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from daily_orders import sheet_day
from incoming import FLAGS as INCOMING_FLAGS, whole_units
from inventory import FORECAST_ONLY
from ledger_sources import parse_day, parse_int
import ssk_check

OK, WARNING, WRONG, UNKNOWN, OFF = "ok", "warning", "wrong", "unknown", "off"
RANK = {WRONG: 0, UNKNOWN: 1, WARNING: 2, OK: 3}
# Spreadsheet error codes only; PENDING is judged separately (a Warning, not an error).
ERROR_CODE = re.compile(r"#(?:DIV/0!|VALUE!|REF!|N/A|NUM!|NAME\?|NULL!|ERROR!|SPILL!|CALC!)", re.I)
PENDING = re.compile(r"\bPENDING\b", re.I)
COLUMNS = {"product": "Product", "starting": "Starting stock", "shipped": "Units shipped",
           "remaining": "Remaining", "demand": "Daily demand", "cover": "Days of cover",
           "status": "Reorder status"}
SUMMARY = {"products": "A7", "reorder": "C7", "out": "E7", "on_hand": "G7"}
CUTOFF = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


def parse_cutoff(value: str | None) -> tuple[time | None, bool]:
    """(cut-off, valid). Unset is (None, True); anything but HH:MM is (None, False)."""
    text = (value or "").strip()
    if not text:
        return None, True
    match = CUTOFF.fullmatch(text)
    return (time(int(match[1]), int(match[2])), True) if match else (None, False)


def _day(value: date) -> str:
    return f"{value:%a} {value:%b} {value.day}, {value:%Y}"


def expected_as_of(now: datetime, cutoff: time) -> date:
    """Mon–Fri after the cut-off: today. Before it: the previous workday. Sat/Sun: Friday.
    Mon–Fri is the proposed default awaiting Gly (#28 open question 2)."""
    today = now.date()
    if today.weekday() >= 5:
        return today - timedelta(days=today.weekday() - 4)
    if now.time() >= cutoff:
        return today
    previous = today - timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= timedelta(days=1)
    return previous


def _problem(state, where, reason):
    return {"state": state, "where": where, "reason": reason}


def _check(key, title, problems, state=None, note=""):
    if state is None:
        state = min((p["state"] for p in problems), key=RANK.get, default=OK)
    return {"key": key, "title": title, "state": state, "problems": problems, "note": note}


def date_check(sheet: dict, now: datetime, cutoff: time | None, cutoff_valid: bool = True) -> dict:
    title, where = "Up to date", "Dashboard as-of (B4)"
    text = str(sheet.get("as_of") or "").strip()
    as_of = sheet_day(text)
    if not text:
        return _check("date", title, [_problem(WRONG, where, "The as-of date is blank.")])
    if as_of is None:
        return _check("date", title, [_problem(WRONG, where, "The as-of cell is not a date.")])
    if as_of > now.date():
        return _check("date", title, [_problem(WRONG, where, f"The Sheet shows {_day(as_of)}, which is in the future.")])
    if not cutoff_valid:
        return _check("date", title, [_problem(UNKNOWN, where, "The cut-off setting (SHEET_CUTOFF_ET) is not HH:MM, "
                                                               "so lateness can't be judged.")])
    if cutoff is None:
        if as_of == now.date():
            return _check("date", title, [], note=f"Shows today, {_day(as_of)}.")
        return _check("date", title, [_problem(WARNING, where, f"The Sheet shows {_day(as_of)}. The cut-off time is not "
                                                                "set yet, so whether it is late can't be judged.")])
    expected = expected_as_of(now, cutoff)
    if as_of < expected:
        return _check("date", title, [_problem(WRONG, where, f"The Sheet shows {_day(as_of)}; it should show "
                                                              f"{_day(expected)} or later (cut-off {cutoff:%H:%M} ET).")])
    return _check("date", title, [], note=f"Shows {_day(as_of)}.")


def cell_check(sheet: dict) -> dict:
    """Formula errors are Wrong; PENDING is a Warning. Forecast-only columns follow the EOD report,
    which shows them as "not shown" and does not flag them."""
    problems = []
    for row in sheet.get("rows", []):
        for key, label in COLUMNS.items():
            value = str(row.get(key) or "")
            where = f"Dashboard row {row.get('sheet_row', '?')} · {label}"
            if ERROR_CODE.search(value):
                problems.append(_problem(WRONG, where, f"{row['product']}: formula error {ERROR_CODE.search(value)[0].upper()}."))
            elif PENDING.search(value):
                problems.append(_problem(WARNING, where, f"{row['product']}: the value is PENDING."))
    for key, cell in SUMMARY.items():
        value = str((sheet.get("summary") or {}).get(key) or "")
        label = (sheet.get("summary_labels") or {}).get(key) or "summary"
        if ERROR_CODE.search(value):
            problems.append(_problem(WRONG, f"Dashboard summary {cell}", f"{label}: formula error {ERROR_CODE.search(value)[0].upper()}."))
        elif PENDING.search(value):
            problems.append(_problem(WARNING, f"Dashboard summary {cell}", f"{label}: the value is PENDING."))
    status = str(sheet.get("report_status") or "")
    if ERROR_CODE.search(status):
        problems.append(_problem(WRONG, "Dashboard report status (E4)", f"Formula error {ERROR_CODE.search(status)[0].upper()}."))
    elif PENDING.search(status):
        problems.append(_problem(WARNING, "Dashboard report status (E4)", "The value is PENDING."))
    action = str(sheet.get("action_list") or "")
    if ERROR_CODE.search(action):  # the EOD report holds on this too
        problems.append(_problem(WRONG, "Dashboard action list (A11)", f"Formula error {ERROR_CODE.search(action)[0].upper()}."))
    return _check("cells", "Formula errors", problems)


def dashboard_check(sheet: dict) -> dict:
    problems = []
    if not sheet.get("rows"):
        problems.append(_problem(WRONG, "Dashboard rows 15–39", "No product rows were found on the Dashboard."))
    if str(sheet.get("report_status", "")).upper() == "REVIEW":
        problems.append(_problem(WARNING, "Dashboard report status (E4)", "The Sheet marks this report REVIEW."))
    for row in sheet.get("rows", []):
        remaining = str(row.get("remaining") or "")
        number = None if ERROR_CODE.search(remaining) else parse_int(remaining)  # "(5)" is negative, as in the EOD
        if number is not None and number < 0:
            problems.append(_problem(WARNING, f"Dashboard row {row.get('sheet_row', '?')} · Remaining",
                                     f"{row['product']}: the remaining balance is negative."))
    if "Reorder summary disagrees with product rows" in sheet.get("warnings", []):
        problems.append(_problem(WARNING, "Dashboard summary C7",
                                 "The reorder count disagrees with the products marked REORDER NOW."))
    if sheet.get("may_continue"):
        problems.append(_problem(WARNING, "Dashboard row 39", "Products may continue past row 39; they are not checked."))
    return _check("dashboard", "Dashboard warnings", problems)


def _number(value):
    return "—" if value is None else f"{value:,}"


def stock_check(client_id: str, sheet: dict, store: dict | None, sheet_current: bool) -> dict:
    title = "Stock vs ShipSidekick"
    if store is None:
        return _check("stock", title, [], OFF, "ShipSidekick is not connected.")
    if store.get("error_code") == "not_configured":
        return _check("stock", title, [], OFF, "No ShipSidekick key for this store.")
    if store.get("error_code"):
        return _check("stock", title, [_problem(UNKNOWN, "ShipSidekick", "ShipSidekick stock did not load.")])
    if not sheet_current:
        return _check("stock", title, [_problem(UNKNOWN, "ShipSidekick", "Not compared: the Sheet is not current or did not load.")])
    result = ssk_check.compare(client_id, store["levels"], sheet, store.get("truncated", False))
    problems = []
    for item in result["skus"]:
        where = f"SKU {item['sku']}" + (f" · {item['label']}" if item["label"] else "")
        if item["status"] == "DIFFERENT":
            ssk = item["ssk"] or {}
            problems.append(_problem(WARNING, where, (
                f"Differs: Sheet {_number(item['sheet_remaining'])}; ShipSidekick available "
                f"{_number(ssk.get('available'))} ({item['vs_available']:+,}), available + committed "
                f"{_number((ssk.get('available') or 0) + (ssk.get('committed') or 0))} "
                f"({item['vs_available_committed']:+,}). Which number should match is undecided (#16).")))
        elif item["status"] == "REVIEW":
            problems.append(_problem(WARNING, where, "; ".join(item["notes"]) + "."))
    # Records that were never compared keep the check from reading OK.
    for level in result["unmatched_ssk"]:
        problems.append(_problem(WARNING, f"ShipSidekick SKU {level['sku'] or '(blank)'}",
                                 f"{level['title'] or 'Untitled'}: not in this client's SKU rules; not compared."))
    for product in result["unmatched_sheet"]:
        problems.append(_problem(WARNING, "Dashboard", f"{product}: not in this client's SKU rules; not compared."))
    for part in result["components"]:
        if part["note"] != ssk_check.KIT_USED:
            problems.append(_problem(WARNING, f"ShipSidekick SKU {part['sku'] or '(blank)'}", f"{part['note']}; not compared."))
    if result["blank_skus"]:
        problems.append(_problem(WARNING, "ShipSidekick", f"{result['blank_skus']} item(s) have no SKU."))
    for sku in result["duplicate_skus"]:
        problems.append(_problem(WARNING, f"ShipSidekick SKU {sku}", "This SKU appears more than once."))
    if store.get("environment") == "test":
        problems.append(_problem(WARNING, "ShipSidekick", "Reading ShipSidekick's TEST environment, not production."))
    if result["rule_status"] == "NONE":
        problems.append(_problem(UNKNOWN, "SKU rules", "This client has no SKU rules, so stock can't be matched."))
    elif result["rule_status"] != "APPROVED":
        problems.append(_problem(WARNING, "SKU rules", "The SKU rules used to match stock are proposed, not approved by ShipMode yet."))
    return _check("stock", title, problems)


def incoming_check(incoming: dict | None) -> dict:
    title = "Incoming"
    if incoming is None or incoming.get("error_code") == "not_configured":
        return _check("incoming", title, [], OFF, "Incoming is not connected.")
    if incoming.get("error_code"):
        return _check("incoming", title, [_problem(UNKNOWN, "Incoming Stocks tab", "The Incoming Stocks tab did not load.")])
    if not incoming.get("available"):
        return _check("incoming", title, [], OFF, "This workbook has no Incoming Stocks tab.")
    problems = []
    for shipment in incoming.get("shipments", []):
        for line in shipment["lines"]:
            label = line["sku"] if line["sku"] and not ERROR_CODE.search(line["sku"]) else line["product"] or "no product"
            if "sku_unverified" not in line["flags"] and whole_units(line["units"]) is None:
                # Left out of the incoming totals, and the daily/EOD reports hold on it.
                problems.append(_problem(WARNING, f"PO {shipment['po']} · {label}",
                                         "Verified Inventory Units is blank or not a whole number; left out of incoming totals."))
            expected = parse_day(line.get("expected_date"))
            for flag in line["flags"]:
                reason = INCOMING_FLAGS[flag]
                if flag == "past_expected" and expected:
                    reason += f" ({_day(expected)}); nothing received"
                problems.append(_problem(WARNING, f"PO {shipment['po']} · {label}", reason + "."))
    if incoming.get("truncated"):
        problems.append(_problem(WARNING, "Incoming Stocks tab", "The tab continues past the rows read; the rest are not checked."))
    return _check("incoming", title, problems)


def client_health(client_id: str, sheet: dict, incoming: dict | None, store: dict | None,
                  now: datetime, cutoff: time | None, cutoff_valid: bool = True) -> dict:
    base = {"client_id": client_id, "source_url": sheet.get("source_url", ""), "as_of": sheet.get("as_of", "")}
    if sheet.get("error_code") == "not_configured":
        return {**base, "state": OFF, "off": ["Sheet"], "checks": [],
                "note": "No Sheet is connected for this client."}
    if sheet.get("error_code") or "rows" not in sheet:
        # The Dashboard checks can't run, but the independently read sources still can.
        checks = [_check("sheet", "Sheet readable", [_problem(UNKNOWN, "Dashboard tab", "The Dashboard did not load.")]),
                  stock_check(client_id, sheet, store, False), incoming_check(incoming)]
    else:
        dated = date_check(sheet, now, cutoff, cutoff_valid)
        checks = [_check("sheet", "Sheet readable", []), dated, cell_check(sheet), dashboard_check(sheet),
                  stock_check(client_id, sheet, store, dated["state"] == OK), incoming_check(incoming)]
    live = [c["state"] for c in checks if c["state"] != OFF]
    return {**base, "state": min(live, key=RANK.get), "checks": checks, "note": "",
            "off": [c["title"].replace("Stock vs ", "") for c in checks if c["state"] == OFF]}
