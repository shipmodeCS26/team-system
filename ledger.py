"""Deterministic inventory calculation (blueprint sections 5, 6, 16, 36).

Calculated on hand = approved physical baseline
                     + approved receipts after the baseline
                     - EOD usage after the baseline
                     ± approved adjustments after the baseline

Nothing here is written anywhere. Missing inputs produce INCOMPLETE, never a
guessed number. Negative results are kept and flagged, never raised to zero.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

AFTER = "after_processing"    # e.g. a closing count: that day's orders are already reflected
BEFORE = "before_processing"  # e.g. an opening count: that day's orders still need subtracting


@dataclass
class Baseline:
    sku: str
    quantity: int
    day: date
    timing: str
    approved_by: str
    source: str


@dataclass
class Movement:
    sku: str
    quantity: int          # signed: receipts positive, adjustments either way
    day: date
    kind: str              # RECEIPT or ADJUSTMENT
    approved_by: str
    reference: str = ""


@dataclass
class SkuBalance:
    sku: str
    label: str
    baseline: Baseline | None
    usage: int = 0
    receipts: int = 0
    adjustments: int = 0
    calculated: int | None = None
    status: str = "INCOMPLETE"
    reasons: list[str] = field(default_factory=list)


def applies_after(baseline: Baseline, day: date) -> bool:
    """Only activity after the baseline moment changes the balance."""
    return day > baseline.day if baseline.timing == AFTER else day >= baseline.day


def calculate(client_id: str, rules, eod_reports: dict, baselines: dict[str, Baseline],
              movements: list[Movement], source_errors: list[str] | None = None) -> dict:
    """Per-SKU balances for ONE client. Every input must already belong to this client."""
    source_errors = source_errors or []
    skus = list(dict.fromkeys([*rules.skus, *baselines]))
    results = []
    for sku in skus:
        base = baselines.get(sku)
        row = SkuBalance(sku=sku, label=rules.labels.get(sku, sku), baseline=base)
        if sku not in rules.skus:
            row.reasons.append("No ShipSidekick product mapping for this SKU; its sales cannot be counted.")
        if base is None:
            row.reasons.append("No approved physical count. Balance is not calculated.")
            results.append(row)
            continue
        if base.timing not in (AFTER, BEFORE):
            row.reasons.append(f"Count timing {base.timing!r} is unclear (before or after processing?).")
            results.append(row)
            continue

        review_days = []
        for day, report in sorted(eod_reports.items()):
            if applies_after(base, day):
                row.usage += report.usage.get(sku, 0)
                if report.needs_review:
                    review_days.append(day)
        for move in movements:
            if move.sku != sku or not applies_after(base, move.day):
                continue
            if move.kind == "RECEIPT":
                row.receipts += move.quantity
            else:
                row.adjustments += move.quantity

        row.calculated = base.quantity + row.receipts - row.usage + row.adjustments
        if row.calculated < 0:
            row.reasons.append(f"Calculated balance is {row.calculated}: a discrepancy to verify, "
                               "not a physical count.")
        if review_days:
            row.reasons.append("EOD audit needs review on " +
                               ", ".join(d.strftime("%m/%d") for d in review_days[-5:]) + ".")
        if rules.status != "APPROVED":
            row.reasons.append("Client rules are proposed, not yet approved by ShipMode.")
        row.status = "REVIEW" if (row.reasons or source_errors) else "VERIFIED"
        results.append(row)

    days = sorted(eod_reports)
    return {
        "client_id": client_id,
        "rules_status": rules.status,
        "data_through": days[-1].isoformat() if days else None,
        "source_errors": source_errors,
        "skus": results,
    }
