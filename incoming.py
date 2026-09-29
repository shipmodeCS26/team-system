"""Read-only incoming shipments per client, from the workbook's Incoming Stocks tab.

Incoming units are reported next to on-hand and never added to it. Only the columns
below are requested from Google; box dimensions and warehouse notes are not read.
Flags describe what the Sheet shows; they never change a number.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from inventory import ERRORS as SHEET_ERRORS, SHEET_ID, SourceError, source_config
from ledger_sources import SheetReader, column_letter, parse_day, parse_int

log = logging.getLogger(__name__)
TAB = "'Incoming Stocks'"
LAST_ROW = 500
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


def line_flags(line: dict, today: date) -> list[str]:
    if _history(line):
        return []  # already covered by the latest physical count
    flags = []
    status = line["status"].lower()
    expected, received = parse_int(line["boxes_expected"]), parse_int(line["boxes_received"])
    nothing_received = not line["boxes_received"] and not line["units_received"]
    if line["received_date"] and expected is not None and received is not None and received < expected:
        flags.append("missing_boxes")
    if ("arrived" in status or "delivered" in status or "received" in status) and not line["boxes_received"]:
        flags.append("receipt_not_recorded")
    if "transfer" in status:
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
        key = (line["po"], line["tracking"])
        if key not in groups:
            groups[key] = {"po": line["po"] or "No PO", "tracking": line["tracking"], "lines": []}
            order.append(key)
        line["flags"] = line_flags(line, today)
        groups[key]["lines"].append(line)

    shipments, history, incoming = [], [], {}
    for key in order:
        group = groups[key]
        group["flags"] = sorted({flag for line in group["lines"] for flag in line["flags"]})
        if all(_history(line) for line in group["lines"]):
            history.append(group)
            continue
        shipments.append(group)
        for line in group["lines"]:
            units = parse_int(line["units"])
            if _history(line) or "sku_unverified" in line["flags"] or units is None:
                continue
            total = incoming.setdefault(line["sku"], {"sku": line["sku"], "product": line["product"], "units": 0})
            total["units"] += units
    unverified = sum("sku_unverified" in line["flags"] and not _history(line)
                     for group in shipments for line in group["lines"])
    return {"available": True, "shipments": shipments, "history": history,
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
