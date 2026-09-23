"""Read-only Google Sheets dashboard adapter. Sheet values are never recalculated here."""

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account


SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
_cache = {}
_cache_lock = threading.Lock()


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
        raise ValueError("Dashboard headers changed; inventory data was not loaded.")

    rows = []
    issues = 0
    for index in range(14, min(len(values), 39)):
        product = cell(index, positions["product"])
        if not product or product.upper() == "TOTAL" or product.startswith("*") or "SUBTOTAL" in product.upper():
            continue
        row = {key: cell(index, pos) if pos is not None else "" for key, pos in positions.items()}
        if not row["status"] and len(values[index]) > 6 and cell(index, 6) == "OUT OF STOCK":
            row["status"] = "OUT OF STOCK"
        if any(re.search(r"#(?:DIV/0!|VALUE!|REF!|N/A|NUM!)|PENDING", value, re.I) for value in row.values()):
            issues += 1
        rows.append(row)

    warnings = []
    if issues:
        warnings.append(f"{issues} row(s) contain pending or formula-error values")
    if any(row["remaining"].startswith("-") for row in rows):
        warnings.append("A displayed remaining balance is negative")
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
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/Dashboard%21A1%3AS39"
    response = session.get(url, params={"valueRenderOption": "FORMATTED_VALUE"}, timeout=15)
    response.raise_for_status()
    result = parse_dashboard(response.json().get("values", []))
    result.update(id=client_id, source_url=f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
                  fetched_at=datetime.now(timezone.utc).isoformat())
    with _cache_lock:
        _cache[(client_id, sheet_id)] = (time.monotonic(), result)
    return result


def read_dashboards(client_ids):
    sources, credentials = source_config()
    for client_id in client_ids:
        if not isinstance(sources.get(client_id), str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,}", sources[client_id]):
            raise ValueError(f"Inventory sheet mapping missing for {client_id}.")
    with ThreadPoolExecutor(max_workers=min(6, len(client_ids))) as pool:
        futures = {client_id: pool.submit(_read_one, client_id, sources[client_id], credentials) for client_id in client_ids}
        result = []
        for client_id, future in futures.items():
            try:
                result.append(future.result())
            except Exception:
                result.append({"id": client_id, "error": "The sheet could not be read. Check sharing, credentials, and Dashboard layout."})
        return result
