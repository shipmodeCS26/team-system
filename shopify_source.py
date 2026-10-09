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
ORDER_QUERY = """query ShipModeOrder($q: String!, $after: String) {
  orders(first: 25, query: $q, after: $after) {
    nodes {
      id name createdAt cancelledAt displayFinancialStatus displayFulfillmentStatus
      lineItems(first: 30) { nodes { name sku quantity currentQuantity requiresShipping } pageInfo { hasNextPage } }
    }
    pageInfo { hasNextPage endCursor }
  }
}"""
# Shopify refuses a query whose requested cost is over 1,000 points: 25 orders x 30 lines is about 850.
# 25 x 100 lines (about 2,600) was refused outright. Longer orders are marked items_truncated.
ORDER_SEARCH_PAGES = 4  # 100 fuzzy candidates; more is reported as an incomplete search, never a unique match
# Separate reads: protected customer fields and fulfillment data need extra access. If either is
# refused, the order itself still shows ("address unavailable" / shipment count unknown).
ORDER_ADDRESS_QUERY = """query ShipModeOrderAddress($id: ID!) {
  order(id: $id) { shippingAddress { name address1 address2 city provinceCode zip countryCodeV2 } }
}"""
ORDER_FULFILLMENTS_QUERY = """query ShipModeOrderFulfillments($id: ID!) {
  order(id: $id) { fulfillments { status requiresShipping trackingInfo(first: 10) { number } } }
}"""
# #14: one client's orders created in a time window, newest first. Order names, dates, statuses and
# line items only: no customer, address, price or note fields are requested.
DAY_ORDERS_QUERY = """query ShipModeDayOrders($q: String!, $after: String) {
  orders(first: 25, query: $q, after: $after, sortKey: CREATED_AT, reverse: true) {
    nodes {
      name createdAt cancelledAt test displayFinancialStatus displayFulfillmentStatus
      lineItems(first: 30) { nodes { name sku quantity currentQuantity requiresShipping } pageInfo { hasNextPage } }
    }
    pageInfo { hasNextPage endCursor }
  }
}"""
DAY_ORDER_PAGES = 80  # 2,000 orders across the window; the chosen day is read first (newest first)
READ_QUERIES = frozenset({SCOPES_QUERY, VARIANTS_QUERY, ORDER_QUERY, ORDER_ADDRESS_QUERY, ORDER_FULFILLMENTS_QUERY,
                          DAY_ORDERS_QUERY})
# Merchants can customise order prefixes/suffixes, so any printable name is allowed; it is escaped
# inside the quoted search term. Control characters are refused.
ORDER_NAME = re.compile(r"[^\x00-\x1f\x7f]{1,100}")

# Shown to signed-in staff. Messages never include shop domains or credentials.
ERRORS = {
    "not_configured": "No Shopify store is mapped for this client in the private deployment settings.",
    "access_denied": "Shopify refused the credentials. Check the client's ShipMode app is installed and the token is current.",
    "missing_scope": "The ShipMode app is missing read access it needs (read_products). Ask the client to add it.",
    "write_scope_granted": "Refused: this Shopify token can make changes. ShipMode only connects with a read-only app.",
    "not_found": "Shopify store not found. Check the store domain in the private deployment settings.",
    "unavailable": "Shopify did not respond. Try again shortly.",
    "read_failed": "The Shopify store could not be read.",
    "throttled": "Shopify asked us to slow down. Try again shortly.",
    "order_scope": "The ShipMode app cannot read orders yet (needs read_orders; addresses also need Shopify protected customer data access).",
}


class SourceError(ValueError):
    def __init__(self, code, status=None):
        super().__init__(code)
        self.code = code
        self.status = status


class InvalidOrderName(Exception):
    """The order name cannot be searched safely; nothing is sent to Shopify."""


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
        if "THROTTLED" in codes:
            raise SourceError("throttled")
        if "MAX_COST_EXCEEDED" in codes:
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


def _search_term(order_name):
    return 'name:"' + order_name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _optional(store, document, variables):
    """A read that may be refused for lack of extra access; the caller shows 'not available'."""
    try:
        return _graphql(store, document, variables, retry=False), None
    except SourceError as error:
        if error.code in ("missing_scope", "read_failed"):
            return None, error.code
        raise


def read_order(client_id, order_name):
    """One client's Shopify order by its exact name, read on demand (#13). Never cached: it carries
    the shipping address, which is returned to the signed-in browser and never stored or logged."""
    store = store_config().get(client_id)
    if not _valid_store(store):
        raise SourceError("not_configured")
    if not isinstance(order_name, str) or not ORDER_NAME.fullmatch(order_name.strip()):
        raise InvalidOrderName(order_name)
    order_name = order_name.strip()
    scopes = granted_scopes(store)
    check_scopes(scopes)  # refuses a token with write access before any order is read
    if "read_orders" not in scopes:
        raise SourceError("order_scope")
    nodes, after, search_complete = [], None, True
    for page in range(ORDER_SEARCH_PAGES):
        data = _graphql(store, ORDER_QUERY, {"q": _search_term(order_name), "after": after})
        connection = data.get("orders") or {}
        nodes += connection.get("nodes") or []
        info = connection.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        after = info.get("endCursor")
        if page == ORDER_SEARCH_PAGES - 1:
            search_complete = False
    matches = [node for node in nodes if str(node.get("name") or "").strip().lower() == order_name.lower()]
    # Without read_all_orders Shopify only searches the last 60 days: an older order with the same
    # (customised) name could be the real one, so the address is shown only after a full search.
    complete = search_complete and "read_all_orders" in scopes
    orders = []
    for node in matches:
        lines = node.get("lineItems") or {}
        orders.append({
            "id": node.get("id"),
            "name": node.get("name"), "created_at": node.get("createdAt"), "cancelled_at": node.get("cancelledAt"),
            "financial": node.get("displayFinancialStatus") or "", "fulfillment": node.get("displayFulfillmentStatus") or "",
            # currentQuantity drops refunded/removed units; a line where it differs from the original
            # quantity can't tell us what physically shipped, so it is marked "changed" (comparison unverified).
            "items": [{"name": (line.get("name") or "").strip(), "sku": (line.get("sku") or "").strip(),
                       "qty": line.get("currentQuantity") if isinstance(line.get("currentQuantity"), int) else line.get("quantity"),
                       "changed": isinstance(line.get("currentQuantity"), int) and line.get("currentQuantity") != line.get("quantity")}
                      # Gift cards, digital goods and tips never ship, so they can't be in a parcel.
                      for line in (lines.get("nodes") or []) if isinstance(line, dict) and line.get("requiresShipping") is not False],
            "items_truncated": bool((lines.get("pageInfo") or {}).get("hasNextPage")),
            "address": None, "address_visible": False, "address_withheld": False,
            "tracking_numbers": None, "fulfillment_count": None, "fulfillments_truncated": False,
        })
    # Extra reads only for a single confirmed match: never fan out over duplicate names.
    if len(orders) == 1 and search_complete:
        order = orders[0]
        found, _ = _optional(store, ORDER_FULFILLMENTS_QUERY, {"id": order["id"]})
        if found is not None:
            listed = [f for f in (((found.get("order") or {}).get("fulfillments")) or []) if isinstance(f, dict)]
            # Cancelled or failed attempts are not parcels; a replacement after one is still one shipment.
            # Digital-only fulfillments (requiresShipping false) are not parcels either.
            fulfillments = [f for f in listed if str(f.get("status") or "").upper() not in ("CANCELLED", "ERROR", "FAILURE")
                            and f.get("requiresShipping") is not False]
            numbers = {str(info.get("number")).strip() for f in fulfillments
                       for info in (f.get("trackingInfo") or []) if isinstance(info, dict) and info.get("number")}
            order["tracking_numbers"] = sorted(numbers)
            # Fulfillments without tracking still mean separate parcels; a full page (20) means maybe more.
            order["fulfillment_count"] = len(fulfillments)
            # Order.fulfillments takes no page argument in this API version, so the list is complete.
        if complete:
            # Address access is approved per field by Shopify (protected customer data), so ask;
            # a refusal simply leaves the address unavailable.
            found, _ = _optional(store, ORDER_ADDRESS_QUERY, {"id": order["id"]})
            address = ((found or {}).get("order") or {}).get("shippingAddress")
            if isinstance(address, dict):
                order["address"] = {key: address.get(key) for key in
                                    ("name", "address1", "address2", "city", "provinceCode", "zip", "countryCodeV2")}
            order["address_visible"] = found is not None
        else:
            order["address_withheld"] = True
    for order in orders:
        order.pop("id", None)
    return {"orders": orders, "complete": complete, "search_complete": search_complete}


def _window_term(start, end):
    """Shopify search for orders created in [start, end); both are timezone-aware datetimes."""
    fmt = lambda moment: moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"created_at:>='{fmt(start)}' created_at:<'{fmt(end)}'"


THROTTLE_WAITS = (2, 4, 8, 16)


def _paced(store, document, variables, sleep=time.sleep):
    """Long reads pause and retry when Shopify's rate limit is reached, instead of failing."""
    for wait in THROTTLE_WAITS:
        try:
            return _graphql(store, document, variables)
        except SourceError as error:
            if error.code != "throttled":
                raise
            sleep(wait)
    return _graphql(store, document, variables)


def read_day_orders(client_id, start, end, progress=None, sleep=time.sleep):
    """#14: one client's orders created in [start, end), read-only and without customer fields.
    Returns orders newest first, `complete` (the whole window was read) and `oldest_read`."""
    store = store_config().get(client_id)
    if not _valid_store(store):
        raise SourceError("not_configured")
    scopes = granted_scopes(store)
    check_scopes(scopes)  # refuses a token with write access before any order is read
    if "read_orders" not in scopes:
        raise SourceError("order_scope")
    orders, after, complete = [], None, False
    for _ in range(DAY_ORDER_PAGES):
        data = _paced(store, DAY_ORDERS_QUERY, {"q": _window_term(start, end), "after": after}, sleep)
        connection = data.get("orders") or {}
        for node in connection.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            lines = node.get("lineItems") or {}
            orders.append({
                "name": str(node.get("name") or "").strip(), "created_at": node.get("createdAt"),
                "cancelled_at": node.get("cancelledAt"), "test": node.get("test") is True,
                "financial": node.get("displayFinancialStatus") or "",
                "fulfillment": node.get("displayFulfillmentStatus") or "",
                # The ordered quantity is kept; a refund or edit (currentQuantity differs) is flagged,
                # never subtracted (#14 acceptance criteria).
                "items": [{"name": (line.get("name") or "").strip(), "sku": (line.get("sku") or "").strip(),
                           "qty": line.get("quantity"),
                           "changed": isinstance(line.get("currentQuantity"), int)
                           and line.get("currentQuantity") != line.get("quantity")}
                          for line in (lines.get("nodes") or [])
                          if isinstance(line, dict) and line.get("requiresShipping") is not False],
                "items_truncated": bool((lines.get("pageInfo") or {}).get("hasNextPage")),
            })
        if progress:
            progress(len(orders))
        info = connection.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            complete = True
            break
        after = info.get("endCursor")
    return {"orders": orders, "complete": complete, "all_orders": "read_all_orders" in scopes,
            "oldest_read": orders[-1]["created_at"] if orders else None,
            "fetched_at": datetime.now(timezone.utc).isoformat()}
