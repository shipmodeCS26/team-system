"""Confirm No Movement shipments with the carrier itself (Issue #31).

ShipSidekick stops refreshing tracking, so a shipment it shows as stuck may already be delivered.
Before a shipment counts as stuck, its tracking number is looked up with the carrier's official
tracking API and given exactly one verdict:

  delivered    the carrier shows a delivery scan with a time (not a delivery back to the sender)
  moved        the carrier has a physical scan newer than ShipSidekick's; the age is recomputed
  confirmed    the carrier shows nothing newer either: really no movement
  unverified   anything else (error, timeout, rate limit, not found, unknown words, a reply for a
               different tracking number, carrier not supported). Never treated as delivered or stuck.

Reads only: the one POST allowed is USPS's OAuth token request. Keys come from environment variables
and never reach logs, pages or exports. Response field names follow USPS Tracking API v3
(GET /tracking/v3/tracking/{n}?expand=DETAIL) and DHL Shipment Tracking - Unified
(GET /track/shipments); they were not checked against a live response from this environment, so the
staging sign-off in #31 (50 shipments per carrier agree 100% with the carrier's website) gates release.
"""
from __future__ import annotations

from datetime import timedelta
import logging
import os
import threading
import time

import requests

from tracking import is_physical, parse_date, utcnow

log = logging.getLogger(__name__)

USPS_BASE = "https://apis.usps.com"
DHL_BASE = "https://api-eu.dhl.com"
TIMEOUT = 20
FLAGGED = ("critical", "urgent", "watch")
VERDICTS = ("delivered", "moved", "confirmed", "unverified")

# USPS writes some events on a schedule without anyone scanning the parcel. Counting them as movement
# would hide a stuck parcel, so they never reset the clock (conservative: the shipment stays in the queue).
NOT_A_SCAN = ("label created", "shipping label", "pre-shipment", "shipment information", "electronic",
              "awaiting item", "in transit to next facility", "arriving late", "reminder to schedule redelivery",
              "delivery status not updated", "awaiting delivery scan", "moving through network",
              "tracking number not yet available", "acceptance pending", "notification received")
RETURNED = ("sender", "return")


class ReadOnlyViolation(RuntimeError):
    """Raised before sending any carrier request that is not an allowlisted read."""


class CheckError(Exception):
    def __init__(self, reason, retry_after=None):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


def enabled():
    return os.getenv("CARRIER_CHECK_ENABLED", "false").lower() == "true"


def _send(method, url, **kwargs):
    """The single path to any carrier. Only tracking reads and the USPS token request may leave."""
    allowed = (method == "GET" and (url.startswith(USPS_BASE + "/tracking/v3/tracking/")
                                    or url == DHL_BASE + "/track/shipments")) or \
              (method == "POST" and url == USPS_BASE + "/oauth2/v3/token")
    if not allowed:
        raise ReadOnlyViolation("Only carrier tracking reads may be sent.")
    try:
        return requests.request(method, url, timeout=TIMEOUT, **kwargs)
    except requests.RequestException:
        raise CheckError("Carrier did not answer")


def _http_error(response):
    status = response.status_code
    if status == 404:
        return CheckError("Tracking number not found at the carrier")
    if status == 429:
        after = response.headers.get("Retry-After", "") if hasattr(response, "headers") else ""
        return CheckError("Carrier rate limit reached", retry_after=int(after) if str(after).isdigit() else None)
    if status in (401, 403):
        return CheckError("Carrier rejected our API key")
    return CheckError(f"Carrier answered with an error ({status})")


def _json(response):
    try:
        body = response.json()
    except ValueError:
        raise CheckError("Carrier answer could not be read")
    if not isinstance(body, dict):
        raise CheckError("Carrier answer could not be read")
    return body


def _event(at, state, description):
    stamp = parse_date(at) if at else None
    return {"at": stamp.isoformat() if stamp else None, "status": state, "description": str(description or "")[:200]}


# ---- USPS ---------------------------------------------------------------------------------------

_token = {"value": None, "expires": 0.0}
_token_lock = threading.Lock()


def _usps_token():
    client_id, secret = os.getenv("USPS_CLIENT_ID", "").strip(), os.getenv("USPS_CLIENT_SECRET", "").strip()
    if not client_id or not secret:
        raise CheckError("USPS API key not set")
    with _token_lock:
        if _token["value"] and time.monotonic() < _token["expires"]:
            return _token["value"]
        response = _send("POST", USPS_BASE + "/oauth2/v3/token", json={
            "client_id": client_id, "client_secret": secret, "grant_type": "client_credentials"})
        if response.status_code != 200:
            raise _http_error(response)
        body = _json(response)
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise CheckError("USPS did not issue a token")
        expires = body.get("expires_in")
        lifetime = int(expires) if str(expires).isdigit() else 900
        _token.update(value=token, expires=time.monotonic() + max(60, lifetime - 120))
        return token


def usps_lookup(tracking_number):
    """-> (echoed tracking number, overall status text, delivered flag, [events])."""
    token = _usps_token()
    response = _send("GET", f"{USPS_BASE}/tracking/v3/tracking/{tracking_number}", params={"expand": "DETAIL"},
                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if response.status_code == 401:
        with _token_lock:
            _token.update(value=None, expires=0.0)
    if response.status_code != 200:
        raise _http_error(response)
    body = _json(response)
    if body.get("error"):
        raise CheckError("Tracking number not found at the carrier")
    events = []
    for raw in body.get("trackingEvents") or []:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("eventType") or "")
        # GMTTimestamp is UTC; eventTimestamp is the local time of the scan without an offset.
        events.append(_event(raw.get("GMTTimestamp") or raw.get("eventTimestamp"),
                             _usps_state(text, raw.get("eventCode")), text))
    category = str(body.get("statusCategory") or "").strip().lower()
    return str(body.get("trackingNumber") or ""), category or str(body.get("status") or "").lower(), \
        category == "delivered", events


def _usps_state(text, code):
    lower = text.lower()
    if lower.startswith("delivered") or str(code or "") == "01":
        return "delivered"
    if not lower:
        return "unknown"
    return "in_transit"


# ---- DHL eCommerce ------------------------------------------------------------------------------

DHL_STATES = {"pre-transit": "pre_transit", "transit": "in_transit", "delivered": "delivered",
              "failure": "in_transit", "unknown": "unknown"}


def dhl_lookup(tracking_number):
    key = os.getenv("DHL_API_KEY", "").strip()
    if not key:
        raise CheckError("DHL API key not set")
    response = _send("GET", DHL_BASE + "/track/shipments",
                     params={"trackingNumber": tracking_number, "service": "ecommerce"},
                     headers={"DHL-API-Key": key, "Accept": "application/json"})
    if response.status_code != 200:
        raise _http_error(response)
    shipments = [s for s in _json(response).get("shipments") or [] if isinstance(s, dict)]
    matches = [s for s in shipments if _dhl_matches(s, tracking_number)]
    if len(matches) != 1:
        raise CheckError("Carrier answer is for a different or ambiguous tracking number")
    shipment = matches[0]
    status = shipment.get("status") if isinstance(shipment.get("status"), dict) else {}
    code = str(status.get("statusCode") or "").lower()
    if code not in DHL_STATES:
        raise CheckError("Carrier status not recognised")
    events = []
    for raw in shipment.get("events") or []:
        if not isinstance(raw, dict):
            continue
        state = DHL_STATES.get(str(raw.get("statusCode") or "").lower(), "unknown")
        events.append(_event(raw.get("timestamp"), state, raw.get("description") or raw.get("status")))
    return tracking_number, code, code == "delivered", events


def _dhl_matches(shipment, tracking_number):
    pieces = ((shipment.get("details") or {}) if isinstance(shipment.get("details"), dict) else {}).get("pieceIds") or []
    return str(shipment.get("id") or "") == tracking_number or tracking_number in [str(p) for p in pieces]


LOOKUPS = {"usps": usps_lookup, "dhl-ecommerce": dhl_lookup}
KEYS = {"usps": ("USPS_CLIENT_ID", "USPS_CLIENT_SECRET"), "dhl-ecommerce": ("DHL_API_KEY",)}


def configured(carrier):
    return carrier in LOOKUPS and all(os.getenv(name, "").strip() for name in KEYS[carrier])
PENDING = {"cirro-ecommerce": "Cirro tracking access pending (#31)"}


# ---- Verdict ------------------------------------------------------------------------------------

def verdict(row, lookup=None, now=None):
    """One verdict for one queue row. Never raises: every failure becomes 'unverified'."""
    now = now or utcnow()
    base = {"checked_at": now.isoformat(), "looked_up": False, "carrier_status": None, "carrier_event": None,
            "carrier_event_at": None, "carrier_last_scan_at": None}
    carrier, number = str(row.get("carrier") or ""), str(row.get("tracking_number") or "").strip()
    lookup = lookup or LOOKUPS.get(carrier)
    if not number.isascii() or not number.isalnum():
        return dict(base, verdict="unverified", reason="Tracking number format not recognised")
    if lookup is None:
        return dict(base, verdict="unverified", reason=PENDING.get(carrier, "Carrier not supported yet"))
    if lookup is LOOKUPS.get(carrier) and not configured(carrier):
        return dict(base, verdict="unverified", reason=f"{carrier.split('-')[0].upper()} API key not set")
    base["looked_up"] = True
    try:
        echoed, status, delivered, events = lookup(number)
    except CheckError as error:
        return dict(base, verdict="unverified", reason=error.reason, retry_after=error.retry_after)
    except ReadOnlyViolation:
        raise
    except Exception as error:  # a malformed answer must never look like evidence
        log.warning("carrier check failed carrier=%s type=%s", carrier, type(error).__name__)
        return dict(base, verdict="unverified", reason="Carrier answer could not be read")
    if echoed.replace(" ", "").upper() != number.replace(" ", "").upper():
        return dict(base, verdict="unverified", reason="Carrier answer is for a different tracking number")
    future = now + timedelta(minutes=5)
    dated = sorted((e for e in events if e["at"] and parse_date(e["at"]) <= future), key=lambda e: e["at"])
    latest = dated[-1] if dated else None
    base.update(carrier_status=status, carrier_event=latest["description"] if latest else None,
                carrier_event_at=latest["at"] if latest else None, events=dated[-15:])
    scans = [e for e in dated if _moved(e)]
    base["carrier_last_scan_at"] = scans[-1]["at"] if scans else None
    handed_over = [e for e in dated if e["status"] == "delivered"
                   and not any(word in e["description"].lower() for word in RETURNED)]
    if delivered and handed_over:
        return dict(base, verdict="delivered", reason="Carrier shows it delivered",
                    carrier_delivered_at=handed_over[-1]["at"])
    if delivered:
        return dict(base, verdict="unverified", reason="Carrier says delivered but shows no delivery scan")
    if not status or status == "unknown":
        return dict(base, verdict="unverified", reason="Carrier status not recognised")
    ssk_scan = parse_date(row.get("last_movement_at")) if row.get("last_movement_at") else None
    if scans and (ssk_scan is None or parse_date(scans[-1]["at"]) > ssk_scan):
        return dict(base, verdict="moved", reason="Carrier has a newer scan than ShipSidekick")
    return dict(base, verdict="confirmed", reason="Carrier also shows no newer movement")


def _moved(event):
    text = event["description"].lower()
    return is_physical(event["status"], text) and not any(term in text for term in NOT_A_SCAN)


def apply(row, result):
    """The queue row after the carrier's evidence. ShipSidekick's own row is never changed."""
    from tracking import classify
    out = dict(row, carrier_check=result)
    if not result:
        return out
    if result["verdict"] == "delivered":
        out.update(tier="delivered", reason="Delivered per carrier — ShipSidekick out of date", ssk_out_of_date=True)
    elif result["verdict"] == "moved":
        out = classify(dict(out, last_movement_at=result["carrier_last_scan_at"]))
        out["ssk_out_of_date"] = True
    return out


# ---- Background checks --------------------------------------------------------------------------
# Lookups are slow and rate limited, so the page never waits for them: flagged shipments are queued,
# checked oldest first by one thread per process within each carrier's limit, and the page shows the
# latest finished verdict with its check time.

_results = {}     # (carrier, tracking number) -> verdict
_wanted = {}      # (carrier, tracking number) -> (days, row)
_next_ok = {}     # carrier -> monotonic time the next lookup may start
_lock = threading.Lock()
_worker = None


def _workers():
    value = os.getenv("WEB_CONCURRENCY", "1").strip()
    return max(1, int(value)) if value.isdigit() else 1


def interval(carrier):
    """Seconds between lookups for one carrier in this process (limits are split across workers)."""
    per_hour = {"usps": os.getenv("USPS_LOOKUPS_PER_HOUR", "60"),
                "dhl-ecommerce": os.getenv("DHL_LOOKUPS_PER_DAY", "250")}
    raw = per_hour.get(carrier, "60")
    count = max(1, int(raw)) if str(raw).isdigit() else 60
    seconds = (86400 if carrier == "dhl-ecommerce" else 3600) / count
    return max(seconds, 5 if carrier == "dhl-ecommerce" else 1) * _workers()


def recheck_seconds():
    value = os.getenv("CARRIER_RECHECK_HOURS", "6").strip()
    return (max(1, int(value)) if value.isdigit() else 6) * 3600


def results_for(rows):
    """Latest verdicts for these rows, and queue the flagged ones that are unchecked or due again."""
    now = time.time()
    out, wanted = {}, {}
    with _lock:
        for row in rows:
            key = (row.get("carrier"), row.get("tracking_number"))
            result = _results.get(key)
            if result:
                out[key] = result
            if row.get("tier") in FLAGGED and configured(key[0]):
                checked = parse_date(result["checked_at"]).timestamp() if result else 0
                if not result or result["verdict"] == "unverified" and now - checked > 900 or now - checked > recheck_seconds():
                    wanted[key] = (row.get("days") or 0, row)
            elif row.get("tier") in FLAGGED and key not in _results:
                out[key] = verdict(row)  # not supported or no key: answered at once, no request sent
        _wanted.clear()
        _wanted.update(wanted)
    if wanted:
        _start()
    return out


def _start():
    global _worker
    with _lock:
        if _worker and _worker.is_alive():
            return
        _worker = threading.Thread(target=_run, daemon=True)
        _worker.start()


def _next_job():
    with _lock:
        ready = [(key, item) for key, item in _wanted.items() if time.monotonic() >= _next_ok.get(key[0], 0)]
        if not ready:
            return None, (min((_next_ok.get(k[0], 0) for k in _wanted), default=None))
        key, (_, row) = max(ready, key=lambda pair: pair[1][0])  # oldest first
        del _wanted[key]
        _next_ok[key[0]] = time.monotonic() + interval(key[0])
        return (key, row), None


def _run():
    while True:
        job, wake = _next_job()
        if job is None:
            if wake is None:
                return
            time.sleep(max(0.5, min(30, wake - time.monotonic())))
            continue
        key, row = job
        result = verdict(row)
        if result.get("retry_after") or result["reason"] == "Carrier rate limit reached":
            with _lock:
                _next_ok[key[0]] = time.monotonic() + (result.get("retry_after") or 900)
        with _lock:
            _results[key] = result


def progress(rows):
    """Counts for the page: how many flagged shipments have a verdict yet."""
    flagged = [r for r in rows if r.get("tier") in FLAGGED or r.get("carrier_check")]
    counts = {v: 0 for v in VERDICTS}
    cannot = 0
    for row in flagged:
        check = row.get("carrier_check")
        if check and check.get("looked_up"):
            counts[check["verdict"]] += 1
        elif check:
            cannot += 1
    return {"checked": sum(counts.values()), "flagged": len(flagged), "cannot_check": cannot, **counts}
