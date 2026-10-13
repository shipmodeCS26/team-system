"""No Movement from a ShipSidekick export (read-only).

Source: either one movement workbook (`MOVEMENT_SHEET_ID`, one tab per client, like the
Shipment Movement Report) or, when that is not set, a `No Movement` tab in each client's
inventory workbook. Either way the tab holds a full, replaced export.

The export has carrier status but no scan timestamps, and each status is only true at the
moment the export was taken. So every age here is measured *as of the export*, never
extrapolated to today:

- as_of = start of the latest Created Date in the export (Miami time). The export was taken
  that day or later, so this can only understate how long a label has been waiting.
- A label counts from the end of its Created Date (dates have no time of day).
- Only labels still `pre_transit` at export time get a Watch / Urgent / Critical tier.
- In-transit and other open statuses have no scan time: they are "Missing data", never a
  guessed age. When the carrier's estimated delivery date had already passed at export time,
  that is reported separately as days past the estimate.
Only the columns below are requested; customer names and addresses are never read.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from inventory import ERRORS as SHEET_ERRORS, SHEET_ID, SourceError, source_config
from ledger_sources import SheetReader, column_letter, parse_day
from tracking import MOVEMENT, REPORT_ZONE, STATUS, classify, organization_matches

log = logging.getLogger(__name__)
TAB = "No Movement"
# Tab names in the shared movement workbook (Shipment Movement Report 2026)
WORKBOOK_TABS = {"claritymd": "ClarityMD", "fascial-labs": "Fascial Labs", "muravai": "Muravai",
                 "nuerosmile": "NeuroSmile", "puravita": "Pure Vita", "onset": "Onset"}
LAST_ROW = 80000
CACHE_SECONDS = 120
COLUMNS = {
    "tracking_number": "Tracking Code", "created": "Created Date", "organization": "Organization",
    "order_number": "Order Name", "carrier": "Carrier", "status": "Tracking Status",
    "estimated_delivery": "Est Delivery Date", "voided": "Voided",
}
OPTIONAL = {"additional": "Additional Tracking Codes"}
ERRORS = {**SHEET_ERRORS,
          "movement_layout": "The shipment export tab is missing a ShipSidekick export column (Tracking Code, "
                             "Created Date, Organization, Order Name, Carrier, Tracking Status, Est Delivery Date, Voided).",
          "movement_empty": "The shipment export tab has no shipments. Paste the latest full ShipSidekick export."}
STATUS_WORDS = {"in_transit": "In transit", "out_for_delivery": "Out for delivery",
                "available_for_pickup": "Ready for pickup", "return_to_sender": "Returning to sender"}
_cache = {}
_lock = threading.Lock()


def _start_of(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=REPORT_ZONE)


def parse_export(client_id: str, columns: dict[str, list]) -> dict:
    """Build classified shipments from column cells. Pure function: no network, no clock."""
    length = max((len(column) for column in columns.values()), default=0)
    raw = []
    for i in range(length):
        row = {key: (str(column[i][0]).strip() if i < len(column) and column[i] else "") for key, column in columns.items()}
        if any(row.values()):
            raw.append(row)
    days = [d for d in (parse_day(r["created"]) for r in raw) if d]
    if not days:
        raise SourceError("movement_empty")
    export_day = max(days)
    as_of = _start_of(export_day)

    warnings, other_org, bad_dates, unknown_status = [], 0, 0, 0
    latest = {}
    duplicates = 0
    for row in raw:
        if row["organization"] and not organization_matches(client_id, row["organization"]):
            other_org += 1  # never show another client's shipment
            continue
        key = (row["tracking_number"], row["carrier"].lower())
        if not row["tracking_number"]:
            continue
        if key in latest:
            duplicates += 1
        latest[key] = row  # later rows in the export win

    shipments, delivered, cancelled = [], 0, 0
    for index, row in enumerate(latest.values(), 1):
        created = parse_day(row["created"])
        status = row["status"].lower().replace("-", "_").replace(" ", "_") or "unknown"
        if row["voided"].strip().lower() in {"yes", "true", "1"}:
            status = "cancelled"
        if status not in STATUS:
            unknown_status += 1
            status = "unknown"
        if status == "delivered":
            delivered += 1
            continue
        if status == "cancelled":
            cancelled += 1
            continue
        if not created:
            bad_dates += 1
        record = {
            "id": index, "client_id": client_id, "tracking_number": row["tracking_number"][:120],
            "carrier": row["carrier"].lower()[:40], "order_number": row["order_number"][:120],
            "fulfillment_status": "from export", "carrier_status": status,
            "label_created_at": _start_of(created).isoformat() if created else None,
            "shipped_at": None, "last_movement_at": None, "date_precision": "report_date",
            "source": "ShipSidekick export (No Movement tab)", "case_status": "open", "notes": "",
            "events": [], "status_as_of": as_of.isoformat(), "export_day": export_day.isoformat(),
            "label_date": created.isoformat() if created else None,
            "estimated_delivery": row["estimated_delivery"] or None,
        }
        result = classify(record, now=as_of)
        if result["tier"] == "data_gap" and status in MOVEMENT:
            result["reason"] = f"{STATUS_WORDS.get(status, status)} at export; the export has no carrier scan time"
        if row.get("additional"):
            result.update(tier="data_gap", days=None,
                          reason="Several packages on one label; check each tracking code in ShipSidekick")
        eta = parse_day(row["estimated_delivery"])
        result["days_past_estimate"] = (export_day - eta).days if eta and eta < export_day else None
        if result["days_past_estimate"] and result["tier"] == "data_gap" and status in MOVEMENT and not row.get("additional"):
            result["reason"] = (f"{STATUS_WORDS.get(status, status)} at export, {result['days_past_estimate']} days after "
                                "the carrier's estimated delivery; check the latest scans")
        shipments.append(result)

    if other_org:
        warnings.append(f"{other_org} row(s) belong to another organization and were left out")
    if duplicates:
        warnings.append(f"{duplicates} repeated tracking code(s); the last row of each was used")
    if bad_dates:
        warnings.append(f"{bad_dates} open shipment(s) have an unreadable Created Date and are listed as Missing data")
    if unknown_status:
        warnings.append(f"{unknown_status} row(s) have an unrecognised Tracking Status and are listed as Missing data")
    return {"id": client_id, "as_of": as_of.isoformat(), "export_day": export_day.isoformat(),
            "shipments": shipments, "delivered": delivered, "cancelled": cancelled, "warnings": warnings}


def _read_one(client_id: str, sheet_id: str, tab: str, credentials: dict) -> dict:
    tab = "'" + tab.replace("'", "''") + "'"
    with _lock:
        cached = _cache.get((client_id, sheet_id, tab))
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
    reader = SheetReader(sheet_id, credentials)
    header = (reader.batch([f"{tab}!1:1"], optional=True)[0] or [[]])[0]
    if not header:
        result = {"id": client_id, "available": False}
    else:
        positions = {str(name).strip(): i for i, name in enumerate(header)}
        if any(name not in positions for name in COLUMNS.values()):
            raise SourceError("movement_layout")
        wanted = {**COLUMNS, **{k: v for k, v in OPTIONAL.items() if v in positions}}
        letters = {key: column_letter(positions[name]) for key, name in wanted.items()}
        blocks = reader.batch([f"{tab}!{letter}2:{letter}{LAST_ROW}" for letter in letters.values()])
        result = {"available": True, **parse_export(client_id, dict(zip(letters, blocks)))}
    with _lock:
        _cache[(client_id, sheet_id, tab)] = (time.monotonic(), result)
    return result


def read_movement(client_ids: list[str]) -> list[dict]:
    """Each client is read separately; one failure never hides another."""
    sources, credentials = source_config()
    workbook = os.getenv("MOVEMENT_SHEET_ID", "").strip()
    if workbook:  # one shared workbook, one tab per client
        targets = {cid: (workbook, WORKBOOK_TABS[cid]) for cid in client_ids if SHEET_ID.fullmatch(workbook)}
    else:
        targets = {cid: (sources[cid], TAB) for cid in client_ids
                   if isinstance(sources.get(cid), str) and SHEET_ID.fullmatch(sources[cid])}
    out = []
    with ThreadPoolExecutor(max_workers=max(1, min(6, len(client_ids)))) as pool:
        futures = {cid: pool.submit(_read_one, cid, sheet, tab, credentials) for cid, (sheet, tab) in targets.items()}
        for cid in client_ids:
            if cid not in futures:
                out.append({"id": cid, "error_code": "not_configured", "error": ERRORS["not_configured"]})
                continue
            try:
                out.append(futures[cid].result())
            except SourceError as error:
                log.warning("movement source failed client=%s code=%s status=%s", cid, error.code, error.status)
                out.append({"id": cid, "error_code": error.code, "error": ERRORS.get(error.code, ERRORS["read_failed"])})
            except Exception as error:
                log.warning("movement source failed client=%s type=%s", cid, type(error).__name__)
                out.append({"id": cid, "error_code": "read_failed", "error": ERRORS["read_failed"]})
    return out
