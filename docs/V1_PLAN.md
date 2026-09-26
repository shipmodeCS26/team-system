# V1 plan and status

Source of truth for scope: `SHIPMODE_CLAUDE_BLUEPRINT.md` (vision) and this file (what V1 builds now).
Process: `FACTORY_WORKFLOW.md`. Business rules awaiting approval: Carlos's decision sheet.

## V1 goal

A read-only, signed-in workspace where ShipMode can see, per client and SKU, a **calculated** balance
(approved count + approved receipts − EOD usage ± approved adjustments) next to what each client Sheet
shows, until the two match every day for two weeks (shadow mode).

## Built in this change

| Blueprint area | What exists | Where |
|---|---|---|
| Rule isolation (s.7–8) | One rule package per client; unknown clients get no rules; SKUs never shared (tested) | `client_rules.py` |
| Muravai rules (s.8) | Approved Muravai EOD rules, golden test = verified Sep 1 EOD (681 units) | `muravai_rules.py`, `docs/clients/muravai/RULES.md` |
| Daily fulfillment (s.20–21) | ShipSidekick export → per-date usage with void, duplicate, loose-shipment, origin, org audits | `eod.py` |
| Ledger + counts (s.5, 6, 16) | Deterministic balance; count timing before/after processing; negatives kept; no count → INCOMPLETE | `ledger.py` |
| Report states (s.36) | VERIFIED only when rules approved, count approved, audits clean, balance ≥ 0 | `ledger.py` |
| Sources (s.44, 48) | Reads only 9 required export columns (no customer names/addresses) and approved rows; Sheets stay read-only | `ledger_sources.py` |
| Shadow validation (s.81) | Calculated panel under the Sheet values on the Inventory tab | `templates/workspace.html`, `static/workspace.js` |
| Safety (Factory s.8) | Endpoint off unless `INVENTORY_LEDGER_ENABLED=true`; sign-in required; reports `writes: disabled` | `app.py` |

## Settings (Render environment, never Git)

- `INVENTORY_LEDGER_ENABLED=true` — turns on the calculated panel (also needs the Sheets settings).
- `INVENTORY_BASELINES_JSON` — approved counts for clients without a Manual Counts tab:
  `{"fascial-labs": {"FAS001": {"quantity": 10990, "date": "2026-09-03", "timing": "after_processing", "approved_by": "Carlos"}}}`

## Client status

| Client | Rules | Approved count | Blocking |
|---|---|---|---|
| Muravai | APPROVED | Manual Counts tab | Initial Stock tab disagrees with approved counts (decision sheet 4b) |
| Fascial Labs | PROPOSED (FASCSUPP-1 → FAS001) | none | Three conflicting starting numbers; receipt damage unknown |
| PuraVita | PROPOSED (CAP-MAGNESIUM-360 → PVT001) | none | Count and rule approval |
| NeuroSmile | PROPOSED (NEURO-120 → NEU001) | none | NEU002 ShipSidekick code unknown |
| ClarityMD, Onset | none | none | Sheets not supplied |

## Next, in order (each its own Issue)

1. Staging service + verify all four Sheets load with the service account (real-data check of the Sep 1 Muravai EOD).
2. Google sign-in replacing the shared password.
3. Carlos approves decision sheet → enter counts in `INVENTORY_BASELINES_JSON`, mark rule packages APPROVED.
4. PostgreSQL event ledger (append-only events, idempotency keys, approvals, audit log) replacing Sheet-held approvals.
5. Receiving and physical-count entry screens (internal writes only; external writes stay off).
6. Forecast / coverage per client profile.
