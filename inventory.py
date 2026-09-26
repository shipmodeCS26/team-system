"""Read-only Google Sheets dashboard adapter. Sheet values are never recalculated here."""

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
import requests


SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
SHEET_ID = re.compile(r"[A-Za-z0-9_-]{20,}")
ERROR_VALUE = re.compile(r"#(?:DIV/0!|VALUE!|REF!|N/A|NUM!|NAME\?|NULL!|ERROR!|SPILL!|CALC!)|PENDING", re.I)
LAST_ROW = 39
log = logging.getLogger(__name__)
_cache = {}
_cache_lock = threading.Lock()

# Shown to signed-in staff. Messages never include spreadsheet IDs or credential details.
ERRORS = {
    "not_configured": "No workbook is mapped for this client in the private deployment settings.",
    "access_denied": "Access denied. Share this workbook with the service account as Viewer.",
    "not_found": "Workbook not found. Check the spreadsheet ID in the private deployment settings.",
    "no_dashboard": "The workbook has no readable Dashboard!A1:S39 range.",
    "layout_changed": "Dashboard headers changed; inventory data was not loaded.",
    "unavailable": "Google Sheets did not respond. Try Refresh shortly.",
    "read_failed": "The sheet could not be read. Check sharing, credentials, and Dashboard layout.",
}


class SourceError(ValueError):
    def __init__(self, code, status=None):
        super().__init__(code)
        self.code = code
        self.status = status


def source_config():
    """Keep private spreadsheet IDs and service-account credentials out of Git."""
    sources = json.loads(os.environ["INVENTORY_SHEETS_JSON"])
    if not isinstance(sources, dict):
        raise ValueError("INVENTORY_SHEETS_JSON must be an object.")
    credentials = json.loads(os.environ["INVENTORY_SERVICE_ACCOUNT_JSON"])
    if not isinstance(credentials, dict) or credentials.get("type") != "service_account":
        raise ValueError("INVENTORY_SERVICE_ACCOUNT_JSON must contain a service account.")
    return sources, credentials


def _field(name):
    return re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()


def parse_dashboard(values):
    """Adapt the six current Dashboard layouts without inventing missing values."""
    def cell(row, col):
        return str(values[row][col]).strip() if row < len(values) and col < len(values[row]) else ""

    headers = {_field(value): index for index, value in enumerate(values[13] if len(values) > 13 else []) if value}

    def column(*names):
        for name in names:
            if _field(name) in headers:
                return headers[_field(name)]
        return None

    positions = {
        "product": column("Product"),
        "starting": column("Initial Stock", "Opening Stock"),
        "shipped": column("Today's Orders", "Sold on Date"),
        "remaining": column("Remaining Stocks", "Calculated EOD On Hand*"),
        "demand": column("Daily Demand", "Daily Demand (30d)"),
        "cover": column("Covered Days", "Days of Cover"),
        "status": column("Status", "Reorder Status"),
    }
    if positions["product"] is None or positions["remaining"] is None:
        raise SourceError("layout_changed")

    rows = []
    issues = 0
    for index in range(14, min(len(values), LAST_ROW)):
        product = cell(index, positions["product"])
        if not product or product.upper() == "TOTAL" or product.startswith("*") or "SUBTOTAL" in product.upper():
            continue
        row = {key: cell(index, pos) if pos is not None else "" for key, pos in positions.items()}
        if not row["status"] and len(values[index]) > 6 and cell(index, 6) == "OUT OF STOCK":
            row["status"] = "OUT OF STOCK"
        errors = {key for key, value in row.items() if ERROR_VALUE.search(value)}
        issues += bool(errors)
        row["flags"] = sorted(errors | ({"remaining"} if row["remaining"].startswith("-") else set()))
        rows.append(row)

    warnings = []
    if not cell(3, 1):
        warnings.append("Dashboard as-of date is blank")
    if not rows:
        warnings.append("No product rows found on the Dashboard")
    last = cell(LAST_ROW - 1, positions["product"])
    if last and last.upper() != "TOTAL" and "SUBTOTAL" not in last.upper() and not last.startswith("*"):
        warnings.append(f"Products may continue past row {LAST_ROW}; rows beyond it are not shown")
    if issues:
        warnings.append(f"{issues} row(s) contain pending or formula-error values")
    if any(row["remaining"].startswith("-") for row in rows):
        warnings.append("A displayed remaining balance is negative")
    if any(ERROR_VALUE.search(cell(6, col)) for col in (0, 2, 4, 6)):
        warnings.append("Dashboard summary contains pending or formula-error values")
    reorder_summary = cell(6, 2).replace(",", "")
    if reorder_summary.isdigit():
        marked = sum(row["status"].upper() == "REORDER NOW" for row in rows)
        if marked != int(reorder_summary):
            warnings.append("Reorder summary disagrees with product rows")

    return {
        "as_of": cell(3, 1),
        "report_status": cell(3, 4) or "SOURCE VALUES",
        "summary": {"products": cell(6, 0), "reorder": cell(6, 2),
                    "out": cell(6, 4), "on_hand": cell(6, 6)},
        "rows": rows,
        "issue_rows": issues,
        "warnings": warnings,
    }


def _read_one(client_id, sheet_id, credentials):
    with _cache_lock:
        cached = _cache.get((client_id, sheet_id))
        if cached and time.monotonic() - cached[0] < 45:
            return cached[1]
    creds = service_account.Credentials.from_service_account_info(credentials, scopes=SCOPES)
    session = AuthorizedSession(creds)
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/Dashboard%21A1%3AS{LAST_ROW}"
    try:
        response = session.get(url, params={"valueRenderOption": "FORMATTED_VALUE"}, timeout=15)
    except requests.RequestException:
        raise SourceError("unavailable")
    if response.status_code != 200:
        code = {400: "no_dashboard", 401: "access_denied", 403: "access_denied", 404: "not_found"}.get(
            response.status_code, "unavailable" if response.status_code in (429, 500, 502, 503, 504) else "read_failed")
        raise SourceError(code, response.status_code)
    result = parse_dashboard(response.json().get("values", []))
    result.update(id=client_id, source_url=f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
                  fetched_at=datetime.now(timezone.utc).isoformat())
    with _cache_lock:
        _cache[(client_id, sheet_id)] = (time.monotonic(), result)
    return result


def _failure(client_id, code, status=None):
    # Log the category and HTTP status only; request URLs carry the private spreadsheet ID.
    log.warning("inventory source failed client=%s code=%s status=%s", client_id, code, status)
    return {"id": client_id, "error_code": code, "error": ERRORS[code]}


def read_dashboards(client_ids):
    """Read each client independently so one broken mapping or workbook never hides the others."""
    sources, credentials = source_config()
    with ThreadPoolExecutor(max_workers=min(6, len(client_ids))) as pool:
        futures = {client_id: pool.submit(_read_one, client_id, sources[client_id], credentials)
                   for client_id in client_ids
                   if isinstance(sources.get(client_id), str) and SHEET_ID.fullmatch(sources[client_id])}
        result = []
        for client_id in client_ids:
            if client_id not in futures:
                result.append(_failure(client_id, "not_configured"))
                continue
            try:
                result.append(futures[client_id].result())
            except SourceError as error:
                result.append(_failure(client_id, error.code, error.status))
            except Exception as error:
                log.warning("inventory source failed client=%s type=%s", client_id, type(error).__name__)
                result.append({"id": client_id, "error_code": "read_failed", "error": ERRORS["read_failed"]})
        return result
