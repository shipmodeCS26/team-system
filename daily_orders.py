"""Daily Shopify orders vs. EOD shipped vs. Sheet units sold, per client and SKU (#14).

Read-only and display-only. Shopify is a cross-check: nothing here changes the EOD, the ledger,
Shopify or the client Sheets. Customer fields are never read.

Defaults for the #14 open questions (proposed to Gly, 2026-10-06):
  1. A day is midnight to midnight US Eastern, by the Shopify order's created time and the
     ShipSidekick label's Created Date.
  2. Every paid or pending order that is not cancelled counts as ordered (on-hold and pre-orders
     included); cancelled and test orders are excluded and counted separately.
  3. An order placed on the day and shipped on a later day (or one placed earlier and shipped on
     the day) is a timing difference, shown separately from unexplained differences.
"""
from __future__ import annotations

import logging
import re
import threading
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from time import monotonic
from zoneinfo import ZoneInfo

import client_rules
import eod
import inventory
import ledger_sources
import shopify_source
import ssk_check
from incoming import whole_units

ET = ZoneInfo("America/New_York")
DAYS_BEFORE = 2  # Shopify orders are read from two days before the chosen day, to explain timing
LIST_LIMIT = 200  # order-level rows returned per type; the counts always cover every order

TEXT = {
    "not_in_ssk": "In Shopify, no ShipSidekick label yet",
    "fulfilled_not_in_ssk": "Shopify shows it fulfilled, but there is no ShipSidekick label",
    "not_in_shopify": "Shipped in ShipSidekick, not found in Shopify",
    "quantity_mismatch": "Quantity differs between Shopify and ShipSidekick",
    "unmapped": "Item could not be mapped to an internal SKU",
    "cancelled_but_shipped": "Cancelled in Shopify, but a label exists",
    "refunded_after_shipping": "Refunded in Shopify after a label was created (not subtracted)",
    "edited": "Order lines were edited or refunded in Shopify (ordered quantity kept)",
    "several_orders": "More than one Shopify order has this name",
    "items_truncated": "Order has more lines than were read; items not compared",
    "shipped_later": "Ordered on this day, shipped on a later day (timing)",
    "ordered_earlier": "Shipped on this day, ordered on an earlier day (timing)",
}
EXCEPTIONS = ("not_in_ssk", "fulfilled_not_in_ssk", "not_in_shopify", "quantity_mismatch", "unmapped",
              "cancelled_but_shipped", "refunded_after_shipping", "several_orders", "items_truncated", "edited")
TIMING = ("shipped_later", "ordered_earlier")

AS_OF_FORMATS = ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d")


def order_key(name) -> str:
    """Shopify '#1234' and a ShipSidekick 'Order Name' of '#1234' or '1234' are the same order."""
    return re.sub(r"\s+", "", str(name or "")).lstrip("#").lower()


def window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day - timedelta(days=DAYS_BEFORE), time(0), ET)
    return start, datetime.combine(day + timedelta(days=1), time(0), ET)


def local_day(timestamp) -> date | None:
    try:
        moment = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(ET).date() if moment.tzinfo else None


def sheet_day(as_of) -> date | None:
    text = re.sub(r"^[A-Za-z]{3,9},\s+(?=\d)", "", str(as_of or "").strip())  # "Mon, 05 Oct 2026"
    for fmt in AS_OF_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _clean(text) -> str:
    return re.sub(r"[;\r\n]+", ",", str(text or "")).strip()


def shopify_usage(rules, order) -> eod.OrderResult:
    """Shopify lines go through the same client rules as a ShipSidekick Items cell, so kits and
    bundles are counted exactly as the EOD counts them. The SKU goes in brackets at the end, where
    single-product clients' rules read their product code."""
    parts = []
    for item in order.get("items") or []:
        qty = item.get("qty")
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            parts.append(f"0x {_clean(item.get('name')) or _clean(item.get('sku')) or 'item'}")
            continue
        sku = _clean(item.get("sku")).replace("(", "").replace(")", "")
        parts.append(f"{qty}x {_clean(item.get('name'))}" + (f" ({sku})" if sku else ""))
    return rules.order_usage("; ".join(parts))


def _shipments(rules, rows):
    """ShipSidekick labels per order, filtered exactly as the EOD filters them."""
    by_order, seen = defaultdict(list), set()
    for row in rows:
        if (row.get("Organization") or "").strip().lower() != rules.organization.lower():
            continue
        if eod.is_voided(row.get("Voided", "")):
            continue
        tracking = (row.get("Tracking Code") or "").strip()
        if tracking and tracking in seen:
            continue  # the EOD holds repeated tracking codes for review and does not count them
        if tracking:
            seen.add(tracking)
        result = rules.order_usage(row.get("Items", ""))
        by_order[order_key(row.get("Order Name"))].append({
            "day": eod.parse_created_date(row.get("Created Date", "")),
            "usage": Counter({k: v for k, v in result.usage.items() if v}),
            "unknown": list(result.unknown_items),
            "name": (row.get("Order Name") or "").strip(),
        })
    return by_order


def _sheet_column(rules, sheet, day):
    if not sheet or sheet.get("error"):
        return None, ["Sheet: " + ((sheet or {}).get("error") or "not read")]
    shown = sheet_day(sheet.get("as_of"))
    if shown != day:
        return None, [f"Sheet: the Dashboard shows {sheet.get('as_of') or 'no date'}, not this day, so its units sold are not compared"]
    units, notes, unreadable = Counter(), [], set()
    for row in sheet.get("rows") or []:
        sku = ssk_check.match_sheet_row(rules, row.get("product", ""))
        qty = whole_units(row.get("shipped", ""))
        if sku is None:
            notes.append(f"Sheet row not matched to a SKU: {row.get('product')}")
        elif qty is None:
            unreadable.add(sku)  # a pending or error cell is never read as zero
            notes.append(f"Sheet units sold for {row.get('product')} is not a whole number: {row.get('shipped') or 'blank'}")
        else:
            units[sku] += qty
    return {sku: (None if sku in unreadable else units.get(sku, 0)) for sku in rules.skus}, notes


def compare(client_id, day, shopify, sales_rows, sheet):
    """`shopify` is shopify_source.read_day_orders() for window(day); `sales_rows` the Daily Sales
    rows (eod.REQUIRED_COLUMNS only); `sheet` the client's Dashboard read (or None)."""
    rules = client_rules.package(client_id)
    if rules is None:
        return {"client_id": client_id, "error_code": "no_rules", "error": "No SKU rules are defined for this client yet."}
    notes = []
    try:
        reports = eod.build_eod(sales_rows, rules)
        shipments = _shipments(rules, sales_rows)
    except ValueError:
        return {"client_id": client_id, "error_code": "sales_dates",
                "error": "A Daily Sales Created Date could not be read; nothing was compared."}
    report = reports.get(day)
    if report is None:
        latest = max(reports) if reports else None
        notes.append("EOD: Daily Sales has no labels for this day"
                     + (f" (latest is {latest.strftime('%m/%d/%Y')})" if latest else ""))

    orders = shopify.get("orders") or []
    start = window(day)[0]
    oldest = local_day(shopify.get("oldest_read"))
    # Newest first: the chosen day is fully read once anything older than it was reached.
    day_complete = bool(shopify.get("complete")) or (oldest is not None and oldest < day)
    window_complete = bool(shopify.get("complete"))
    if not day_complete:
        notes.append("Shopify: more orders than could be read for this day; the Shopify column is incomplete")
    elif not window_complete:
        notes.append(f"Shopify: orders before {oldest.strftime('%m/%d/%Y')} were not read; "
                     "'not found in Shopify' is only checked back to that day")
    if not shopify.get("all_orders") and (datetime.now(ET).date() - day).days > 59:
        notes.append("Shopify: without read_all_orders, Shopify only returns the last 60 days of orders")

    by_key = defaultdict(list)
    for order in orders:
        by_key[order_key(order.get("name"))].append(order)

    ordered, timing = Counter(), Counter()
    found = {kind: [] for kind in EXCEPTIONS + TIMING}
    counts = Counter()

    def add(kind, name, detail="", units=None):
        counts[kind] += 1
        if len(found[kind]) < LIST_LIMIT:
            found[kind].append({"order": name, "detail": detail, "units": dict(units or {})})

    for order in orders:
        if local_day(order.get("created_at")) != day:
            continue
        name, key = order.get("name") or "", order_key(order.get("name"))
        labels = shipments.get(key, [])
        if order.get("test"):
            counts["test_orders"] += 1
            continue
        if order.get("cancelled_at"):
            counts["cancelled_orders"] += 1
            if labels:
                add("cancelled_but_shipped", name)
            continue
        counts["orders"] += 1
        usage = shopify_usage(rules, order)
        shop = Counter({k: v for k, v in usage.usage.items() if v})
        ordered.update(shop)
        if usage.unknown_items:
            add("unmapped", name, "Shopify: " + ", ".join(usage.unknown_items))
        if order.get("items_truncated"):
            add("items_truncated", name)
        if any(item.get("changed") for item in order.get("items") or []):
            add("edited", name)
        if len(by_key[key]) > 1:
            add("several_orders", name)
            continue
        if not labels:
            fulfilled = str(order.get("fulfillment") or "").upper() == "FULFILLED"
            if fulfilled:
                add("fulfilled_not_in_ssk", name, units=shop)
            else:
                add("not_in_ssk", name, units=shop)
            timing.subtract(shop)  # ordered today, not shipped today
            continue
        financial = str(order.get("financial") or "").upper()
        if financial in ("REFUNDED", "PARTIALLY_REFUNDED"):
            add("refunded_after_shipping", name, financial.replace("_", " ").lower())
        on_day = sum((label["usage"] for label in labels if label["day"] == day), Counter())
        later = [label for label in labels if label["day"] > day]
        shipped = sum((label["usage"] for label in labels), Counter())
        unknown = [item for label in labels for item in label["unknown"]]
        if unknown:
            add("unmapped", name, "ShipSidekick: " + ", ".join(unknown))
        comparable = not (usage.unknown_items or unknown or order.get("items_truncated"))
        if comparable and shipped != shop:
            add("quantity_mismatch", name, units={k: shipped.get(k, 0) - shop.get(k, 0)
                                                  for k in set(shipped) | set(shop)})
        if later:
            rest = shop - on_day
            timing.subtract(rest)
            add("shipped_later", name, "Shipped " + ", ".join(sorted({l["day"].strftime("%m/%d/%Y") for l in later})),
                units=rest)

    for key, labels in shipments.items():
        on_day = sum((label["usage"] for label in labels if label["day"] == day), Counter())
        if not any(label["day"] == day for label in labels):
            continue
        name = labels[0]["name"] or "(no order name)"
        matches = by_key.get(key, [])
        if not matches:
            if window_complete:
                add("not_in_shopify", name, f"No Shopify order created {start.strftime('%m/%d/%Y')}–{day.strftime('%m/%d/%Y')}",
                    units=on_day)
            else:
                counts["not_checked"] += 1
            continue
        created = [local_day(o.get("created_at")) for o in matches]
        if len(matches) == 1 and created[0] and created[0] < day:
            if matches[0].get("cancelled_at"):
                add("cancelled_but_shipped", name, "Ordered " + created[0].strftime("%m/%d/%Y"))
                continue
            timing.update(on_day)
            add("ordered_earlier", name, "Ordered " + created[0].strftime("%m/%d/%Y"), units=on_day)

    eod_units = dict(report.usage) if report else None
    sheet_units, sheet_notes = _sheet_column(rules, sheet, day)
    notes += sheet_notes
    rows = []
    for sku in rules.skus:
        shop = ordered.get(sku, 0) if day_complete else None
        shipped = eod_units.get(sku, 0) if eod_units is not None else None
        sold = sheet_units.get(sku) if sheet_units is not None else None
        diff = None if shop is None or shipped is None else shipped - shop
        rows.append({"sku": sku, "label": rules.labels.get(sku, sku), "shopify_ordered": shop, "eod_shipped": shipped,
                     "sheet_sold": sold, "eod_minus_shopify": diff, "timing": timing.get(sku, 0),
                     "unexplained": None if diff is None else diff - timing.get(sku, 0),
                     "sheet_minus_eod": None if sold is None or shipped is None else sold - shipped})
    total = lambda key: None if any(r[key] is None for r in rows) else sum(r[key] for r in rows)
    return {
        "client_id": client_id, "date": day.isoformat(), "timezone": "America/New_York",
        "window_start": start.date().isoformat(), "rows": rows,
        "totals": {k: total(k) for k in ("shopify_ordered", "eod_shipped", "sheet_sold", "eod_minus_shopify",
                                         "timing", "unexplained", "sheet_minus_eod")},
        "counts": {"orders": counts["orders"], "cancelled_orders": counts["cancelled_orders"],
                   "test_orders": counts["test_orders"], "not_checked": counts["not_checked"],
                   "eod_orders": report.orders if report else 0,
                   **{kind: counts[kind] for kind in EXCEPTIONS + TIMING}},
        "exceptions": [{"type": kind, "text": TEXT[kind], "count": counts[kind], "orders": found[kind]}
                       for kind in EXCEPTIONS if counts[kind]],
        "timing_orders": [{"type": kind, "text": TEXT[kind], "count": counts[kind], "orders": found[kind]}
                          for kind in TIMING if counts[kind]],
        "notes": notes, "rules_status": rules.status,
        "shopify_fetched_at": shopify.get("fetched_at"),
        "sheet_as_of": (sheet or {}).get("as_of"),
    }


# ---- Background reads -------------------------------------------------------------------------
# A day of Shopify orders can take longer than one web request may run (Shopify paces reads), so
# the read runs in a background thread and the page polls. Results hold no customer fields.

DONE_SECONDS = 600     # a finished comparison is reused for 10 minutes (3 for today)
TODAY_SECONDS = 180
FAILED_SECONDS = 30
MAX_RUNNING = 3        # at most three Shopify day reads at once, across all clients
_jobs = {}
_jobs_lock = threading.Lock()
log = logging.getLogger(__name__)


def _read_and_compare(client_id, day, job):
    sources, credentials = inventory.source_config()
    sheet_id = sources.get(client_id)
    if not (isinstance(sheet_id, str) and inventory.SHEET_ID.fullmatch(sheet_id)):
        raise inventory.SourceError("not_configured")
    start, end = window(day)
    shopify = shopify_source.read_day_orders(client_id, start, end,
                                             progress=lambda n: job.update(orders_read=n))
    rows = ledger_sources.SheetReader(sheet_id, credentials).daily_sales()
    sheet = inventory.read_dashboards([client_id])[0]
    return compare(client_id, day, shopify, rows, sheet)


def _run(key, job):
    client_id, day = key
    try:
        result = _read_and_compare(client_id, day, job)
        state = {"status": "done", "result": result}
    except shopify_source.SourceError as error:
        shopify_source.failure(client_id, error.code, error.status)
        state = {"status": "failed", "error": shopify_source.ERRORS.get(error.code, shopify_source.ERRORS["read_failed"])}
    except inventory.SourceError as error:
        log.warning("daily orders sheet failed client=%s code=%s status=%s", client_id, error.code, error.status)
        state = {"status": "failed", "error": ledger_sources.ERRORS.get(error.code, ledger_sources.ERRORS["read_failed"])}
    except Exception as error:  # never log order data: type only
        log.warning("daily orders failed client=%s type=%s", client_id, type(error).__name__)
        state = {"status": "failed", "error": "The comparison could not be completed."}
    with _jobs_lock:
        job.update(state, finished=monotonic())


def status(client_id, day, today, start=threading.Thread):
    """Start (or reuse) the comparison for one client and day; returns the job's public state."""
    key = (client_id, day)
    with _jobs_lock:
        job = _jobs.get(key)
        if job and job["status"] != "running":
            ttl = FAILED_SECONDS if job["status"] == "failed" else TODAY_SECONDS if day >= today else DONE_SECONDS
            if monotonic() - job["finished"] > ttl:
                job = None
        if job is None and sum(v["status"] == "running" for v in _jobs.values()) >= MAX_RUNNING:
            return {"status": "failed", "error": "Other Shopify comparisons are still running. Try again in a minute."}
        if job is None:
            for old in [k for k, v in _jobs.items() if v["status"] != "running" and monotonic() - v["finished"] > DONE_SECONDS]:
                _jobs.pop(old)
            job = _jobs[key] = {"status": "running", "orders_read": 0}
            start(target=_run, args=(key, job), daemon=True).start()
        return {k: job[k] for k in ("status", "orders_read", "result", "error") if k in job}
