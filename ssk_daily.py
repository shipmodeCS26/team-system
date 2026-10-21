"""One Eastern-time day of ShipSidekick shipments for a client, read-only.

Used to load a day's sales into a client's Daily Sales tab and to check a loaded
day against ShipSidekick. Only GET requests, through ssk_source._get.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import ssk_source

ET = ZoneInfo("America/New_York")


def et_date(created_at):
    """ShipSidekick createdAt (UTC ISO) -> the Eastern calendar date."""
    return datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(ET).date()


def _items(shipment):
    """(quantity, title, sku) per line item across all packages."""
    out = []
    for package in shipment.get("packages") or []:
        for line in package.get("lineItems") or []:
            variant = line.get("productVariant") or {}
            out.append((int(line.get("quantity") or 0), variant.get("title") or "", variant.get("sku") or ""))
    return out


def items_text(items):
    """Same shape as the CSV export's Items column: '2x Title (sku); 1x Title (sku)'."""
    return "; ".join(f"{q}x {t} ({s})" for q, t, s in items)


def day_shipments(client_id, day, page_size=100, max_pages=200):
    """Shipments whose createdAt falls on `day` (Eastern). Pages newest-first until older than `day`.

    /shipments ignores date filters, so this walks back from the newest shipment.
    Returns a list of dicts; voided shipments are kept with voided=True so callers decide.
    """
    key = ssk_source.api_key(client_id)
    if not key:
        raise RuntimeError(f"no ShipSidekick key for {client_id}")
    rows, seen = [], set()
    for page in range(1, max_pages + 1):
        body = ssk_source._get(key, "/shipments", {"sortOrder": "desc", "limit": page_size, "page": page})
        data = body.get("data") or []
        if not data:
            break
        oldest = None
        for s in data:
            d = et_date(s["createdAt"])
            oldest = d if oldest is None or d < oldest else oldest
            if d != day or s.get("id") in seen:
                continue
            seen.add(s.get("id"))
            rows.append({
                "id": s.get("id"),
                "tracking": s.get("trackingCode") or "",
                "created_at": s.get("createdAt"),
                "order": (s.get("order") or {}).get("name") or "",
                "voided": bool(s.get("voidStatus")),
                "items": _items(s),
            })
        if oldest is not None and oldest < day or not body.get("hasMore", True):
            break
    else:
        raise RuntimeError(f"stopped after {max_pages} pages before reaching {day}")
    return rows


def sku_totals(rows):
    """Units per SKU over non-voided shipments."""
    totals = {}
    for r in rows:
        if r["voided"]:
            continue
        for q, _t, s in r["items"]:
            totals[s] = totals.get(s, 0) + q
    return totals


if __name__ == "__main__":
    import json
    import sys

    client, day = sys.argv[1], date.fromisoformat(sys.argv[2])
    rows = day_shipments(client, day)
    live = [r for r in rows if not r["voided"]]
    print(json.dumps({
        "client": client, "day": str(day), "shipments": len(live), "voided": len(rows) - len(live),
        "orders": len({r["order"] for r in live}), "sku_totals": sku_totals(rows),
    }, indent=1))
