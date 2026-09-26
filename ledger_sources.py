"""Read-only inputs for the calculated inventory view.

Reads, per client workbook:
  - Daily Sales (the ShipSidekick export): only eod.REQUIRED_COLUMNS. Customer
    names and addresses are never requested from Google.
  - Manual Counts, Receipts, Adjustments & Reships: approved rows only.
Private approved baselines can also come from INVENTORY_BASELINES_JSON.
Nothing is ever written back to a Sheet.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

import client_rules
import eod
import ledger
from inventory import ERRORS as SHEET_ERRORS, SCOPES, SHEET_ID, SourceError, source_config

log = logging.getLogger(__name__)
CACHE_SECONDS = 120
_cache = {}
ERRORS = {**SHEET_ERRORS,
          "sales_layout": "The Daily Sales tab is missing a required ShipSidekick column; nothing was calculated."}
_lock = threading.Lock()
API = "https://sheets.googleapis.com/v4/spreadsheets/{}/values:batchGet"


def column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def parse_int(value) -> int | None:
    text = str(value or "").replace(",", "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        return int(float(text))
    except ValueError:
        return None


def parse_day(value) -> date | None:
    text = str(value or "").strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def count_timing(count_type: str) -> str:
    text = (count_type or "").upper()
    if "CLOSING" in text or "AFTER" in text:
        return ledger.AFTER
    if "OPENING" in text or "BEFORE" in text:
        return ledger.BEFORE
    return f"unclear: {count_type}"


def _is_approved(record: dict) -> bool:
    return (record.get("Approval Status", "").strip().upper() == "APPROVED"
            and bool(record.get("Approved By", "").strip()))


def _records(values: list[list]) -> list[dict]:
    if not values:
        return []
    header = [str(h).strip() for h in values[0]]
    return [{header[i]: str(row[i]).strip() for i in range(min(len(header), len(row)))}
            for row in values[1:] if any(str(c).strip() for c in row)]


class SheetReader:
    def __init__(self, sheet_id: str, credentials: dict):
        creds = service_account.Credentials.from_service_account_info(credentials, scopes=SCOPES)
        self.session = AuthorizedSession(creds)
        self.sheet_id = sheet_id

    def batch(self, ranges: list[str], optional: bool = False) -> list[list[list]]:
        try:
            response = self.session.get(API.format(self.sheet_id), timeout=30,
                                        params=[("ranges", r) for r in ranges] +
                                               [("valueRenderOption", "FORMATTED_VALUE")])
        except requests.RequestException:
            raise SourceError("unavailable")
        if response.status_code == 400 and optional:
            return [[] for _ in ranges]  # tab does not exist in this workbook
        if response.status_code != 200:
            code = {401: "access_denied", 403: "access_denied", 404: "not_found"}.get(
                response.status_code,
                "unavailable" if response.status_code in (429, 500, 502, 503, 504) else "read_failed")
            raise SourceError(code, response.status_code)
        return [block.get("values", []) for block in response.json().get("valueRanges", [])]

    def daily_sales(self) -> list[dict]:
        header = (self.batch(["'Daily Sales'!1:1"])[0] or [[]])[0]
        positions = {str(name).strip(): i for i, name in enumerate(header)}
        missing = [c for c in eod.REQUIRED_COLUMNS if c not in positions]
        if missing:
            raise SourceError("sales_layout")
        ranges = [f"'Daily Sales'!{column_letter(positions[c])}2:{column_letter(positions[c])}"
                  for c in eod.REQUIRED_COLUMNS]
        columns = self.batch(ranges)
        length = max((len(col) for col in columns), default=0)
        rows = []
        for i in range(length):
            row = {name: (columns[j][i][0] if i < len(columns[j]) and columns[j][i] else "")
                   for j, name in enumerate(eod.REQUIRED_COLUMNS)}
            if any(row.values()):
                rows.append(row)
        return rows

    def approved_records(self) -> tuple[list[dict], list[dict], list[dict]]:
        counts, receipts, adjustments = self.batch(
            ["'Manual Counts'!A1:J", "'Receipts'!A1:J", "'Adjustments & Reships'!A1:K"], optional=True)
        return ([r for r in _records(counts) if _is_approved(r)],
                [r for r in _records(receipts) if _is_approved(r)],
                [r for r in _records(adjustments) if _is_approved(r)])


def private_baselines(client_id: str) -> dict[str, ledger.Baseline]:
    """Approved counts entered in private Render settings (never in Git)."""
    raw = os.getenv("INVENTORY_BASELINES_JSON")
    if not raw:
        return {}
    result = {}
    for sku, item in (json.loads(raw).get(client_id) or {}).items():
        day, qty = parse_day(item.get("date")), parse_int(item.get("quantity"))
        if day and qty is not None and item.get("approved_by"):
            result[sku] = ledger.Baseline(sku, qty, day, item.get("timing", ""), item["approved_by"],
                                          "Private approved baseline")
    return result


def build_inputs(client_id: str, counts, receipts, adjustments):
    baselines = private_baselines(client_id)
    for record in counts:
        day, qty = parse_day(record.get("Count Date")), parse_int(record.get("Count Quantity"))
        sku = record.get("SKU", "").strip()
        if not (day and qty is not None and sku):
            continue
        current = baselines.get(sku)
        if current is None or day >= current.day:
            baselines[sku] = ledger.Baseline(sku, qty, day, count_timing(record.get("Count Type", "")),
                                             record["Approved By"],
                                             record.get("Record ID") or "Manual Counts")
    movements = []
    for kind, records, qty_field, day_field, ref_field in (
            ("RECEIPT", receipts, "Quantity", "Receipt Date", "PO / Tracking"),
            ("ADJUSTMENT", adjustments, "Quantity Delta", "Effective Date", "Related Order / Tracking")):
        for record in records:
            day, qty = parse_day(record.get(day_field)), parse_int(record.get(qty_field))
            if day and qty is not None and record.get("SKU"):
                movements.append(ledger.Movement(record["SKU"].strip(), qty, day, kind,
                                                 record["Approved By"], record.get(ref_field, "")))
    return baselines, movements


def serialize(result: dict, reports: dict, rules) -> dict:
    latest = max(reports) if reports else None
    report = reports.get(latest)
    return {
        **{k: v for k, v in result.items() if k != "skus"},
        "skus": [{
            "sku": row.sku, "label": row.label, "status": row.status, "reasons": row.reasons,
            "calculated": row.calculated, "usage": row.usage, "receipts": row.receipts,
            "adjustments": row.adjustments,
            "baseline": None if row.baseline is None else {
                "quantity": row.baseline.quantity, "date": row.baseline.day.isoformat(),
                "timing": row.baseline.timing, "approved_by": row.baseline.approved_by,
                "source": row.baseline.source},
        } for row in result["skus"]],
        "latest_eod": None if report is None else {
            "date": latest.isoformat(), "orders": report.orders, "missions": report.missions,
            "usage": report.usage, "total_units": report.total_units,
            "needs_review": report.needs_review, "audit": eod.audit_statement(report),
            "findings": (report.flags + report.unknown_items + report.duplicate_tracking_excluded)[:20],
        },
        "rules_source": rules.source,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def calculate_client(client_id: str, sheet_id: str, credentials: dict) -> dict:
    with _lock:
        cached = _cache.get((client_id, sheet_id))
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
    rules = client_rules.package(client_id)
    if rules is None:
        return {"client_id": client_id, "error_code": "no_rules",
                "error": "No inventory rules are defined for this client yet."}
    reader = SheetReader(sheet_id, credentials)
    rows = reader.daily_sales()
    reports = eod.build_eod(rows, rules)
    baselines, movements = build_inputs(client_id, *reader.approved_records())
    result = serialize(ledger.calculate(client_id, rules, reports, baselines, movements), reports, rules)
    with _lock:
        _cache[(client_id, sheet_id)] = (time.monotonic(), result)
    return result


def calculate_clients(client_ids: list[str]) -> list[dict]:
    """Each client is read and calculated separately; one failure never affects another."""
    sources, credentials = source_config()
    out = []
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(client_ids)))) as pool:
        futures = {cid: pool.submit(calculate_client, cid, sources[cid], credentials)
                   for cid in client_ids
                   if isinstance(sources.get(cid), str) and SHEET_ID.fullmatch(sources[cid])}
        for cid in client_ids:
            if cid not in futures:
                out.append({"client_id": cid, "error_code": "not_configured", "error": ERRORS["not_configured"]})
                continue
            try:
                out.append(futures[cid].result())
            except SourceError as error:
                log.warning("ledger source failed client=%s code=%s status=%s", cid, error.code, error.status)
                out.append({"client_id": cid, "error_code": error.code,
                            "error": ERRORS.get(error.code, ERRORS["read_failed"])})
            except Exception as error:
                log.warning("ledger failed client=%s type=%s", cid, type(error).__name__)
                out.append({"client_id": cid, "error_code": "read_failed", "error": ERRORS["read_failed"]})
    return out
