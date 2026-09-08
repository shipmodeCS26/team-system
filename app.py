import csv
import hashlib
import hmac
import io
import json
import os
import secrets
from functools import wraps

from flask import Flask, Response, jsonify, render_template, request, session
from werkzeug.security import check_password_hash

from tracking import CLIENTS, classify, parse_csv, parse_date, sample_shipments, tracker_update, utcnow

app = Flask(__name__)
app.config.update(SECRET_KEY=os.getenv("SECRET_KEY", secrets.token_hex(32)),
                  SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Strict", MAX_CONTENT_LENGTH=5 * 1024 * 1024)


def live():
    return os.getenv("APP_MODE", "demo") == "live"


def ready():
    return all(os.getenv(k) for k in ("DATABASE_URL", "WORKSPACE_USER", "WORKSPACE_PASSWORD_HASH", "SECRET_KEY"))


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
        if live():
            if not ready():
                return jsonify(error="Live workspace is not configured. No shipment data is exposed."), 503
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
    else:
        records = sample_shipments()
    return {"mode": "live" if live() else "demo", "clients": CLIENTS,
            "shipments": [classify(row) for row in records], "as_of": utcnow().isoformat(),
            "integration": "Awaiting verified ShipSidekick connection"}


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
