# Shipmode operations workspace

Five client views: ClarityMD, Fascial. Labs, Muravai, Nuerosmile, PuraVita.
The initial focus is No Movement. Inventory follows the supplied report columns;
Invoices is reserved for the next phase. Neither module currently imports balances.

## Current deployment: sample mode

`APP_MODE` defaults to `demo`. All shipments are synthetic `DEMO-*` records.
Client switching, search, carrier/tier filters, pagination, shipment history, and
CSV export work. Imported records and case-note writes are deliberately disabled.
Only the selected-client preference is stored in the browser. No real shipment
data is stored there or committed to this public repository.

## Aging rules

- 5–6 complete 24-hour days: Watch; 7–9: Urgent; 10 and above: Critical.
- Never remove a shipment simply because it passed ten days.
- Use the latest physical carrier event, never an order fulfillment timestamp.
- Pre-transit without a physical scan uses shipped date, or label-created date
  when shipment date is absent. The interface identifies that fallback.
- In-transit/unknown statuses without physical scan timestamps go to Missing data.
- Carrier-confirmed delivered and cancelled labels do not enter the exception queue.
- Investigation status does not clear a carrier exception.
- Receiving a webhook does not itself reset the movement clock.

## Run and test

Install `requirements.txt`, then run `flask --app app run` for development.
Render build: `pip install -r requirements.txt`.
Render start: `gunicorn app:app --bind 0.0.0.0:$PORT`.
Health check: `/api/health`. Auto-deploy: On Commit.
Tests: `python -B -m unittest -v test_tracking`.

## Live mode prerequisites (not activated)

1. Provision a persistent PostgreSQL database and choose reliable always-on
   hosting for webhook reception. The current free service sleeps when idle.
   Review recurring costs with the workspace owner first.
2. Set `DATABASE_URL`, `SECRET_KEY` (long random value), `WORKSPACE_USER`, and
   `WORKSPACE_PASSWORD_HASH` (Werkzeug-generated password hash) privately in
   Render environment settings. Do not commit credentials or send them in chat.
   Owner must enter their own authentication credential. This version uses one
   HTTPS Basic-auth workspace login; it does not implement separate staff roles.
   Add appropriate rate limiting/SSO before a broader staff rollout.
3. Run `flask --app app init-db` once in the target environment. Verify the database
   backup/restore policy and persistence across deploys before importing real data.
4. Set `APP_MODE=live`. Missing access/storage settings cause the live app to fail
   closed. Browser write requests additionally require a session CSRF token.
5. Test with synthetic data end-to-end before loading client records. PostgreSQL
   transaction paths require integration testing against the chosen database.

## Initial imports

Select one client per file. The parser accepts the supplied ShipSidekick export
headers (`Tracking Code`, `Created Date`, `Organization`, `Order Name`, `Carrier`,
`Tracking Status`, `Voided`, `Mission Num`, `Additional Tracking Codes`) or the
downloadable normalized CSV template. Recipient names, addresses, and billing
columns are discarded. A mismatched Organization is rejected.

`Created Date` is retained as label date, not shipping or last movement. Date-only
US-format values are interpreted at UTC midnight and marked report-date precision;
confirm the source timezone and prefer ISO timestamps before operational use.
No scan timestamp is invented from a status. Additional tracking codes require
one template row per package; the entire file is rejected if expansion is needed.
Duplicate carrier/tracking identities within a file are rejected. Re-imports
upsert on client + carrier + tracking number while preserving follow-up and newer
webhook evidence. Imports are transactional and limited to 10,000 rows / 5 MB.

## ShipSidekick integration adapter (not connected)

Official reference: https://apidocs.shipsidekick.com/docs/guides/webhooks

The receiver implements the documented EasyPost-compatible `tracker.created` and
`tracker.updated` shape. Per-client endpoints:

| Client | Endpoint | Secret environment variable |
|---|---|---|
| ClarityMD | `/api/shipsidekick/claritymd` | `SSK_WEBHOOK_SECRET_CLARITYMD` |
| Fascial. Labs | `/api/shipsidekick/fascial-labs` | `SSK_WEBHOOK_SECRET_FASCIAL_LABS` |
| Muravai | `/api/shipsidekick/muravai` | `SSK_WEBHOOK_SECRET_MURAVAI` |
| Nuerosmile | `/api/shipsidekick/nuerosmile` | `SSK_WEBHOOK_SECRET_NUEROSMILE` |
| PuraVita | `/api/shipsidekick/puravita` | `SSK_WEBHOOK_SECRET_PURAVITA` |

Use a distinct secret and correct ShipSidekick organization per endpoint.
The server verifies `X-SSK-Signature` using HMAC-SHA256 over the exact raw body.
Only production-mode events enter the live queue. Event IDs are deduplicated in
the same transaction as shipment updates; out-of-order snapshots do not regress
carrier state. Unsupported payloads fail explicitly, rather than guessing fields.
No raw webhook payload or recipient address is retained. There is no carrier API
key configured and no direct carrier polling running.

Before activating: validate a redacted actual webhook payload and organization
mapping; backfill every open shipment; test duplicates and out-of-order events;
test notes and imports against PostgreSQL; configure periodic reconciliation
against ShipSidekick/carrier records so missed webhooks are recovered. A dashboard
refresh recalculates age while open; it is not a background alerting service.
No email, Slack alerts, scheduler, or guaranteed shipment coverage is active.

## Inventory reference findings

The supplied three inventory reports use product, starting stock, units sold,
remaining stock, demand, days of cover, projected stock, reorder status and order
quantity. Muravai also has receipts, physical counts, adjustments/reships and
audit exceptions. Some displayed summary statuses and calculation guides disagree;
validate the business formulas rather than blindly porting those cells. Preserve
the original Sheets as read-only references until a separate migration is agreed.
