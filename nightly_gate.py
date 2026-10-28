"""Nightly check gate: one result per client per night (accuracy plan Phase 1, Issue #34).

Combines the repo's tested checks (eod_check: Sheet shipped vs ShipSidekick recount; ssk_check:
Sheet remaining vs ShipSidekick stock) with the gate rules into one overall result:

- INCOMPLETE: a source is missing, cut off, from the test environment, not aligned to the
  end-of-day cutoff, or the Sheet date is stale; also no rule package.
- REVIEW: any difference or unknown, a PROPOSED package, a future date, no approved warehouse scope.
- VERIFIED: every check passed, the package is APPROVED and the Sheet date is yesterday (ET).

INCOMPLETE wins over REVIEW, and REVIEW wins over VERIFIED. Pure function: reads nothing and
writes nothing; the caller passes in what it read.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import client_rules

VERIFIED, REVIEW, INCOMPLETE = "VERIFIED", "REVIEW", "INCOMPLETE"
ET = ZoneInfo("America/New_York")

# Open business rule (plan section 6, item 7): which ShipSidekick locations each client's Dashboard
# represents. Not set for any client until Carlos approves it, so no client can be VERIFIED yet.
WAREHOUSE_SCOPES: dict[str, tuple[str, ...]] = {}

SSK_ERRORS = {"not_configured": "no ShipSidekick key"}


def expected_sheet_date(now: datetime) -> date:
    """The Sheet should be current to yesterday in America/New_York."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(ET).date()
    return date.fromordinal(local.toordinal() - 1)


def gate(client_id: str, *, now: datetime, eod: dict | None, store: dict | None, compare: dict | None,
         export_complete: bool = False, cutoff_aligned: bool = False,
         wrong_row_writes: list[str] | None = None) -> dict:
    """`eod` is eod_check.check output, `store` is ssk_source.read_store output (or its error dict),
    `compare` is ssk_check.compare output. `export_complete` is True only with evidence that the
    shipment export is whole (SSK pages read to the end, or a row/count manifest). `cutoff_aligned`
    is True only when the SSK balance was rolled back to the end-of-day cutoff."""
    incomplete, review = [], []
    rules = client_rules.package(client_id)
    expected = expected_sheet_date(now)

    if rules is None:
        incomplete.append("No rule package exists for this client.")
    elif rules.status == "PROPOSED":
        review.append("Rule package is PROPOSED, not approved.")
    elif rules.status != "APPROVED":
        review.append(f"Rule package status is {rules.status!r}, not APPROVED.")

    if not WAREHOUSE_SCOPES.get(client_id):
        review.append("No approved ShipSidekick warehouse scope for this client.")

    # Sheet date (from the Dashboard as-of the EOD check read).
    sheet_day = date.fromisoformat(eod["report_date"]) if eod and eod.get("report_date") else None
    if sheet_day is None:
        incomplete.append("Sheet as-of date is blank or unreadable.")
    elif sheet_day < expected:
        incomplete.append(f"Sheet is stale: dated {sheet_day:%d %b %Y}, expected {expected:%d %b %Y}.")
    elif sheet_day > expected:
        review.append(f"Sheet is dated in the future: {sheet_day:%d %b %Y}, expected {expected:%d %b %Y}.")

    # Shipped vs ShipSidekick recount.
    if eod is None:
        incomplete.append("The EOD check did not run.")
    elif eod.get("status") == INCOMPLETE:
        incomplete += [f"EOD check: {reason}" for reason in eod.get("reasons") or ["incomplete"]]
    elif eod.get("status") == REVIEW:
        review += [f"EOD check: {reason}" for reason in eod.get("reasons") or ["review"]]
    elif eod.get("status") != VERIFIED:
        incomplete.append(f"EOD check returned {eod.get('status')!r}.")
    if eod is not None and not export_complete and not eod.get("no_shipments_confirmed"):
        incomplete.append("No evidence the shipment export is complete (truncated).")

    # Remaining vs ShipSidekick stock.
    if store is None:
        incomplete.append("ShipSidekick stock was not read.")
    elif store.get("error_code"):
        incomplete.append(f"ShipSidekick: {SSK_ERRORS.get(store['error_code'], store.get('error') or store['error_code'])}.")
    else:
        if store.get("environment") != "production":
            incomplete.append("ShipSidekick data is from the test environment.")
        if store.get("truncated"):
            incomplete.append("ShipSidekick stock list was cut off (truncated).")
        if not cutoff_aligned:
            incomplete.append("ShipSidekick stock is not aligned to the end-of-day cutoff.")
    if compare is not None and not (store or {}).get("error_code"):
        for row in compare.get("skus") or []:
            if row.get("status") != "MATCH":
                notes = "; ".join(row.get("notes") or []) or "differs from the Sheet"
                review.append(f"Stock {row.get('sku')}: {row.get('status')} ({notes}).")
        for item in compare.get("unmatched_ssk") or []:
            review.append(f"ShipSidekick SKU {item.get('sku') or item.get('title')!r} is not in the rules.")
        for product in compare.get("unmatched_sheet") or []:
            review.append(f"Sheet product {product!r} is not in the rules.")
        if compare.get("blank_skus"):
            review.append(f"{compare['blank_skus']} ShipSidekick item(s) have no SKU.")
        for sku in compare.get("duplicate_skus") or []:
            review.append(f"ShipSidekick SKU {sku} appears more than once.")
    elif store is not None and not store.get("error_code"):
        incomplete.append("The stock comparison did not run.")

    review += [f"Wrong-row write: {cell}" for cell in wrong_row_writes or []]

    status = INCOMPLETE if incomplete else REVIEW if review else VERIFIED
    return {"client_id": client_id, "status": status, "expected_sheet_date": expected.isoformat(),
            "sheet_date": sheet_day.isoformat() if sheet_day else "",
            "rules": rules.status if rules else "NONE", "incomplete": incomplete, "review": review}
