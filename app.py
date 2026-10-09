import csv
import gzip
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import io
import json
import os
import secrets
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, render_template, request, session
from werkzeug.security import check_password_hash

from tracking import CLIENTS, classify, parse_csv, parse_date, sample_shipments, tracker_update, utcnow
from daily_update import build_update
from incoming import read_incoming
from inventory import SHEET_ID, read_dashboards
from ledger_sources import calculate_clients
import shopify_source
import sku_check
import client_rules
import daily_orders
import order_check
import ssk_check
import ssk_source

app = Flask(__name__)
app.config.update(SECRET_KEY=os.getenv("SECRET_KEY", secrets.token_hex(32)),
                  SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Strict", MAX_CONTENT_LENGTH=5 * 1024 * 1024)


def live():
    return os.getenv("APP_MODE", "demo") == "live"


def inventory_enabled():
    return os.getenv("INVENTORY_SHEETS_ENABLED", "false").lower() == "true"


def ledger_enabled():
    return inventory_enabled() and os.getenv("INVENTORY_LEDGER_ENABLED", "false").lower() == "true"


def ready():
    return all(os.getenv(k) for k in ("DATABASE_URL", "WORKSPACE_USER", "WORKSPACE_PASSWORD_HASH", "SECRET_KEY"))


def inventory_ready():
    return all(os.getenv(k) for k in ("INVENTORY_SHEETS_JSON", "INVENTORY_SERVICE_ACCOUNT_JSON", "WORKSPACE_USER", "WORKSPACE_PASSWORD_HASH", "SECRET_KEY"))


def shopify_order_clients():
    """Clients whose Shopify store is mapped, so the order lookup is only offered where it can work."""
    if not shopify_source.enabled():
        return []
    try:
        stores = shopify_source.store_config()
    except (ValueError, KeyError, json.JSONDecodeError):
        return []
    return [client["id"] for client in CLIENTS if shopify_source._valid_store(stores.get(client["id"]))]


def inventory_sheet_id(value):
    return bool(SHEET_ID.fullmatch(value))


def daily_order_clients():
    """Clients the daily Shopify comparison can run for: a mapped store, SKU rules and a mapped
    workbook (Daily Sales and Dashboard)."""
    if not inventory_enabled():
        return []
    try:
        sheets = json.loads(os.environ["INVENTORY_SHEETS_JSON"])
    except (KeyError, json.JSONDecodeError):
        return []
    if not isinstance(sheets, dict):
        return []
    return [client_id for client_id in shopify_order_clients() if client_rules.package(client_id)
            and isinstance(sheets.get(client_id), str) and inventory_sheet_id(sheets[client_id])]


def shopify_ready():
    return all(os.getenv(k) for k in ("SHOPIFY_STORES_JSON", "WORKSPACE_USER", "WORKSPACE_PASSWORD_HASH", "SECRET_KEY"))


def ssk_ready():
    return all(os.getenv(k) for k in ("WORKSPACE_USER", "WORKSPACE_PASSWORD_HASH", "SECRET_KEY"))


def db():
    import psycopg
    return psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=10)


@app.cli.command("init-db")
def init_db():
    """Run once against the configured persistent PostgreSQL database."""
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS shipments (
            id BIGSERIAL PRIMARY KEY, client_id TEXT NOT NULL,
            carrier TEXT NOT NULL, tracking_number TEXT NOT NULL,
            record JSONB NOT NULL, UNIQUE(client_id, carrier, tracking_number))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS tracking_events (
            client_id TEXT NOT NULL, event_id TEXT NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY(client_id,event_id))""")
    print("Workspace tables ready.")


def protected(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if live() or inventory_enabled() or shopify_source.enabled() or ssk_source.enabled():
            if live() and not ready():
                return jsonify(error="Live workspace is not configured. No shipment data is exposed."), 503
            if inventory_enabled() and not inventory_ready():
                return jsonify(error="Inventory access is not configured."), 503
            if shopify_source.enabled() and not shopify_ready():
                return jsonify(error="Shopify access is not configured."), 503
            if ssk_source.enabled() and not ssk_ready():
                return jsonify(error="ShipSidekick access is not configured."), 503
            auth = request.authorization
            if not auth or auth.username != os.getenv("WORKSPACE_USER") or not check_password_hash(os.getenv("WORKSPACE_PASSWORD_HASH", ""), auth.password or ""):
                return Response("Sign in to your Shipmode workspace.", 401, {"WWW-Authenticate": 'Basic realm="Shipmode workspace", charset="UTF-8"'})
        return fn(*args, **kwargs)
    return wrapper


def writable(fn):
    @wraps(fn)
    @protected
    def wrapper(*args, **kwargs):
        if not live():
            return jsonify(error="Sample workspace is read-only. Connect private storage and sign-in before adding real shipment data."), 409
        expected = session.get("csrf", "")
        if not expected or not hmac.compare_digest(expected, request.headers.get("X-CSRF-Token", "")):
            return jsonify(error="Refresh the workspace and try again."), 403
        return fn(*args, **kwargs)
    return wrapper


@app.after_request
def secure(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    return compress(response)


def compress(response):
    """Gzip large JSON responses (the ShipSidekick queue can be megabytes); the browser unpacks them."""
    if (response.mimetype != "application/json" or response.direct_passthrough
            or request.accept_encodings["gzip"] <= 0  # honours q-values, e.g. "gzip;q=0" means no
            or "Content-Encoding" in response.headers):
        return response
    body = response.get_data()
    if len(body) < 20_000:
        return response
    response.set_data(gzip.compress(body, compresslevel=5))
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Vary"] = "Accept-Encoding"
    return response


@app.get("/")
@protected
def home():
    session.setdefault("csrf", secrets.token_hex(24))
    return render_template("workspace.html", csrf=session["csrf"])


@app.get("/api/health")
def health():
    return {"message": "Success! Your application is online.", "version": "workspace-1"}


@app.get("/api/workspace")
@protected
def workspace():
    if live():
        with db() as conn:
            records = [dict(record, id=identity) for identity, record in conn.execute("SELECT id,record FROM shipments ORDER BY id").fetchall()]
    elif ssk_source.enabled():
        # Issue #17: real shipments pulled read-only from each store's ShipSidekick account; no database.
        days = ssk_source.lookback_days()
        stores = ssk_source.queue_snapshot([client["id"] for client in CLIENTS], days)
        records, sources = [], []
        for store in stores:
            if store.get("loading"):
                sources.append({"client_id": store["id"], "loading": True})
                continue
            if "error_code" in store:
                sources.append({"client_id": store["id"], "error_code": store["error_code"], "error": store["error"]})
                continue
            records.extend(store["rows"])
            sources.append({"client_id": store["id"], "shipments": len(store["rows"]),
                            "truncated": store["truncated"], "skipped_statuses": store["skipped_statuses"],
                            "environment": store["environment"], "fetched_at": store["fetched_at"]})
        return {"mode": "ssk", "clients": CLIENTS, "sources": sources, "lookback_days": days,
                "loading": any(src.get("loading") for src in sources),
                "shipments": [classify(dict(row, id=index + 1)) for index, row in enumerate(records)],
                "as_of": utcnow().isoformat(), "writes": "disabled", "shopify_orders": shopify_order_clients(),
                "daily_orders": daily_order_clients(),
                "integration": "ShipSidekick API (read-only)"}
    else:
        records = sample_shipments()
    return {"mode": "live" if live() else "demo", "clients": CLIENTS,
            "shipments": [classify(row) for row in records], "as_of": utcnow().isoformat(),
            "integration": "Awaiting verified ShipSidekick connection", "shopify_orders": shopify_order_clients(),
            "daily_orders": daily_order_clients()}


@app.get("/api/inventory")
@protected
def inventory():
    if not inventory_enabled():
        return jsonify(error="Google Sheets inventory is not connected."), 503
    selected = request.args.get("client_id", "all")
    client_ids = [client["id"] for client in CLIENTS]
    if selected != "all" and selected not in client_ids:
        return jsonify(error="Unknown client."), 400
    try:
        return {"sources": read_dashboards(client_ids if selected == "all" else [selected]),
                "as_of": utcnow().isoformat()}
    except (ValueError, KeyError, json.JSONDecodeError):
        return jsonify(error="Inventory configuration is invalid or incomplete."), 503


@app.get("/api/incoming")
@protected
def incoming():
    """Read-only incoming shipments for one client. Incoming units are never added to on-hand."""
    if not inventory_enabled():
        return jsonify(error="Google Sheets inventory is not connected."), 503
    selected = request.args.get("client_id", "")
    if selected not in {client["id"] for client in CLIENTS}:
        return jsonify(error="Choose one client."), 400
    try:
        source = read_incoming([selected], datetime.now(ZoneInfo("America/New_York")).date())[0]
    except (ValueError, KeyError, json.JSONDecodeError):
        return jsonify(error="Inventory configuration is invalid or incomplete."), 503
    return {"source": source, "as_of": utcnow().isoformat()}


@app.get("/api/daily-update")
@protected
def daily_update():
    """Draft text for one client's Slack update. Nothing is sent; staff review and copy it."""
    if not inventory_enabled():
        return jsonify(error="Google Sheets inventory is not connected."), 503
    selected = request.args.get("client_id", "")
    names = {client["id"]: client["name"] for client in CLIENTS}
    if selected not in names:
        return jsonify(error="Choose one client."), 400
    try:
        source = read_dashboards([selected])[0]
        if source.get("error"):
            return jsonify(error=f"{names[selected]} inventory did not load: {source['error']}"), 409
        extra = read_incoming([selected], datetime.now(ZoneInfo("America/New_York")).date())[0]
    except (ValueError, KeyError, json.JSONDecodeError):
        return jsonify(error="Inventory configuration is invalid or incomplete."), 503
    return {**build_update(names[selected], source, None if extra.get("error") else extra),
            "incoming_error": extra.get("error") or (None if extra.get("available") else
                                                      "This client's workbook has no Incoming Stocks tab."),
            "sheet_read_at": source.get("fetched_at")}


@app.get("/api/inventory/calculated")
@protected
def calculated_inventory():
    """Read-only calculated balances (ledger shadow check). Never writes anywhere."""
    if not ledger_enabled():
        return jsonify(error="Calculated inventory is switched off."), 503
    selected = request.args.get("client_id", "all")
    client_ids = [client["id"] for client in CLIENTS]
    if selected != "all" and selected not in client_ids:
        return jsonify(error="Unknown client."), 400
    try:
        return {"clients": calculate_clients(client_ids if selected == "all" else [selected]),
                "as_of": utcnow().isoformat(), "writes": "disabled"}
    except (ValueError, KeyError, json.JSONDecodeError):
        return jsonify(error="Inventory configuration is invalid or incomplete."), 503


@app.get("/api/shopify/sku-check")
@protected
def shopify_sku_check():
    """Read-only Shopify catalog vs. each client's own SKU rules. Never writes to Shopify."""
    if not shopify_source.enabled():
        return jsonify(error="Shopify is not connected."), 503
    selected = request.args.get("client_id", "all")
    client_ids = [client["id"] for client in CLIENTS]
    if selected != "all" and selected not in client_ids:
        return jsonify(error="Unknown client."), 400
    try:
        catalogs = shopify_source.read_catalogs(client_ids if selected == "all" else [selected])
    except (ValueError, KeyError, json.JSONDecodeError):
        return jsonify(error="Shopify configuration is invalid or incomplete."), 503
    clients = []
    for catalog in catalogs:
        if "error_code" in catalog:
            clients.append({"client_id": catalog["id"], "error_code": catalog["error_code"], "error": catalog["error"]})
            continue
        result = sku_check.check(catalog["id"], catalog["variants"])
        warnings = []
        if catalog["truncated"]:
            warnings.append(f"Store has more than {shopify_source.PAGE_SIZE * shopify_source.MAX_PAGES:,} variants; the rest were not checked.")
        if catalog["missing_scopes"]:
            warnings.append("Not granted yet (needed for later order checks): " + ", ".join(catalog["missing_scopes"]))
        clients.append(dict(result, warnings=warnings, truncated=catalog["truncated"], fetched_at=catalog["fetched_at"]))
    return {"clients": clients, "as_of": utcnow().isoformat(), "writes": "disabled"}


@app.get("/api/shopify/order")
@protected
def shopify_order():
    """#13: the Shopify order behind one ShipSidekick shipment, read on demand. Read-only.
    The shipping address goes only to this signed-in response (no-store); it is never cached,
    logged or exported."""
    if not shopify_source.enabled():
        return jsonify(error="Shopify is not connected."), 503
    if not ssk_source.enabled():
        return jsonify(error="Needs the ShipSidekick shipment queue."), 503
    client_id, shipment_id = request.args.get("client_id", ""), request.args.get("shipment", "")
    if client_id not in [client["id"] for client in CLIENTS] or not shipment_id or len(shipment_id) > 64:
        return jsonify(error="Choose one shipment."), 400
    try:
        store = ssk_source.read_shipments(client_id, ssk_source.lookback_days())
    except ssk_source.SourceError as error:
        return jsonify(error=ssk_source.ERRORS.get(error.code, ssk_source.ERRORS["read_failed"])), 502
    # ShipSidekick's own shipment id: unique, unlike a tracking number reused across carriers.
    matches = [row for row in store["rows"] if row.get("ssk_id") == shipment_id]
    if len(matches) != 1:
        return jsonify(error="That shipment is not (uniquely) in this client's current queue."), 404
    shipment = matches[0]
    name = shipment.get("order_number")
    if not name:
        return {"order": None, **order_check.check(client_id, shipment, [], unlinked=True), "writes": "disabled"}
    # A voided label (shown as cancelled) is not a parcel: its replacement is the only shipment.
    key = shopify_source._name_key(name)
    same_order = sum(shopify_source._name_key(row.get("order_number")) == key
                     and row.get("carrier_status") != "cancelled" for row in store["rows"])
    try:
        found = shopify_source.read_order(client_id, name)
    except shopify_source.SourceError as error:
        shopify_source.failure(client_id, error.code, error.status)
        return jsonify(error=shopify_source.ERRORS[error.code]), 502
    except shopify_source.InvalidOrderName:
        return jsonify(error="This shipment's order number cannot be looked up in Shopify."), 422
    except (ValueError, KeyError, json.JSONDecodeError):
        return jsonify(error="Shopify configuration is invalid or incomplete."), 503
    orders = found["orders"]
    queue_complete = not store.get("truncated") and not store.get("skipped_statuses")
    result = order_check.check(client_id, shipment, orders, same_order, complete=found["complete"],
                               queue_complete=queue_complete,
                               search_complete=found["search_complete"])
    unique = len(orders) == 1 and found["search_complete"]
    return {"order": orders[0] if unique else None, "matches": len(orders), **result, "writes": "disabled"}


@app.get("/api/shopify/daily-orders")
@protected
def shopify_daily_orders():
    """#14: one client's Shopify orders for one day vs. EOD shipped vs. the Sheet. Read-only and
    display-only: nothing changes the EOD, the ledger, Shopify or the Sheets. No customer fields.
    The read runs in the background; the page polls until the status is done or failed."""
    if not shopify_source.enabled():
        return jsonify(error="Shopify is not connected."), 503
    if not inventory_enabled():
        return jsonify(error="Needs the Google Sheets connection (Daily Sales and Dashboard)."), 503
    client_id = request.args.get("client_id", "")
    if client_id not in [client["id"] for client in CLIENTS]:
        return jsonify(error="Choose one client."), 400
    today = datetime.now(ZoneInfo("America/New_York")).date()
    try:
        day = datetime.strptime(request.args.get("date", ""), "%Y-%m-%d").date()
    except ValueError:
        return jsonify(error="Choose a date."), 400
    if day > today or (today - day).days > 366:
        return jsonify(error="Choose a date in the last year, not in the future."), 400
    if client_id not in shopify_order_clients():
        return jsonify(error="No Shopify store is mapped for this client."), 503
    if client_rules.package(client_id) is None:
        # Nothing could be compared, so Shopify and the Sheets are never read for it.
        return jsonify(error="No SKU rules are defined for this client yet."), 409
    state = daily_orders.status(client_id, day, today)
    return state, 202 if state["status"] == "running" else 200


@app.get("/api/ssk/shipment-fields")
@protected
def ssk_shipment_fields():
    """Issue #17 discovery: which fields ShipSidekick shipments carry. Names and status words only."""
    if not ssk_source.enabled():
        return jsonify(error="ShipSidekick API is not connected."), 503
    client_id = request.args.get("client_id", "")
    if client_id not in [client["id"] for client in CLIENTS]:
        return jsonify(error="Choose one client."), 400
    key = ssk_source.api_key(client_id)
    if not key:
        return jsonify(error=ssk_source.ERRORS["not_configured"]), 503
    try:
        result = ssk_source.shipment_fields(key)
    except ssk_source.SourceError as error:
        ssk_source.failure(client_id, error.code, error.status)
        return jsonify(error=ssk_source.ERRORS[error.code]), 502
    app.logger.warning("ssk shipment fields client=%s %s", client_id, json.dumps(result, separators=(",", ":")))
    return dict(result, client_id=client_id, writes="disabled")


@app.get("/api/ssk/inventory")
@protected
def ssk_inventory():
    """Read-only ShipSidekick stock next to the client Sheet. Never writes to ShipSidekick."""
    if not ssk_source.enabled():
        return jsonify(error="ShipSidekick API is not connected."), 503
    selected = request.args.get("client_id", "all")
    client_ids = [client["id"] for client in CLIENTS]
    if selected != "all" and selected not in client_ids:
        return jsonify(error="Unknown client."), 400
    ids = client_ids if selected == "all" else [selected]
    with ThreadPoolExecutor(max_workers=2) as pool:
        # The two sources are independent; a slow Sheet must not delay ShipSidekick stock.
        sheet_future = pool.submit(read_dashboards, ids) if inventory_enabled() else None
        stores = pool.submit(ssk_source.read_stores, ids).result()
        sheets = {}
        if sheet_future:
            try:
                sheets = {source["id"]: source for source in sheet_future.result()}
            except (ValueError, KeyError, json.JSONDecodeError):
                sheets = {}
    clients = []
    for store in stores:
        if "error_code" in store:
            clients.append({"client_id": store["id"], "error_code": store["error_code"], "error": store["error"]})
            continue
        result = ssk_check.compare(store["id"], store["levels"], sheets.get(store["id"]), store["truncated"])
        warnings = []
        if store["truncated"]:
            warnings.append(f"More than {ssk_source.PAGE_SIZE * ssk_source.MAX_PAGES:,} inventory rows; the rest were not read.")
        if store["environment"] == "test":
            warnings.append("Reading ShipSidekick's TEST environment, not production.")
        clients.append(dict(result, warnings=warnings, environment=store["environment"], fetched_at=store["fetched_at"]))
    return {"clients": clients, "as_of": utcnow().isoformat(), "writes": "disabled"}


@app.get("/api/template.csv")
@protected
def template():
    return Response("order_number,tracking_number,carrier,fulfillment_status,carrier_status,shipped_at,label_created_at,last_movement_at\n", mimetype="text/csv", headers={"Content-Disposition": 'attachment; filename="shipment-import-template.csv"'})


@app.post("/api/import")
@writable
def import_csv():
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("csv"), str):
        return jsonify(error="Provide a CSV file."), 400
    try:
        rows = parse_csv(body["csv"], body.get("client_id"))
    except ValueError as error:
        return jsonify(error=str(error)), 400
    from psycopg.types.json import Jsonb
    inserted = updated = 0
    with db() as conn:
        for row in rows:
            key = (row["client_id"], row["carrier"], row["tracking_number"])
            # Serialize imports and webhook updates for the same tracking identity.
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("|".join(key),))
            found = conn.execute("SELECT id,record FROM shipments WHERE client_id=%s AND carrier=%s AND tracking_number=%s FOR UPDATE", key).fetchone()
            if found:
                identity, old = found
                # Manifests enrich dates/order metadata; they never overwrite newer carrier evidence or case work.
                for field in ("order_number", "fulfillment_status", "shipped_at", "label_created_at", "mission_number"):
                    if row.get(field) and row[field] != "unknown":
                        old[field] = row[field]
                if old.get("source") != "ShipSidekick":
                    incoming = parse_date(row.get("last_movement_at"))
                    existing = parse_date(old.get("last_movement_at"))
                    if incoming and (not existing or incoming > existing):
                        old["last_movement_at"] = row["last_movement_at"]
                        old["carrier_status"] = row["carrier_status"]
                    elif row["carrier_status"] in {"delivered", "cancelled"}:
                        old["carrier_status"] = row["carrier_status"]
                conn.execute("UPDATE shipments SET record=%s WHERE id=%s", (Jsonb(old), identity))
                updated += 1
            else:
                conn.execute("INSERT INTO shipments(client_id,carrier,tracking_number,record) VALUES(%s,%s,%s,%s)", (*key, Jsonb(row)))
                inserted += 1
    return {"inserted": inserted, "updated": updated}


@app.patch("/api/shipments/<int:identity>")
@writable
def update_case(identity):
    body = request.get_json(silent=True) or {}
    status = body.get("case_status")
    notes = body.get("notes", "")
    if status not in {"open", "investigating", "carrier_contacted"} or not isinstance(notes, str) or len(notes) > 4000:
        return jsonify(error="Choose a valid case status and keep notes under 4,000 characters."), 400
    from psycopg.types.json import Jsonb
    with db() as conn:
        found = conn.execute("SELECT record FROM shipments WHERE id=%s FOR UPDATE", (identity,)).fetchone()
        if not found:
            return jsonify(error="Shipment not found."), 404
        record = found[0]
        record.update(case_status=status, notes=notes, reviewed_at=utcnow().isoformat())
        conn.execute("UPDATE shipments SET record=%s WHERE id=%s", (Jsonb(record), identity))
    return {"saved": True}


@app.post("/api/shipsidekick/<client_id>")
def webhook(client_id):
    if not live() or not ready():
        return jsonify(error="Live tracking is not enabled."), 503
    if client_id not in {c["id"] for c in CLIENTS}:
        return jsonify(error="Unknown client."), 404
    secret = os.getenv("SSK_WEBHOOK_SECRET_" + client_id.upper().replace("-", "_"))
    if not secret:
        return jsonify(error="Client connection is not configured."), 503
    signature = request.headers.get("X-SSK-Signature", "")
    expected = hmac.new(secret.encode(), request.get_data(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return jsonify(error="Invalid webhook signature."), 401
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("id"), str) or not payload["id"] or len(payload["id"]) > 200:
        return jsonify(error="Missing event ID."), 400
    if payload.get("mode") != "production":
        return jsonify(ignored="Only production events enter the live queue."), 200
    try:
        incoming = tracker_update(payload)
    except (ValueError, TypeError, AttributeError) as error:
        return jsonify(error=str(error)), 422
    from psycopg.types.json import Jsonb
    key = (client_id, incoming["carrier"], incoming["tracking_number"])
    with db() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("|".join(key),))
        added = conn.execute("INSERT INTO tracking_events(client_id,event_id) VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING event_id", (client_id, payload["id"])).fetchone()
        if not added:
            return {"duplicate": True}
        found = conn.execute("SELECT id,record FROM shipments WHERE client_id=%s AND carrier=%s AND tracking_number=%s FOR UPDATE", key).fetchone()
        identity, record = found if found else (None, {"client_id": client_id, "order_number": "", "fulfillment_status": "unknown", "case_status": "open", "notes": "", "shipped_at": None, "label_created_at": None, "events": []})
        old_stamp = parse_date(record.get("provider_event_at"))
        if not old_stamp or parse_date(incoming["provider_event_at"]) > old_stamp:
            old_movement = record.get("last_movement_at")
            merged_events = {json.dumps(e, sort_keys=True): e for e in record.get("events", []) + incoming["events"]}
            record.update(incoming)
            if old_movement and (not incoming["last_movement_at"] or parse_date(old_movement) > parse_date(incoming["last_movement_at"])):
                record["last_movement_at"] = old_movement
            record["events"] = sorted(merged_events.values(), key=lambda e: e["at"])[-100:]
        record.update(source="ShipSidekick", last_received_at=utcnow().isoformat())
        if identity:
            conn.execute("UPDATE shipments SET record=%s WHERE id=%s", (Jsonb(record), identity))
        else:
            conn.execute("INSERT INTO shipments(client_id,carrier,tracking_number,record) VALUES(%s,%s,%s,%s)", (*key, Jsonb(record)))
    return {"received": True}


@app.errorhandler(413)
def too_large(error):
    return jsonify(error="The file is too large. Maximum upload size is 5 MB."), 413


@app.errorhandler(500)
def service_error(error):
    return jsonify(error="The workspace could not load or save data. Please try again; contact the workspace administrator if it continues."), 500
