"""Sheet-write check: every value written for a report must land in its approved cell
(accuracy plan Phase 1, Issue #34 step 4).

A write names what it is for (tab and row key, e.g. Incoming Stocks / "PO-26 status") and the cell
it went to. The check compares that cell with the approved expected-cell map and re-reads it:

- the cell differs from the map → wrong-row write (the G329-instead-of-G328 mistake)
- no approved cell for that key → held (never guessed)
- the re-read value differs from what was written → held

Pure function: the caller does the write and the re-read; this module reads and writes nothing.
The result feeds `nightly_gate.gate(wrong_row_writes=...)`, where any entry makes the client REVIEW.
"""
from __future__ import annotations

# Approved expected-cell maps, per client: {(tab, row key): cell}. None are approved yet (each Sheet's
# layout needs Gly's sign-off), so every report write is held for review until its map exists.
CELL_MAPS: dict[str, dict[tuple[str, str], str]] = {}


def _norm(value) -> str:
    return str(value if value is not None else "").replace(",", "").strip()


def check_writes(client_id: str, writes: list[dict], reread: dict[tuple[str, str], object]) -> list[str]:
    """`writes`: [{"tab", "key", "cell", "value"}]. `reread`: {(tab, cell): value} read back after
    writing. Returns one reason per problem write; an empty list means every write is in place."""
    cells = CELL_MAPS.get(client_id, {})
    problems = []
    for write in writes:
        tab, key, cell = write.get("tab", ""), write.get("key", ""), str(write.get("cell", "")).upper()
        where = f"{tab} {cell}"
        expected = cells.get((tab, key))
        if expected is None:
            problems.append(f"{where} ({key}): no approved cell for this row in {client_id}'s cell map")
            continue
        if cell != expected.upper():
            problems.append(f"{where} (expected {expected.upper()} for {key})")
            continue
        if (tab, cell) not in reread:
            problems.append(f"{where} ({key}): not re-read after writing")
        elif _norm(reread[(tab, cell)]) != _norm(write.get("value")):
            problems.append(f"{where} ({key}): re-read {reread[(tab, cell)]!r}, wrote {write.get('value')!r}")
    return problems
