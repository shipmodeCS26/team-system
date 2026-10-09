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
    "test_order_shipped": "Shopify test order, but a label exists",
    "items_truncated": "Order has more lines than were read; items not compared",
    "shipped_later": "Ordered on this day, shipped on a later day (timing)",
    "ordered_earlier": "Shipped on this day, ordered on an earlier day (timing)",
    "partly_shipped": "Ordered on this day, partly shipped; the rest is not shipped yet (timing)",
}
EXCEPTIONS = ("not_in_ssk", "fulfilled_not_in_ssk", "not_in_shopify", "quantity_mismatch", "unmapped",
              "cancelled_but_shipped", "test_order_shipped", "refunded_after_shipping", "several_orders",
              "items_truncated", "edited")
SHIPPED_STATUSES = ("FULFILLED", "PARTIALLY_FULFILLED")
LOOKUP_LIMIT = 40  # shipped orders older than the window are looked up by name, at most this many
TIMING = ("shipped_later", "partly_shipped", "ordered_earlier")

AS_OF_FORMATS = ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d")


def order_key(name) -> str:
    """Shopify '#1234' and a ShipSidekick 'Order Name' of '#1234' or '1234' are the same order.
    Only surrounding spaces and a leading '#' are dropped: 'SM 10' and 'SM10' stay different."""
    return shopify_source._name_key(name)


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
    if sheet.get("may_continue"):
        # Rows past the Dashboard's last read row are missing: a SKU there would read as zero.
        return None, ["Sheet: the Dashboard may continue past the rows read, so its units sold are not compared"]
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


def _window_state(day, shopify, today):
    oldest = local_day(shopify.get("oldest_read"))
    # Newest first: the chosen day is fully read once anything older than it was reached.
    day_complete = bool(shopify.get("complete")) or (oldest is not None and oldest < day)
    window_complete = bool(shopify.get("complete"))
    # Without read_all_orders Shopify returns only its last 60 days: an empty answer for an older
    # day is not proof that there were no orders.
    hidden = not shopify.get("all_orders") and (today - window(day)[0].date()).days > 59
    return day_complete and not hidden, window_complete and not hidden, oldest, hidden


def lookup_names(client_id, day, shopify, sales_rows):
    """Names of orders shipped on the day that are not in the Shopify window: they are looked up
    one by one (an order can be placed long before it ships)."""
    rules = client_rules.package(client_id)
    if rules is None:
        return []
    try:
        shipments = _shipments(rules, sales_rows)
    except ValueError:
        return []
    known = {order_key(order.get("name")) for order in shopify.get("orders") or []}
    names = [labels[0]["name"] for key, labels in shipments.items()
             if key and key not in known and any(label["day"] == day for label in labels) and labels[0]["name"]]
    return names[:LOOKUP_LIMIT]


def compare(client_id, day, shopify, sales_rows, sheet, lookups=None, today=None):
    """`shopify` is shopify_source.read_day_orders() for window(day); `sales_rows` the Daily Sales
    rows (eod.REQUIRED_COLUMNS only); `sheet` the client's Dashboard read (or None); `lookups` is
    shopify_source.find_orders() for lookup_names()."""
    rules = client_rules.package(client_id)
    if rules is None:
        return {"client_id": client_id, "error_code": "no_rules", "error": "No SKU rules are defined for this client yet."}
    today = today or datetime.now(ET).date()
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
    day_complete, window_complete, oldest, hidden = _window_state(day, shopify, today)
    if hidden:
        notes.append("Shopify: without read_all_orders, Shopify only returns its last 60 days of orders, "
                     "so this day cannot be compared")
    elif not day_complete:
        notes.append("Shopify: more orders than could be read for this day; the Shopify column is incomplete")
    elif not window_complete:
        notes.append(f"Shopify: orders before {oldest.strftime('%m/%d/%Y')} were not read in the day window")

    by_key = defaultdict(list)
    for order in orders:
        by_key[order_key(order.get("name"))].append(order)

    ordered, timing = Counter(), Counter()
    found = {kind: [] for kind in EXCEPTIONS + TIMING}
    counts = Counter()
    truncated = False

    def add(kind, name, detail="", units=None):
        counts[kind] += 1
        if len(found[kind]) < LIST_LIMIT:
            found[kind].append({"order": name, "detail": detail, "units": {k: v for k, v in dict(units or {}).items() if v}})

    for order in orders:
        if local_day(order.get("created_at")) != day:
            continue
        name, key = order.get("name") or "", order_key(order.get("name"))
        labels = shipments.get(key, [])
        if order.get("test"):
            counts["test_orders"] += 1
            if labels:
                add("test_order_shipped", name)
            continue
        if order.get("cancelled_at"):
            counts["cancelled_orders"] += 1
            if labels:
                add("cancelled_but_shipped", name)
            continue
        if order.get("digital_only"):
            counts["digital_orders"] += 1  # gift cards and digital goods never get a label
            continue
        counts["orders"] += 1
        usage = shopify_usage(rules, order)
        shop = Counter({k: v for k, v in usage.usage.items() if v})
        ordered.update(shop)
        if usage.unknown_items:
            add("unmapped", name, "Shopify: " + ", ".join(usage.unknown_items))
        if order.get("items_truncated"):
            truncated = True  # the order's later lines were not read: Shopify totals are not shown
            add("items_truncated", name)
        if any(item.get("changed") for item in order.get("items") or []):
            add("edited", name)
        if len(by_key[key]) > 1:
            add("several_orders", name)
            continue
        if not labels:
            if str(order.get("fulfillment") or "").upper() in SHIPPED_STATUSES:
                # Shopify says it shipped, possibly today: never explained away as timing.
                add("fulfilled_not_in_ssk", name, units=shop)
            else:
                add("not_in_ssk", name, units=shop)
                timing.subtract(shop)  # ordered today, not shipped yet
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
        when = "Shipped " + ", ".join(sorted({l["day"].strftime("%m/%d/%Y") for l in later})) if later else ""
        if comparable:
            extra, missing = shipped - shop, shop - shipped
            fulfilled = str(order.get("fulfillment") or "").upper() == "FULFILLED"
            if extra or (missing and fulfilled):
                add("quantity_mismatch", name, units={k: shipped.get(k, 0) - shop.get(k, 0)
                                                      for k in set(shipped) | set(shop)})
            shipped_later = shop - on_day - missing  # labelled on a later day
            unshipped = missing if not fulfilled else Counter()  # Shopify still owes these units
            if shipped_later:
                timing.subtract(shipped_later)
                add("shipped_later", name, when, units=shipped_later)
            if unshipped:
                timing.subtract(unshipped)
                add("partly_shipped", name, units=unshipped)
        elif later:
            rest = shop - on_day
            timing.subtract(rest)
            add("shipped_later", name, when, units=rest)

    looked = (lookups or {}).get("found") or {}
    all_orders = bool((lookups or {}).get("all_orders"))
    for key, labels in shipments.items():
        if not any(label["day"] == day for label in labels):
            continue
        on_day = sum((label["usage"] for label in labels if label["day"] == day), Counter())
        name = labels[0]["name"] or "(no order name)"
        matches = by_key.get(key, [])
        if not matches:
            result = looked.get(labels[0]["name"])
            if result is None:
                counts["not_checked"] += 1  # not looked up (too many, or not searchable)
                continue
            if not result["search_complete"]:
                counts["not_checked"] += 1  # an unread page could hold another order with this name
                continue
            matches = result["orders"]
            if not matches:
                if all_orders:
                    add("not_in_shopify", name, "No Shopify order has this name", units=on_day)
                else:
                    add("not_in_shopify", name, "Not in Shopify's last 60 days of orders "
                                                "(older orders need read_all_orders)", units=on_day)
                continue
        created = [local_day(o.get("created_at")) for o in matches]
        if day in created:
            continue  # placed on the day: handled above
        if len(matches) > 1:
            add("several_orders", name)
            continue
        match, placed = matches[0], created[0]
        if placed is None or placed > day:
            continue
        when = "Ordered " + placed.strftime("%m/%d/%Y")
        if match.get("test"):
            add("test_order_shipped", name, when)
            continue
        if match.get("cancelled_at"):
            add("cancelled_but_shipped", name, when)
            continue
        if str(match.get("financial") or "").upper() in ("REFUNDED", "PARTIALLY_REFUNDED"):
            add("refunded_after_shipping", name, when + ", " + str(match["financial"]).replace("_", " ").lower())
        usage = shopify_usage(rules, match)
        unknown = [item for label in labels for item in label["unknown"]]
        if usage.unknown_items or unknown or match.get("items_truncated"):
            add("unmapped" if usage.unknown_items or unknown else "items_truncated", name,
                when + (": " + ", ".join(usage.unknown_items + unknown) if usage.unknown_items or unknown else ""))
            continue  # not comparable: nothing is explained as timing
        if any(item.get("changed") for item in match.get("items") or []):
            add("edited", name, when)
            continue  # edited or refunded lines: what is still owed is unknown, so never timing
        shop = Counter({k: v for k, v in usage.usage.items() if v})
        before = sum((label["usage"] for label in labels if label["day"] < day), Counter())
        outstanding = shop - before
        explained = on_day & outstanding  # only units still owed on the order are timing
        excess = on_day - outstanding
        timing.update(explained)
        if explained:
            add("ordered_earlier", name, when, units=explained)
        if excess:
            add("quantity_mismatch", name, when + "; shipped more than was still owed (reship or extra units?)",
                units=excess)

    eod_units = dict(report.usage) if report else None
    sheet_units, sheet_notes = _sheet_column(rules, sheet, day)
    notes += sheet_notes
    if truncated:
        notes.append("Shopify: an order has more lines than were read, so Shopify totals are not shown")
    shop_known = day_complete and not truncated
    rows = []
    for sku in rules.skus:
        shop = ordered.get(sku, 0) if shop_known else None
        shipped = eod_units.get(sku, 0) if eod_units is not None else None
        sold = sheet_units.get(sku) if sheet_units is not None else None
        diff = None if shop is None or shipped is None else shipped - shop
        wait = timing.get(sku, 0) if shop_known else None
        rows.append({"sku": sku, "label": rules.labels.get(sku, sku), "shopify_ordered": shop, "eod_shipped": shipped,
                     "sheet_sold": sold, "eod_minus_shopify": diff, "timing": wait,
                     "unexplained": None if diff is None or wait is None else diff - wait,
                     "sheet_minus_eod": None if sold is None or shipped is None else sold - shipped})
    total = lambda key: None if any(r[key] is None for r in rows) else sum(r[key] for r in rows)
    return {
        "client_id": client_id, "date": day.isoformat(), "timezone": "America/New_York",
        "window_start": start.date().isoformat(), "rows": rows,
        "totals": {k: total(k) for k in ("shopify_ordered", "eod_shipped", "sheet_sold", "eod_minus_shopify",
                                         "timing", "unexplained", "sheet_minus_eod")},
        "counts": {"orders": counts["orders"], "cancelled_orders": counts["cancelled_orders"],
                   "test_orders": counts["test_orders"], "digital_orders": counts["digital_orders"],
                   "not_checked": counts["not_checked"], "eod_orders": report.orders if report else 0,
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
    names = lookup_names(client_id, day, shopify, rows)
    lookups = shopify_source.find_orders(client_id, names) if names else None
    sheet = inventory.read_dashboards([client_id])[0]
    return compare(client_id, day, shopify, rows, sheet, lookups)


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
