"""Read-only Shopify Admin API adapter (Issue #12).

ShipMode never changes anything in a client's Shopify store. This is enforced
three ways:

1. Every GraphQL request goes through `_graphql`, which only sends documents
   from the fixed `READ_QUERIES` allowlist and refuses any document that
   contains a mutation or subscription, before anything leaves the server.
2. Before any data is read, the token's granted scopes are checked. A token
   holding any write scope is refused (`write_scope_granted`), so a store can
   only be connected with a read-only app.
3. The ShipMode app itself is created with read scopes only (docs/SHOPIFY_SETUP.md);
   Shopify rejects anything the token is not allowed to do.

The only other request is the OAuth client-credentials exchange, which
fetches a short-lived read token and changes nothing in the store.
Logs and responses never contain shop domains or credentials.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

API_VERSION = "2026-07"
SHOP_DOMAIN = re.compile(r"[a-z0-9][a-z0-9-]{0,98}\.myshopify\.com")
WRITE_OPERATION = re.compile(r"\b(mutation|subscription)\b", re.I)
PAGE_SIZE = 100
MAX_PAGES = 50  # 5,000 variants per store; more is reported as a warning, never silently dropped.
CACHE_SECONDS = 300  # catalogs change rarely; the Inventory tab refreshes every minute
REQUIRED_SCOPES = ("read_products",)
# Needed by the next Shopify Issues (#13, #14). Missing ones are warnings here, not failures.
PLANNED_SCOPES = ("read_products", "read_inventory", "read_orders", "read_customers")

log = logging.getLogger(__name__)
_cache = {}
_tokens = {}
_lock = threading.Lock()

SCOPES_QUERY = "query ShipModeScopes { currentAppInstallation { accessScopes { handle } } }"
VARIANTS_QUERY = """query ShipModeVariants($after: String) {
  productVariants(first: %d, after: $after) {
    nodes { title sku requiresComponents product { title status } inventoryItem { tracked } }
    pageInfo { hasNextPage endCursor }
  }
}""" % PAGE_SIZE
READ_QUERIES = frozenset({SCOPES_QUERY, VARIANTS_QUERY})

# Shown to signed-in staff. Messages never include shop domains or credentials.
ERRORS = {
    "not_configured": "No Shopify store is mapped for this client in the private deployment settings.",
    "access_denied": "Shopify refused the credentials. Check the client's ShipMode app is installed and the token is current.",
    "missing_scope": "The ShipMode app is missing read access it needs (read_products). Ask the client to add it.",
    "write_scope_granted": "Refused: this Shopify token can make changes. ShipMode only connects with a read-only app.",
    "not_found": "Shopify store not found. Check the store domain in the private deployment settings.",
    "unavailable": "Shopify did not respond. Try again shortly.",
    "read_failed": "The Shopify store could not be read.",
}


class SourceError(ValueError):
    def __init__(self, code, status=None):
        super().__init__(code)
        self.code = code
        self.status = status


class ReadOnlyViolation(RuntimeError):
    """Raised before sending any Shopify request that is not an allowlisted read."""


def enabled():
    return os.getenv("SHOPIFY_ENABLED", "false").lower() == "true"


def store_config():
    """Private store domains and credentials come from the environment, never Git."""
    stores = json.loads(os.environ["SHOPIFY_STORES_JSON"])
    if not isinstance(stores, dict):
        raise ValueError("SHOPIFY_STORES_JSON must be an object.")
    return stores


def _valid_store(store):
    if not isinstance(store, dict) or not isinstance(store.get("shop"), str):
        return False
    if not SHOP_DOMAIN.fullmatch(store["shop"]):
        return False
    token = isinstance(store.get("token"), str) and store["token"]
    client = all(isinstance(store.get(k), str) and store[k] for k in ("client_id", "client_secret"))
    return bool(token or client)


def _store_key(client_id, store):
    raw = json.dumps([client_id, store.get("shop"), store.get("token"), store.get("client_id")])
    return hashlib.sha256(raw.encode()).hexdigest()


def _status_error(status):
    if status in (401, 423):
        return "access_denied"
    if status == 403:
        return "missing_scope"
    if status == 404:
        return "not_found"
    if status in (402, 429, 500, 502, 503, 504):
        return "unavailable"
    return "read_failed"


def _access_token(store):
    """A fixed admin token, or a short-lived token from the client-credentials grant."""
    if store.get("token"):
        return store["token"]
    key = (store["shop"], store["client_id"])
    with _lock:
        cached = _tokens.get(key)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
    try:
        response = requests.post(f"https://{store['shop']}/admin/oauth/access_token",
                                 data={"grant_type": "client_credentials", "client_id": store["client_id"],
                                       "client_secret": store["client_secret"]}, timeout=15)
    except requests.RequestException:
        raise SourceError("unavailable")
    if response.status_code != 200:
        raise SourceError("access_denied" if response.status_code in (400, 401, 403) else _status_error(response.status_code),
                          response.status_code)
    body = response.json()
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        raise SourceError("access_denied")
    lifetime = body.get("expires_in") if isinstance(body.get("expires_in"), int) else 3600
    with _lock:
        _tokens[key] = (time.monotonic() + max(lifetime - 300, 60), token)
    return token


def _graphql(store, document, variables=None, retry=True):
    """The single path to the Shopify Admin API. Reads only."""
    if document not in READ_QUERIES or WRITE_OPERATION.search(document):
        raise ReadOnlyViolation("Only allowlisted Shopify read queries may be sent.")
    try:
        return _graphql_once(store, document, variables)
    except SourceError as error:
        # A client-credentials token can be revoked before it expires (e.g. after the client fixes
        # scopes and reinstalls). Drop the cached token and exchange again, once.
        if retry and error.code in ("access_denied", "missing_scope") and not store.get("token"):
            # Retry even if a concurrent request already evicted it: the next call exchanges or
            # reuses the fresh token.
            with _lock:
                _tokens.pop((store["shop"], store["client_id"]), None)
            return _graphql(store, document, variables, retry=False)
        raise


def _graphql_once(store, document, variables):
    if document not in READ_QUERIES or WRITE_OPERATION.search(document):
        raise ReadOnlyViolation("Only allowlisted Shopify read queries may be sent.")
    try:
        response = requests.post(f"https://{store['shop']}/admin/api/{API_VERSION}/graphql.json",
                                 json={"query": document, "variables": variables or {}},
                                 headers={"X-Shopify-Access-Token": _access_token(store)}, timeout=15)
    except requests.RequestException:
        raise SourceError("unavailable")
    if response.status_code != 200:
        raise SourceError(_status_error(response.status_code), response.status_code)
    body = response.json()
    errors = body.get("errors") or []
    if errors:
        codes = {str((e.get("extensions") or {}).get("code", "")).upper() for e in errors if isinstance(e, dict)}
        if "ACCESS_DENIED" in codes:
            raise SourceError("missing_scope")
        if "THROTTLED" in codes or "MAX_COST_EXCEEDED" in codes:
            raise SourceError("unavailable")
        raise SourceError("read_failed")
    return body.get("data") or {}


def granted_scopes(store):
    data = _graphql(store, SCOPES_QUERY)
    scopes = ((data.get("currentAppInstallation") or {}).get("accessScopes")) or []
    return sorted({s.get("handle", "") for s in scopes if isinstance(s, dict)})


def check_scopes(scopes):
    """Refuse write access outright; report missing read access."""
    if any("write" in scope for scope in scopes):
        raise SourceError("write_scope_granted")
    if any(scope not in scopes for scope in REQUIRED_SCOPES):
        raise SourceError("missing_scope")
    return [scope for scope in PLANNED_SCOPES if scope not in scopes]


def read_variants(store):
    variants, after, truncated = [], None, False
    for page in range(MAX_PAGES):
        data = _graphql(store, VARIANTS_QUERY, {"after": after})
        connection = data.get("productVariants") or {}
        for node in connection.get("nodes") or []:
            product = node.get("product") or {}
            variants.append({
                "product": (product.get("title") or "").strip(),
                "variant": (node.get("title") or "").strip(),
                "sku": (node.get("sku") or "").strip(),
                "product_status": (product.get("status") or "").upper(),
                "tracked": bool((node.get("inventoryItem") or {}).get("tracked")),
                "bundle": node.get("requiresComponents") is True,
            })
        info = connection.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        after = info.get("endCursor")
        if page == MAX_PAGES - 1:
            truncated = True
    return variants, truncated


def read_catalog(client_id, store):
    """One client's catalog. Cached briefly; failures are never cached."""
    key = _store_key(client_id, store)
    with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
    missing = check_scopes(granted_scopes(store))
    variants, truncated = read_variants(store)
    result = {"id": client_id, "variants": variants, "truncated": truncated, "missing_scopes": missing,
              "fetched_at": datetime.now(timezone.utc).isoformat()}
    with _lock:
        _cache[key] = (time.monotonic(), result)
    return result


def failure(client_id, code, status=None):
    # Category and HTTP status only: URLs and headers carry the private shop domain and token.
    log.warning("shopify source failed client=%s code=%s status=%s", client_id, code, status)
    return {"id": client_id, "error_code": code, "error": ERRORS[code]}


def read_catalogs(client_ids):
    """Read each client's store independently so one failure never hides another client."""
    stores = store_config()
    with ThreadPoolExecutor(max_workers=max(1, min(6, len(client_ids)))) as pool:
        futures = {client_id: pool.submit(read_catalog, client_id, stores[client_id])
                   for client_id in client_ids if _valid_store(stores.get(client_id))}
        result = []
        for client_id in client_ids:
            if client_id not in futures:
                result.append(failure(client_id, "not_configured"))
                continue
            try:
                result.append(futures[client_id].result())
            except SourceError as error:
                result.append(failure(client_id, error.code, error.status))
            except Exception as error:
                log.warning("shopify source failed client=%s type=%s", client_id, type(error).__name__)
                result.append({"id": client_id, "error_code": "read_failed", "error": ERRORS["read_failed"]})
        return result
