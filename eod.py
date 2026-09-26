"""Client-independent EOD engine for ShipSidekick CSV rows.

Each client supplies a rule package (see client_rules.py). This module never
changes inventory; it produces usage proposals and audit findings per date.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

EXPECTED_ORIGIN = "miami, fl, 33166"
VOID_VALUES = {"yes", "true", "1"}
ITEM_PATTERN = re.compile(r"^\s*(\d+)\s*x\s+(.+)$", re.IGNORECASE)

# Only these ShipSidekick columns are ever read. Customer names and addresses are never loaded.
REQUIRED_COLUMNS = ("Tracking Code", "Created Date", "Organization", "Order Name", "Tracking Status",
                    "Voided", "Mission Num", "Items", "Origin Address")


@dataclass
class OrderResult:
    usage: dict[str, int]
    raw: dict[str, int]
    flags: list[str] = field(default_factory=list)
    unknown_items: list[str] = field(default_factory=list)


@dataclass
class DayReport:
    day: date
    skus: tuple = ()
    orders: int = 0
    missions: int = 0
    loose_shipments: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    usage_by_origin: dict[str, dict[str, int]] = field(default_factory=dict)
    voided_excluded: int = 0
    duplicate_tracking_excluded: list[str] = field(default_factory=list)
    possible_split_orders: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    unknown_items: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.usage:
            self.usage = {s: 0 for s in self.skus}

    @property
    def total_units(self) -> int:
        return sum(self.usage.values())

    @property
    def needs_review(self) -> bool:
        return bool(self.flags or self.unknown_items or self.duplicate_tracking_excluded
                    or len(self.usage_by_origin) > 1)


def parse_items(items: str) -> list[tuple[int, str]]:
    """Split at semicolons; the number before the first x is the quantity."""
    parsed = []
    for part in (items or "").split(";"):
        if not part.strip():
            continue
        match = ITEM_PATTERN.match(part)
        parsed.append((int(match.group(1)), match.group(2).strip()) if match else (0, part.strip()))
    return parsed


def parse_created_date(value: str) -> date:
    value = (value or "").strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized Created Date: {value!r}")


def is_voided(value: str) -> bool:
    return (value or "").strip().lower() in VOID_VALUES


def build_eod(rows: list[dict], rules) -> dict[date, DayReport]:
    """Group valid rows by Created Date and compute each date independently.

    `rules` is a client rule package: .organization, .skus, .order_usage(items).
    """
    reports: dict[date, DayReport] = {}
    seen_tracking: dict[str, tuple] = {}
    order_rows: dict[tuple[date, str], list[tuple[str, str]]] = defaultdict(list)
    missions: dict[date, set] = defaultdict(set)

    for index, row in enumerate(rows, start=2):  # row 1 is the CSV header
        day = parse_created_date(row.get("Created Date", ""))
        report = reports.setdefault(day, DayReport(day=day, skus=rules.skus))

        org = (row.get("Organization") or "").strip()
        if org.lower() != rules.organization.lower():
            report.flags.append(f"Row {index}: organization is {org!r}, not {rules.organization}; excluded.")
            continue
        if is_voided(row.get("Voided", "")):
            report.voided_excluded += 1
            continue

        tracking = (row.get("Tracking Code") or "").strip()
        items = row.get("Items", "")
        if tracking and tracking in seen_tracking:
            report.duplicate_tracking_excluded.append(
                f"Row {index}: tracking {tracking} repeats row {seen_tracking[tracking][0]}; "
                "not counted twice, needs review.")
            continue
        if tracking:
            seen_tracking[tracking] = (index,)

        order = (row.get("Order Name") or "").strip()
        order_rows[(day, order)].append((tracking, items))

        mission = (row.get("Mission Num") or "").strip()
        if mission:
            missions[day].add(mission)
        else:
            report.loose_shipments.append(order or f"row {index}")

        origin = (row.get("Origin Address") or "").strip()
        origin_key = "Miami" if origin.lower().startswith(EXPECTED_ORIGIN) else (origin or "Unknown")

        result = rules.order_usage(items)
        report.orders += 1
        for sku in rules.skus:
            report.usage[sku] += result.usage.get(sku, 0)
            report.usage_by_origin.setdefault(origin_key, {s: 0 for s in rules.skus})
            report.usage_by_origin[origin_key][sku] += result.usage.get(sku, 0)
        report.flags.extend(f"Order {order}: {flag}" for flag in result.flags)
        report.unknown_items.extend(f"Order {order}: {item}" for item in result.unknown_items)

    for (day, order), shipments in order_rows.items():
        if len(shipments) > 1:
            same_items = len({items for _, items in shipments}) == 1
            note = (f"Order {order} has {len(shipments)} tracking codes"
                    + (" with identical items; confirm split shipment vs duplicate." if same_items
                       else "; treated as a split shipment."))
            reports[day].possible_split_orders.append(note)
            if same_items:
                reports[day].flags.append(note)

    for day, report in reports.items():
        report.missions = len(missions[day])
        if len(report.usage_by_origin) > 1:
            report.flags.append("Shipments from more than one origin; totals are split by origin.")
    return reports


def eod_message(report: DayReport, prefix: str = "MUR", labels: dict | None = None) -> str:
    d = report.day.strftime("%m/%d/%Y")
    labels = labels or {}
    lines = [
        f"{prefix} EOD — {d}",
        f"Orders: {report.orders}",
        f"Missions: {report.missions}",
    ]
    lines += [f"{sku} — {labels.get(sku, sku)}: {qty}" for sku, qty in report.usage.items()]
    lines += [
        f"Total units: {report.total_units}",
        "",
    ]
    lines += [f"{d}\t{sku}\t{qty}\tEOD" for sku, qty in report.usage.items() if qty]
    lines.append("")
    lines.append(audit_statement(report))
    return "\n".join(lines)


def audit_statement(report: DayReport) -> str:
    """Only claims what the data confirms."""
    parts = []
    if list(report.usage_by_origin) == ["Miami"]:
        parts.append("All shipments originated in Miami.")
    else:
        parts.append("Origins: " + ", ".join(report.usage_by_origin) + " (flagged).")
    clean = []
    if report.voided_excluded:
        parts.append(f"{report.voided_excluded} voided shipment(s) excluded.")
    else:
        clean.append("voids")
    if report.duplicate_tracking_excluded:
        parts.append(f"{len(report.duplicate_tracking_excluded)} duplicate tracking code(s) held for review.")
    else:
        clean.append("duplicates")
    if report.loose_shipments:
        parts.append(f"{len(report.loose_shipments)} loose shipment(s): {', '.join(report.loose_shipments)}.")
    else:
        clean.append("loose shipments")
    if len(clean) == 3:
        parts.append("No voids, duplicates, or loose shipments were found.")
    elif clean:
        parts.append("No " + " or ".join(clean) + " were found.")
    return " ".join(parts)


