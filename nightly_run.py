"""One client's nightly check, end to end (accuracy plan Phase 1, Issue #34 step 4).

Chains the tested checks in the order the plan sets:
  1. eod_check: Sheet "shipped" vs a recount of the ShipSidekick shipments
  2. ssk_check: Sheet "remaining" vs ShipSidekick stock, bound to that stock read
  3. sheet_writes: every value written for the report is in its approved cell
  4. nightly_gate: one VERIFIED / REVIEW / INCOMPLETE result

Pure: the caller passes in what it read (Dashboard, shipments, ShipSidekick store, writes and their
re-read). Nothing is read from or written to Sheets, ShipSidekick or Slack here.
"""
from __future__ import annotations

from datetime import datetime

import eod_check
import nightly_gate
import sheet_writes
import ssk_check


def run_client(client_id: str, *, now: datetime, dashboard: dict, shipments: list[dict] | None,
               store: dict | None, export_complete: bool = False, cutoff_aligned: bool = False,
               no_shipments_confirmed: bool = False, writes: list[dict] | None = None,
               reread: dict | None = None) -> dict:
    """`dashboard` is inventory.parse_dashboard output, `shipments` the day's ShipSidekick export rows
    (None when they could not be read; [] for a read that found none), `store` ssk_source.read_store
    output or its error dict."""
    if dashboard.get("id") != client_id:
        # Another client's Sheet must never verify this one: nothing is checked against it.
        result = nightly_gate.gate(client_id, now=now, eod=None, store=store, compare=None,
                                   export_complete=export_complete, cutoff_aligned=cutoff_aligned)
        result["incomplete"].insert(0, f"The Dashboard read belongs to {dashboard.get('id')!r}, not {client_id!r}.")
        return {**result, "status": nightly_gate.INCOMPLETE, "eod": None, "compare": None, "wrong_row_writes": []}
    # None = the export could not be read: a failed read is never a confirmed empty day.
    missing = "The ShipSidekick shipments could not be read." if shipments is None else ""
    eod = eod_check.check(client_id, dashboard, shipments, no_shipments_confirmed=no_shipments_confirmed,
                          missing_reason=missing)
    compare = None
    if store is not None and not store.get("error_code"):
        compare = ssk_check.compare(client_id, store.get("levels") or [], dashboard,
                                    bool(store.get("truncated")), store.get("fetched_at", ""))
    wrong = sheet_writes.check_writes(client_id, writes or [], reread or {}) if writes else []
    result = nightly_gate.gate(client_id, now=now, eod=eod, store=store, compare=compare,
                               export_complete=export_complete, cutoff_aligned=cutoff_aligned,
                               wrong_row_writes=wrong)
    return {**result, "eod": eod, "compare": compare, "wrong_row_writes": wrong}
