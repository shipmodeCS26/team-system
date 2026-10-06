"""Read-only incoming shipments per client, from the workbook's Incoming Stocks tab.

Incoming units are reported next to on-hand and never added to it. Only the columns
below are requested from Google; box dimensions and warehouse notes are not read.
Flags describe what the Sheet shows; they never change a number.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from inventory import ERRORS as SHEET_ERRORS, SHEET_ID, SourceError, source_config
from ledger_sources import SheetReader, column_letter, parse_day, parse_int

log = logging.getLogger(__name__)
TAB = "'Incoming Stocks'"
LAST_ROW = 2000  # reaching this row is reported as truncated, never silently dropped
CACHE_SECONDS = 45
COLUMNS = {
    "po": "PO (Every Row)", "tracking": "Tracking (Every Row)", "product": "Product (Report Name)",
    "sku": "Verified SKU", "units": "Verified Inventory Units", "status": "Status",
    "treatment": "Forecast Treatment", "boxes_expected": "Boxes Expected", "boxes_received": "Boxes Received",
    "units_received": "Units Received", "received_date": "Received Date", "where": "Where It Is Now",
    "expected_date": "Expected in Miami",
}
FLAGS = {
    "missing_boxes": "Missing boxes",
    "receipt_not_recorded": "Receipt not recorded",
    "needs_transfer": "Needs transfer",
    "sku_unverified": "SKU not verified",
    "past_expected": "Past expected date",
}
ERRORS = {**SHEET_ERRORS,
          "incoming_layout": "The Incoming Stocks tab headers changed; incoming shipments were not loaded."}
_cache = {}
_lock = threading.Lock()


def _history(line: dict) -> bool:
    return line["treatment"].upper().startswith("INCLUDED IN LATEST COUNT")


def _none(value: str) -> bool:
    """Blank or an explicit 0 both mean nothing was received."""
    return not value.strip() or parse_int(value) == 0


RECEIVED_WORD = re.compile(r"\b(arrived|delivered|received)\b")
NEGATED = re.compile(r"\b(not|never|un)\s*(yet\s+)?(arrived|delivered|received)\b|\bundelivered\b")


def whole_units(value) -> int | None:
    """A whole, non-negative quantity, or None. '10.9' and '-100' are rejected, never used."""
    number = parse_int(value)
    text = str(value or "").replace(",", "").strip().strip("()")
    try:
        return number if number is not None and number >= 0 and float(text) == number else None
    except ValueError:
        return None


TRANSFER_DONE = re.compile(r"\btransferred\b|\btransfer\s+(complete|completed|done|finished)\b")
TRANSFER_NEGATED = re.compile(r"\b(not|never|un)\s*(yet\s+)?transferred\b|\btransfer\s+not\s+(complete|done|finished)")


def _says_received(status: str) -> bool:
    """'Arrived in warehouse' counts; 'Not received' or 'Undelivered' does not."""
    return bool(RECEIVED_WORD.search(status)) and not NEGATED.search(status)


def line_flags(line: dict, today: date) -> list[str]:
    if _history(line):
        return []  # already covered by the latest physical count
    flags = []
    status = line["status"].lower()
    expected, received = parse_int(line["boxes_expected"]), parse_int(line["boxes_received"])
    nothing_received = _none(line["boxes_received"]) and _none(line["units_received"])
    if line["received_date"] and expected is not None and received is not None and received < expected:
        flags.append("missing_boxes")
    if _says_received(status) and _none(line["boxes_received"]):
        flags.append("receipt_not_recorded")
    if "transfer" in status and (TRANSFER_NEGATED.search(status) or not TRANSFER_DONE.search(status)):
        flags.append("needs_transfer")
    if line["treatment"].upper().startswith("REVIEW") or line["sku"].upper() in ("", "REVIEW"):
        flags.append("sku_unverified")
    expected_day = parse_day(line["expected_date"])
    if expected_day and expected_day < today and nothing_received:
        flags.append("past_expected")
    return flags


def parse_incoming(values_by_key: dict[str, list], today: date) -> dict:
    """Group Sheet rows by PO and tracking number. values_by_key maps COLUMNS keys to column cells."""
    length = max((len(column) for column in values_by_key.values()), default=0)
    groups, order = {}, []
    for i in range(length):
        line = {key: (str(column[i][0]).strip() if i < len(column) and column[i] else "")
                for key, column in values_by_key.items()}
        if not any(line.values()):
            continue
        # A row with neither PO nor tracking is its own shipment; unrelated rows are never merged.
        key = (line["po"], line["tracking"]) if line["po"] or line["tracking"] else ("", "", i)
        if key not in groups:
            groups[key] = {"po": line["po"] or f"No PO (Sheet row {i + 2})", "tracking": line["tracking"], "lines": []}
            order.append(key)
        line["flags"] = line_flags(line, today)
        groups[key]["lines"].append(line)

    shipments, history, incoming = [], [], {}
    for key in order:
        group = groups[key]
        # A PO can be partly counted already: received lines go to history, the rest stay open.
        for target, lines in ((history, [l for l in group["lines"] if _history(l)]),
                              (shipments, [l for l in group["lines"] if not _history(l)])):
            if lines:
                target.append({**group, "lines": lines,
                               "flags": sorted({flag for line in lines for flag in line["flags"]})})
    for group in shipments:
        for line in group["lines"]:
            units = whole_units(line["units"])
            if "sku_unverified" in line["flags"] or units is None:
                continue
            total = incoming.setdefault(line["sku"], {"sku": line["sku"], "product": line["product"], "units": 0})
            total["units"] += units
    # Lines left out of the totals: no verified SKU, or no whole-number quantity.
    unverified = sum("sku_unverified" in line["flags"] or whole_units(line["units"]) is None
                     for group in shipments for line in group["lines"])
    # Columns are read up to LAST_ROW; a filled final row means the tab may continue past it.
    truncated = length >= LAST_ROW - 1
    return {"available": True, "truncated": truncated, "shipments": shipments, "history": history,
            "incoming_by_sku": sorted(incoming.values(), key=lambda row: row["sku"]),
            "unverified_lines": unverified}


def _read_one(client_id: str, sheet_id: str, credentials: dict, today: date) -> dict:
    with _lock:
        cached = _cache.get((client_id, sheet_id))
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
    reader = SheetReader(sheet_id, credentials)
    header = (reader.batch([f"{TAB}!1:1"], optional=True)[0] or [[]])[0]
    if not header:
        result = {"id": client_id, "available": False}
    else:
        positions = {str(name).strip(): i for i, name in enumerate(header)}
        if any(name not in positions for name in COLUMNS.values()):
            raise SourceError("incoming_layout")
        letters = {key: column_letter(positions[name]) for key, name in COLUMNS.items()}
        blocks = reader.batch([f"{TAB}!{letter}2:{letter}{LAST_ROW}" for letter in letters.values()])
        result = {"id": client_id, **parse_incoming(dict(zip(letters, blocks)), today)}
    with _lock:
        _cache[(client_id, sheet_id)] = (time.monotonic(), result)
    return result


def read_incoming(client_ids: list[str], today: date) -> list[dict]:
    """Each client is read separately; one failure never hides another."""
    sources, credentials = source_config()
    out = []
    with ThreadPoolExecutor(max_workers=max(1, min(6, len(client_ids)))) as pool:
        futures = {cid: pool.submit(_read_one, cid, sources[cid], credentials, today)
                   for cid in client_ids
                   if isinstance(sources.get(cid), str) and SHEET_ID.fullmatch(sources[cid])}
        for cid in client_ids:
            if cid not in futures:
                out.append({"id": cid, "error_code": "not_configured", "error": ERRORS["not_configured"]})
                continue
            try:
                out.append(futures[cid].result())
            except SourceError as error:
                log.warning("incoming source failed client=%s code=%s status=%s", cid, error.code, error.status)
                out.append({"id": cid, "error_code": error.code, "error": ERRORS.get(error.code, ERRORS["read_failed"])})
            except Exception as error:
                log.warning("incoming source failed client=%s type=%s", cid, type(error).__name__)
                out.append({"id": cid, "error_code": "read_failed", "error": ERRORS["read_failed"]})
    return out
