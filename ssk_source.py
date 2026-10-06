"""Read-only ShipSidekick API adapter (Issue #16).

ShipSidekick's API can also create orders, move inventory, create shipments
and archive products. ShipMode never does any of that: `_get` is the only path
to the API, it only issues GET requests, only to the fixed `READ_PATHS`, and
only to ShipSidekick's own production or test host. Anything else raises
`ReadOnlyViolation` before a request is sent.

One API key per store, from private settings `SSK_API_KEY_<CLIENT>`. Keys never
appear in logs or responses.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

PRODUCTION = "https://www.shipsidekick.com/api/v1"
TEST = "https://test.shipsidekick.com/api/v1"
READ_PATHS = frozenset({"/inventory/levels", "/products", "/orders", "/shipments"})
PAGE_SIZE = 100
MAX_PAGES = 50  # 5,000 rows per store; more is reported as a warning, never silently dropped.
CACHE_SECONDS = 55  # stock moves all day; the Inventory tab refreshes every minute

log = logging.getLogger(__name__)
_cache = {}
_lock = threading.Lock()

# Shown to signed-in staff. Messages never include keys.
ERRORS = {
    "not_configured": "No ShipSidekick API key is set for this store in the private deployment settings.",
    "access_denied": "ShipSidekick refused the API key for this store. Check the key is current.",
    "unavailable": "ShipSidekick did not respond. Try again shortly.",
    "read_failed": "The ShipSidekick data could not be read.",
    "filter_ignored": "ShipSidekick did not apply the tracking-status filter, so the queue was not loaded (it would be incomplete).",
}
QUANTITIES = ("available", "committed", "incoming", "reserved", "damaged", "quality_control", "safety_stock")


class SourceError(ValueError):
    def __init__(self, code, status=None):
        super().__init__(code)
        self.code = code
        self.status = status


class ReadOnlyViolation(RuntimeError):
    """Raised before sending any ShipSidekick request that is not an allowlisted GET."""


def enabled():
    return os.getenv("SSK_API_ENABLED", "false").lower() == "true"


def base_url():
    # Only ShipSidekick's own hosts, so a misconfigured setting can never send a key elsewhere.
    return TEST if os.getenv("SSK_API_BASE", "").rstrip("/") == TEST else PRODUCTION


def api_key(client_id):
    return os.getenv("SSK_API_KEY_" + client_id.upper().replace("-", "_"), "").strip()


def _status_error(status):
    if status in (401, 403):
        return "access_denied"
    if status in (429, 500, 502, 503, 504):
        return "unavailable"
    return "read_failed"


def _get(key, path, params=None, method="GET"):
    """The single path to the ShipSidekick API. Reads only."""
    if method != "GET" or path not in READ_PATHS:
        raise ReadOnlyViolation("Only allowlisted ShipSidekick GET requests may be sent.")
    try:
        response = requests.get(base_url() + path, params=params or {}, timeout=20,
                                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    except requests.RequestException:
        raise SourceError("unavailable")
    if response.status_code != 200:
        raise SourceError(_status_error(response.status_code), response.status_code)
    body = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise SourceError("read_failed")
    return body


def get_all(key, path, params=None):
    rows, truncated = [], False
    for page in range(1, MAX_PAGES + 1):
        body = _get(key, path, {**(params or {}), "limit": PAGE_SIZE, "page": page})
        rows.extend(body["data"])
        # Prefer ShipSidekick's own hasMore; fall back to "short page = last page" if it is absent.
        more = body["hasMore"] if isinstance(body.get("hasMore"), bool) else len(body["data"]) == PAGE_SIZE
        if not more:
            break
        if page == MAX_PAGES:
            truncated = True
    return rows, truncated


def _qty(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def inventory_levels(key):
    """Levels summed per variant SKU across warehouses. Only SKU, title and quantities are kept."""
    rows, truncated = get_all(key, "/inventory/levels")
    variants = {}
    for row in rows:
        variant = row.get("productVariant") or {}
        sku = str(variant.get("sku") or "").strip()
        title = str(variant.get("title") or "").strip()
        product = variant.get("product") if isinstance(variant.get("product"), dict) else {}
        name = str(product.get("name") or product.get("title") or "").strip()
        # One variant can sit in several warehouses (summed); two variants sharing a SKU stay separate.
        key_ = str(variant.get("id") or "") or f"sku:{sku.upper() or title}"
        bundle = any(isinstance(d, dict) and d.get("isBundle") is True for d in (product, variant, row))
        item = variants.setdefault(key_, {
            "sku": sku, "title": title, "product": name, "bundle": bundle,
            "aliases": sorted({str(a).strip() for a in variant.get("skuAliases") or [] if str(a).strip()}),
            **{q: 0 for q in QUANTITIES}, "locations": 0})
        item["available"] += _qty(row.get("availableQuantity"))
        item["committed"] += _qty(row.get("committedQuantity"))
        item["incoming"] += _qty(row.get("incomingQuantity"))
        item["reserved"] += _qty(row.get("reservedQuantity"))
        item["damaged"] += _qty(row.get("damagedQuantity"))
        item["quality_control"] += _qty(row.get("qualityControlQuantity"))
        item["safety_stock"] += _qty(row.get("safetyStockQuantity"))
        item["locations"] += 1
    return list(variants.values()), truncated


# Shipment field discovery (Issue #17). The public docs truncate the Shipment type, so staging reports
# the field names it actually receives. Only names, value types and short status/carrier words leave
# this function: no addresses, names, tracking numbers, IDs or free text.
ENUM_KEY = re.compile(r"(?:status|carriercode|eventtype|source|service)$", re.I)
PRIVATE_KEY = re.compile(r"address|name|company|street|city|postal|zip|phone|email|tracking(?:code|number|url)|"
                         r"description|message|note|label(?:url|data)|url|id$", re.I)
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2})?")
SAFE_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_ -]{0,39}")


def _kind(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "date" if ISO_DATE.match(value) else "string"
    return "object" if isinstance(value, dict) else "list"


def _walk(value, path, fields, values):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            kinds = fields.setdefault(child_path, set())
            kinds.add(_kind(child))
            if re.search(r"address", str(key), re.I):
                continue  # never descend into an address
            if isinstance(child, str) and ENUM_KEY.search(str(key)) and not PRIVATE_KEY.search(str(key)) \
                    and SAFE_WORD.fullmatch(child):
                values.setdefault(child_path, set()).add(child)
            _walk(child, child_path, fields, values)
    elif isinstance(value, list):
        for item in value[:20]:
            _walk(item, path + "[]", fields, values)


def shipment_fields(key, sample=25):
    """Field names and status words from the most recent shipments. Read-only, nothing kept."""
    body = _get(key, "/shipments", {"limit": sample, "page": 1, "sortOrder": "desc"})
    fields, values = {}, {}
    for shipment in body["data"]:
        _walk(shipment, "", fields, values)
    return {"sampled": len(body["data"]),
            "fields": {path: sorted(kinds) for path, kinds in sorted(fields.items())},
            "values": {path: sorted(words)[:30] for path, words in sorted(values.items())}}


# Every tracking status except delivered/cancelled, spelled the way ShipSidekick returns them ("pre-transit").
# Reading only these keeps the request count small: delivered shipments are most of a store's volume.
QUEUE_STATUSES = ("pre-transit", "in-transit", "out-for-delivery", "available-for-pickup",
                  "return-to-sender", "failure", "unknown", "error")
_shipment_cache = {}


def lookback_days():
    value = os.getenv("SSK_SHIPMENT_DAYS", "30").strip()
    return min(max(int(value), 1), 90) if value.isdigit() else 30


def _norm(value):
    return str(value or "unknown").strip().lower().replace("_", "-")


def open_shipments(key, days):
    """Shipments created in the last `days` days that are not delivered. GET only."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    found, truncated, counts, rejected = {}, False, {}, []
    for state in QUEUE_STATUSES:
        try:
            rows, cut = get_all(key, "/shipments", {"trackingStatus": state, "dateRange[from]": since})
        except SourceError as error:
            if error.status != 400:
                raise
            # ShipSidekick does not accept this status word; the others still load, and the store is
            # reported as incomplete (never as full coverage).
            counts[state] = "rejected"
            rejected.append(state)
            continue
        # If ShipSidekick ignored the filter, other statuses would come back; refuse rather than show a partial queue.
        if any(_norm((row.get("tracker") or {}).get("status") if isinstance(row, dict) else None) != state
               for row in rows):
            raise SourceError("filter_ignored")
        counts[state] = len(rows)
        truncated = truncated or cut
        for row in rows:
            found[str(row.get("id") or len(found))] = row
    if all(value == "rejected" for value in counts.values()):
        raise SourceError("filter_ignored")
    return list(found.values()), truncated, counts, rejected


def read_shipments(client_id, days):
    from ssk_shipments import to_row
    key = api_key(client_id)
    if not key:
        raise SourceError("not_configured")
    cache_key = (client_id, days, hashlib.sha256(key.encode()).hexdigest())
    with _lock:
        cached = _shipment_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
    raw, truncated, counts, rejected = open_shipments(key, days)
    rows = [row for row in (to_row(s, client_id) for s in raw) if row]
    log.info("ssk shipments client=%s days=%s counts=%s", client_id, days, counts)
    result = {"id": client_id, "rows": rows, "truncated": truncated, "skipped_statuses": rejected,
              "environment": "test" if base_url() == TEST else "production",
              "fetched_at": datetime.now(timezone.utc).isoformat()}
    with _lock:
        _shipment_cache[cache_key] = (time.monotonic(), result)
    return result


def read_shipment_stores(client_ids, days):
    """Each store with its own key; one failure never hides another store. Missing keys are not logged."""
    with ThreadPoolExecutor(max_workers=max(1, min(6, len(client_ids)))) as pool:
        futures = {cid: pool.submit(read_shipments, cid, days) for cid in client_ids}
        out = []
        for cid in client_ids:
            try:
                out.append(futures[cid].result())
            except SourceError as error:
                if error.code == "not_configured":
                    out.append({"id": cid, "error_code": "not_configured", "error": ERRORS["not_configured"]})
                else:
                    out.append(failure(cid, error.code, error.status))
            except Exception as error:
                log.warning("ssk shipments failed client=%s type=%s", cid, type(error).__name__)
                out.append({"id": cid, "error_code": "read_failed", "error": ERRORS["read_failed"]})
        return out


def read_store(client_id):
    key = api_key(client_id)
    if not key:
        raise SourceError("not_configured")
    cache_key = (client_id, hashlib.sha256(key.encode()).hexdigest())
    with _lock:
        cached = _cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
    levels, truncated = inventory_levels(key)
    result = {"id": client_id, "levels": levels, "truncated": truncated,
              "environment": "test" if base_url() == TEST else "production",
              "fetched_at": datetime.now(timezone.utc).isoformat()}
    with _lock:
        _cache[cache_key] = (time.monotonic(), result)
    return result


def failure(client_id, code, status=None):
    log.warning("ssk source failed client=%s code=%s status=%s", client_id, code, status)
    return {"id": client_id, "error_code": code, "error": ERRORS[code]}


def read_stores(client_ids):
    """Each store is read with its own key; one failure never hides another store."""
    with ThreadPoolExecutor(max_workers=max(1, min(6, len(client_ids)))) as pool:
        futures = {cid: pool.submit(read_store, cid) for cid in client_ids}
        out = []
        for cid in client_ids:
            try:
                out.append(futures[cid].result())
            except SourceError as error:
                out.append(failure(cid, error.code, error.status))
            except Exception as error:
                log.warning("ssk source failed client=%s type=%s", cid, type(error).__name__)
                out.append({"id": cid, "error_code": "read_failed", "error": ERRORS["read_failed"]})
        return out
