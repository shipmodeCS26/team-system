"""Carrier evidence, not fulfillment status, determines inactivity."""
from datetime import datetime, timezone, timedelta
import csv
import io

CLIENTS = [
    {"id": "claritymd", "name": "ClarityMD", "initials": "CM"},
    {"id": "fascial-labs", "name": "Fascial. Labs", "initials": "FL"},
    {"id": "muravai", "name": "Muravai", "initials": "MU"},
    {"id": "nuerosmile", "name": "Nuerosmile", "initials": "NS"},
    {"id": "puravita", "name": "PuraVita", "initials": "PV"},
]
MOVEMENT = {"in_transit", "out_for_delivery", "available_for_pickup", "delivered", "return_to_sender"}
STATUS = MOVEMENT | {"pre_transit", "unknown", "failure", "cancelled"}


def utcnow():
    return datetime.now(timezone.utc)


def parse_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        value = value.isoformat()
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def classify(row, now=None):
    now = now or utcnow()
    result = dict(row)
    status = row.get("carrier_status", "unknown")
    last_scan = parse_date(row.get("last_movement_at"))
    shipped = parse_date(row.get("shipped_at"))
    label = parse_date(row.get("label_created_at"))
    anchor = last_scan or shipped or label
    days = max(0, int((now - anchor).total_seconds() // 86400)) if anchor else None
    if status == "delivered":
        tier, reason = "delivered", "Delivery confirmed by carrier"
    elif status == "cancelled":
        tier, reason = "cancelled", "Label cancelled"
    elif anchor is None:
        tier, reason = "data_gap", "Shipping date or scan history needed"
    elif not last_scan and status not in {"pre_transit"}:
        tier, reason = "data_gap", "Carrier scan timestamp needed to measure inactivity"
    else:
        tier = "critical" if days >= 10 else "urgent" if days >= 7 else "watch" if days >= 5 else "monitoring"
        reason = "Stalled after a carrier scan" if last_scan else "No carrier acceptance scan recorded"
    result.update(tier=tier, days=days, reason=reason,
                  anchor_at=anchor.isoformat() if anchor else None,
                  anchor_source="Last carrier movement" if last_scan else "Shipped date" if shipped else "Label created" if label else "Missing",
                  never_scanned=not bool(last_scan))
    return result


def parse_csv(text, client_id):
    if client_id not in {c["id"] for c in CLIENTS}:
        raise ValueError("Choose one client before importing.")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    native = reader.fieldnames and "Tracking Code" in reader.fieldnames
    if not reader.fieldnames or not native and "tracking_number" not in reader.fieldnames:
        raise ValueError("Use a ShipSidekick shipment export with Tracking Code, or download our CSV template.")
    rows, seen = [], set()
    for line, raw in enumerate(reader, 2):
        if line > 10001:
            raise ValueError("Import at most 10,000 shipments per file.")
        if None in raw:
            raise ValueError("Row %s has more values than column headers." % line)
        raw = {k: (v or "").strip() for k, v in raw.items()}
        if native:
            organization = raw.get("Organization", "")
            normalize = lambda value: "".join(c.lower() for c in value if c.isalnum())
            expected_name = next(c["name"] for c in CLIENTS if c["id"] == client_id)
            if organization and normalize(organization) != normalize(expected_name):
                raise ValueError("Row %s belongs to %s, not the selected client. Export one client at a time." % (line, organization))
            created = raw.get("Created Date", "")
            if created and "/" in created:
                try:
                    fmt = "%m/%d/%y" if len(created.split("/")[-1]) == 2 else "%m/%d/%Y"
                    # Date-only report values are a day-level label-age anchor, never a physical scan.
                    created = datetime.strptime(created, fmt).replace(tzinfo=timezone.utc).isoformat()
                except ValueError:
                    raise ValueError("Row %s has an unrecognized Created Date. Use the ISO template for timestamps." % line)
            raw = {"tracking_number": raw.get("Tracking Code", ""), "carrier": raw.get("Carrier", ""),
                   "order_number": raw.get("Order Name", ""), "fulfillment_status": "unknown",
                   "carrier_status": "cancelled" if raw.get("Voided", "").lower() in {"yes", "true", "1"} else raw.get("Tracking Status", "unknown"),
                   "label_created_at": created, "mission_number": raw.get("Mission Num", ""),
                   "additional_tracking_codes": raw.get("Additional Tracking Codes", "")}
        if raw.get("additional_tracking_codes"):
            raise ValueError("Row %s has additional tracking codes. Provide one row per package using the template so no package is missed." % line)
        number = raw.get("tracking_number", "")
        carrier = raw.get("carrier", "").lower()
        if not number or len(number) > 120 or not carrier or len(carrier) > 40:
            raise ValueError("Row %s needs a tracking number and carrier." % line)
        key = (number, carrier)
        if key in seen:
            raise ValueError("Row %s repeats a tracking number for the same carrier." % line)
        seen.add(key)
        row = {"client_id": client_id, "tracking_number": number, "carrier": carrier,
               "order_number": raw.get("order_number", "")[:120],
               "fulfillment_status": raw.get("fulfillment_status", "shipped")[:40],
               "carrier_status": raw.get("carrier_status", "unknown").lower().replace("-", "_").replace(" ", "_") or "unknown",
               "mission_number": raw.get("mission_number", "")[:100],
               "additional_tracking_codes": raw.get("additional_tracking_codes", "")[:1000]}
        if row["carrier_status"] not in STATUS:
            raise ValueError("Row %s has an unsupported carrier_status." % line)
        for name in ("shipped_at", "label_created_at", "last_movement_at"):
            try:
                value = parse_date(raw.get(name))
            except (ValueError, TypeError):
                raise ValueError("Row %s: %s must be an ISO date such as 2026-09-01T14:00:00Z." % (line, name))
            if value and value > utcnow() + timedelta(minutes=5):
                raise ValueError("Row %s: %s cannot be in the future." % (line, name))
            row[name] = value.isoformat() if value else None
        row.update(case_status="open", notes="", events=[], source="ShipSidekick CSV" if native else "CSV import", last_received_at=None,
                   date_precision="report_date" if native else "timestamp")
        rows.append(row)
    if not rows:
        raise ValueError("This CSV contains no shipment rows.")
    return rows


def tracker_update(payload):
    """Normalize the documented EasyPost-compatible ShipSidekick tracker event."""
    if payload.get("description") not in {"tracker.created", "tracker.updated"}:
        raise ValueError("Only tracker.created and tracker.updated events are supported.")
    tracker = payload.get("result")
    if not isinstance(tracker, dict):
        raise ValueError("Missing tracker result.")
    number = tracker.get("tracking_code")
    carrier = tracker.get("carrier")
    if not isinstance(number, str) or not number or not isinstance(carrier, str) or not carrier:
        raise ValueError("Expected the EasyPost-compatible tracking_code and carrier fields.")
    events = []
    for detail in tracker.get("tracking_details", []):
        stamp = parse_date(detail.get("datetime"))
        if not stamp or stamp > utcnow() + timedelta(minutes=5):
            continue
        description = str(detail.get("description", ""))[:500]
        status = detail.get("status", "unknown")
        # Electronic / label events never count as physical movement, even if a provider labels them in_transit.
        electronic = any(term in description.lower() for term in ("label created", "shipping label", "shipment information", "electronic notification", "awaiting item", "pre-shipment"))
        events.append({"at": stamp.isoformat(), "status": status, "description": description,
                       "movement": status in MOVEMENT and not electronic})
    events.sort(key=lambda x: x["at"])
    movement = [e["at"] for e in events if e["movement"]]
    stamp = parse_date(payload.get("created_at"))
    if not stamp:
        raise ValueError("Missing event timestamp.")
    status = tracker.get("status", "unknown")
    return {"tracking_number": number[:120], "carrier": carrier.lower()[:40],
            "carrier_status": status if status in STATUS else "unknown",
            "last_movement_at": max(movement) if movement else None,
            "events": events[-100:], "provider_event_at": stamp.isoformat()}


def sample_shipments():
    now = utcnow()
    ago = lambda n: (now - timedelta(days=n, hours=2)).isoformat()
    examples = [
        (12, "pre_transit", False, "fulfilled"), (10, "in_transit", True, "fulfilled"),
        (8, "pre_transit", False, "shipped"), (7, "in_transit", True, "fulfilled"),
        (6, "pre_transit", False, "fulfilled"), (5, "in_transit", True, "shipped"),
        (2, "in_transit", True, "fulfilled"), (1, "delivered", True, "fulfilled"),
    ]
    result = []
    for client_index, client in enumerate(CLIENTS):
        for i, (days, status, moved, fulfillment) in enumerate(examples):
            days += client_index % 2 if status != "delivered" else 0
            number = "DEMO-%s-%04d" % (client["initials"], i + 1)
            shipped = ago(days + 3 if moved else days)
            events = [{"at": shipped, "status": "pre_transit", "description": "Shipping label created", "movement": False}]
            if moved:
                events.append({"at": ago(days), "status": status, "description": "Delivered" if status == "delivered" else "Arrived at carrier facility", "movement": True})
            result.append({"id": len(result)+1, "client_id": client["id"], "order_number": "#%d" % (10420 + client_index*100 + i),
                           "tracking_number": number, "carrier": ["usps", "ups", "fedex"][i % 3],
                           "fulfillment_status": fulfillment, "carrier_status": status,
                           "shipped_at": shipped, "label_created_at": shipped,
                           "last_movement_at": ago(days) if moved else None,
                           "last_received_at": ago(0), "source": "Sample data",
                           "case_status": "investigating" if i == 1 else "open", "notes": "", "events": events})
    result.append({"id": 41, "client_id": "claritymd", "order_number": "#10499", "tracking_number": "DEMO-CM-0099", "carrier": "usps", "fulfillment_status": "fulfilled", "carrier_status": "unknown", "shipped_at": None, "label_created_at": None, "last_movement_at": None, "last_received_at": None, "source": "Sample data", "case_status": "open", "notes": "", "events": []})
    return result
