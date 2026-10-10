"""Shipping report per client (Issue #24): the shipping side of the 3PL reports, next to the EOD
inventory report (#9).

Built only from the ShipSidekick shipments the No Movement page already reads (GET only), for one
client at a time. The queue holds shipments that are not delivered yet, so every count here is of
open shipments in the lookback window. Rows carry order number, tracking, carrier and dates only:
no customer names or addresses ever reach the report, its text or its CSV. Nothing is sent.
"""
from __future__ import annotations

import csv
import io
import os
from datetime import date, datetime, timedelta

from tracking import classify, parse_date, utcnow

COMPLETE, INCOMPLETE = "COMPLETE", "INCOMPLETE"
STATUSES = (("pre_transit", "Label created, not scanned"), ("in_transit", "In transit"),
            ("out_for_delivery", "Out for delivery"), ("available_for_pickup", "Available for pickup"),
            ("return_to_sender", "Return to sender"), ("failure", "Delivery failure"),
            ("unknown", "Unknown status"))
EXCEPTIONS = {"return_to_sender": "Return to sender", "failure": "Delivery failure"}
AGING = (("critical", "10+ days"), ("urgent", "7-9 days"), ("watch", "5-6 days"),
         ("monitoring", "under 5 days"), ("data_gap", "missing scan data"))
OLDEST_SHOWN = 10
CSV_HEADER = ["client", "section", "order_number", "tracking_number", "carrier", "carrier_status",
              "label_created_at", "last_scan_at", "days", "tier"]


def label_late_business_days() -> int:
    """PROPOSED rule (#24 open question 1): a label not scanned after this many business days is late."""
    value = os.getenv("SHIP_LABEL_LATE_BUSINESS_DAYS", "2").strip()
    return min(max(int(value), 1), 10) if value.isdigit() else 2


def business_days_between(start: date, end: date) -> int:
    """Weekdays after `start` up to and including `end` (holidays are not known, so not skipped)."""
    days, current = 0, start
    while current < end:
        current += timedelta(days=1)
        if current.weekday() < 5:
            days += 1
    return days


def _csv_cell(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _line(row: dict) -> str:
    parts = [row.get("order_number") or "no order number", row.get("tracking_number") or "no tracking",
             (row.get("carrier") or "unknown").upper()]
    return " · ".join(parts)


def build(client_id: str, client_name: str, store: dict, window_days: int, now: datetime | None = None) -> dict:
    """`store` is one finished ShipSidekick read for this client (ssk_source.read_shipments output)."""
    now = now or utcnow()
    rows = [classify(row, now) for row in store.get("rows") or [] if row.get("client_id") == client_id]
    foreign = sum(1 for row in store.get("rows") or [] if row.get("client_id") != client_id)
    reasons = []
    if store.get("truncated"):
        reasons.append("ShipSidekick returned more shipments than the app reads; later ones may be missing.")
    if store.get("skipped_statuses"):
        reasons.append("ShipSidekick did not accept these status filters, so they are not counted: "
                       + ", ".join(store["skipped_statuses"]) + ".")
    if foreign:
        reasons.append(f"{foreign} shipment(s) from another store were left out.")
    status = INCOMPLETE if reasons else COMPLETE

    open_rows = [row for row in rows if row.get("carrier_status") not in ("delivered", "cancelled")]
    volume = [{"status": key, "label": label, "count": sum(r.get("carrier_status") == key for r in open_rows)}
              for key, label in STATUSES]
    exceptions = [row for row in open_rows if row.get("carrier_status") in EXCEPTIONS]
    exceptions.sort(key=lambda r: r.get("last_movement_at") or r.get("label_created_at") or "")
    aging = [{"tier": key, "label": label, "count": sum(r.get("tier") == key for r in open_rows)}
             for key, label in AGING]
    stalled = [r for r in open_rows if r.get("tier") in ("critical", "urgent", "watch")]
    oldest = sorted(stalled, key=lambda r: -(r.get("days") or 0))[:OLDEST_SHOWN]
    threshold = label_late_business_days()
    late_labels = []
    for row in open_rows:
        label_at = parse_date(row.get("label_created_at"))
        if row.get("carrier_status") == "pre_transit" and row.get("never_scanned") and label_at:
            waited = business_days_between(label_at.date(), now.date())
            if waited >= threshold:
                late_labels.append(dict(row, business_days=waited))
    late_labels.sort(key=lambda r: -r["business_days"])

    fetched = store.get("fetched_at") or ""
    sections = [
        {"title": "Open shipments", "lines": [f"• {v['label']}: {v['count']:,}" for v in volume]
         + [f"• Total not yet delivered: {len(open_rows):,}"]},
        {"title": "Exceptions", "lines": [f"• {EXCEPTIONS[r['carrier_status']]}: {_line(r)}" for r in exceptions]
         or ["• None."]},
        {"title": "No Movement", "lines": [f"• {a['label']}: {a['count']:,}" for a in aging]
         + ([f"Oldest without movement:"] + [f"• {r['days']} days: {_line(r)}" for r in oldest] if oldest else [])},
        {"title": "Labels not picked up", "lines": [f"• {r['business_days']} business days: {_line(r)}"
                                                     for r in late_labels]
         or [f"• None older than {threshold} business days."]},
        {"title": "Data status", "lines": [f"• {status}: ShipSidekick read {fetched[:16].replace('T', ' ')} UTC, "
                                           f"last {window_days} days, open shipments only."]
         + [f"• {reason}" for reason in reasons]},
    ]
    lines = []
    if status != COMPLETE:
        lines += [f"INCOMPLETE, CHECK BEFORE USING: " + " ".join(reasons), ""]
    lines.append(f"{client_name} shipping report (internal), last {window_days} days.")
    for number, section in enumerate(sections, start=1):
        lines += ["", f"*{number}. {section['title']}*", *section["lines"]]

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_HEADER)
    listed = ([("exception", r) for r in exceptions] + [("no_movement", r) for r in stalled]
              + [("label_not_picked_up", r) for r in late_labels])
    for section, row in listed:
        writer.writerow([_csv_cell(v) for v in (
            client_id, section, row.get("order_number"), row.get("tracking_number"), row.get("carrier"),
            row.get("carrier_status"), row.get("label_created_at"), row.get("last_movement_at"),
            row.get("business_days", row.get("days")), row.get("tier"))])

    return {"client_id": client_id, "status": status, "reasons": reasons, "window_days": window_days,
            "fetched_at": fetched, "label_late_business_days": threshold, "label_rule": "PROPOSED",
            "volume": volume, "aging": aging, "exceptions": len(exceptions), "labels_late": len(late_labels),
            "sections": sections, "text": "\n".join(lines), "csv": out.getvalue(), "writes": "disabled"}
