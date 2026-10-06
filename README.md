# Shipmode operations workspace

Build and release process: [BUILD_WORKFLOW.md](BUILD_WORKFLOW.md).
Full Claude handoff: [SHIPMODE_CLAUDE_BLUEPRINT.md](SHIPMODE_CLAUDE_BLUEPRINT.md).

Six client views: ClarityMD, Fascial. Labs, Muravai, Neurosmile, PuraVita, Onset.
The No Movement and Inventory tabs share one workspace. Inventory reads the
displayed `Dashboard` values from each client's Google Sheet when the private
read-only connection is configured. It refreshes about every minute while open.
It preserves each workbook's as-of date, report status, pending values, and
formula errors; it does not independently calculate or verify balances.
Invoices is reserved for the next phase.

## Current deployment: sample mode

`APP_MODE` defaults to `demo`. All shipments are synthetic `DEMO-*` records.
Client switching, search, carrier/tier filters, pagination, shipment history, and
CSV export work. Imported records and case-note writes are deliberately disabled.
Only the selected-client preference is stored in the browser. No real shipment
or inventory data is stored there or committed to this public repository.

## Google Sheets inventory connection (not yet configured)

The deployed app cannot use a desktop Google Drive connector. Give it its own
Google Cloud service account with the **Sheets API enabled** and share only the
six current client inventory workbooks with that service account as **Viewer**.
Keep the service-account JSON and spreadsheet IDs in private Render environment
settings, never in this public repository:

- `INVENTORY_SHEETS_ENABLED=true`
- `INVENTORY_SERVICE_ACCOUNT_JSON` = complete service-account JSON
- `INVENTORY_SHEETS_JSON` = JSON object mapping `claritymd`, `fascial-labs`,
  `muravai`, `nuerosmile`, `puravita`, and `onset` to their spreadsheet IDs
- `WORKSPACE_USER`, `WORKSPACE_PASSWORD_HASH`, `SECRET_KEY` = private workspace
  authentication settings. The inventory connection fails closed without them.

`nuerosmile` is the existing app's internal ID; its display name is Neurosmile.
`APP_MODE` may remain `demo` for shipment tracking while Inventory reads Sheets.
When inventory is enabled, the whole workspace requires Basic authentication.
The server fetches only bounded `Dashboard!A1:S39` displayed values via the
read-only Sheets scope. It caches each read for at most 45 seconds. The browser
requests new data every 60 seconds while Inventory is open, or when Refresh is
clicked. A failed or unshared sheet is reported per client; no saved numbers are
substituted. A missing or malformed mapping for one client fails only that
client. Each failure carries an `error_code` for staging verification:
`not_configured` (no valid ID mapped), `access_denied` (not shared with the
service account), `not_found` (wrong ID), `no_dashboard` (no readable
`Dashboard!A1:S39`), `layout_changed` (headers not recognized), `unavailable`
(timeout or Google error), or `read_failed`. The server log records the client,
code, and HTTP status only, never spreadsheet IDs or credentials. Cells with
`PENDING` or formula errors are highlighted; a blank as-of date, no product rows,
possible products past row 39, summary errors, negative balances, and reorder
summary mismatches are listed as warnings. Verify the six mappings and access in
a private environment before enabling this on Render.

## Incoming shipments (read-only)

With one client selected, the Inventory tab shows open shipments from that
client's `Incoming Stocks` tab (`/api/incoming?client_id=…`, signed in).
Only these headers are read: PO (Every Row), Tracking (Every Row), Product
(Report Name), Verified SKU, Verified Inventory Units, Status, Forecast
Treatment, Boxes Expected, Boxes Received, Units Received, Received Date, Where
It Is Now, Expected in Miami. A missing header shows `incoming_layout` for that
client only; a workbook without the tab shows "not available".

Rows marked `INCLUDED IN LATEST COUNT` are received history (hidden by default,
never flagged). Other rows are grouped by PO and tracking number and flagged:
missing boxes (received date set, boxes received < expected), receipt not
recorded (status arrived/delivered, boxes received blank), needs transfer, SKU
not verified, and past expected date (nothing received). Incoming totals per
SKU use verified open rows only and are never added to on-hand or to the
calculated panel. Flags change no numbers; receiving entry needs approved
receipt rules first.

## Daily update draft (nothing is sent)

With one client selected and its Dashboard loaded, **Copy daily update** opens
a review dialog with Slack-ready text built from the displayed Sheet values
(`/api/daily-update?client_id=…`, signed in): one line per product with units,
days of cover, and reorder status; products out of stock now; the earliest
product to run out (an estimate at the Sheet's daily demand); and open incoming
shipments with their flags. A REVIEW report or any warning puts a
`DRAFT (source under review)` line first. Incoming shipments without a verified
SKU or quantity are left out of the text and named in the dialog. The app has no
Slack access; staff copy the text and post it themselves.

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
Tests: `python -B -m unittest -v test_tracking test_inventory test_ledger test_muravai_rules test_shopify test_ssk test_frontend` (test_frontend runs `node --test test_panels.js` when Node.js is installed).

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
| Neurosmile | `/api/shipsidekick/nuerosmile` | `SSK_WEBHOOK_SECRET_NUEROSMILE` |
| PuraVita | `/api/shipsidekick/puravita` | `SSK_WEBHOOK_SECRET_PURAVITA` |
| Onset | `/api/shipsidekick/onset` | `SSK_WEBHOOK_SECRET_ONSET` |

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

## Shopify SKU mapping (read-only, off by default)

Setup and safety rules: [docs/SHOPIFY_SETUP.md](docs/SHOPIFY_SETUP.md). With
`SHOPIFY_ENABLED=true` and `SHOPIFY_STORES_JSON` set privately, the Inventory tab
shows each Shopify variant matched to the client's ShipSidekick code and internal
SKU, with blank, duplicate, draft/archived and unmapped SKUs flagged. ShipMode
never writes to Shopify: only allowlisted read queries can be sent, and a token
with any write scope is refused. Enabling Shopify makes the whole workspace
require sign-in.

## ShipSidekick API stock (read-only, off by default)

`ssk_source.py` reads each store's `GET /inventory/levels` with that store's own
API key and shows ShipSidekick available / committed / incoming / damaged next to
the Sheet's Remaining (Inventory tab, "ShipSidekick stock vs. Sheet"). ShipMode
never writes to ShipSidekick: only GET requests to an allowlist of read paths on
ShipSidekick's own hosts can be sent. Private Render settings:

- `SSK_API_ENABLED=true` (also makes the whole workspace require sign-in)
- `SSK_API_KEY_CLARITYMD`, `SSK_API_KEY_FASCIAL_LABS`, `SSK_API_KEY_MURAVAI`,
  `SSK_API_KEY_NUEROSMILE`, `SSK_API_KEY_PURAVITA`, `SSK_API_KEY_ONSET`
- optional `SSK_API_BASE=https://test.shipsidekick.com/api/v1` to read the test environment

A store without a key shows "not configured"; the others still load. Which
ShipSidekick number should equal the Sheet's Remaining is undecided (#16), so
both differences are shown.

No Movement from ShipSidekick (#17): with `SSK_API_ENABLED=true` (and not
`APP_MODE=live`), the No Movement queue shows each store's real shipments
instead of sample data. For each store with a key it reads, GET only, shipments
created in the last `SSK_SHIPMENT_DAYS` days (default 30, max 90) whose tracking
status is not delivered, one status at a time. Each status is checked against
what comes back: if the status filter is ignored, the store shows an error
instead of a partial queue. The existing aging rules apply unchanged: only
physical carrier scans reset the clock, and label-only shipments fall back to
the label date. Addresses, prices and label files are never copied into a row; line
items keep only SKU, product name and quantity (for the Shopify order check). Follow-up notes stay off until the database exists.

Shopify order behind a shipment (#13): with `SHOPIFY_ENABLED=true` and the
ShipSidekick queue on, opening a shipment in No Movement offers "Show Shopify
order and address". It reads that one order by its exact name (read-only, needs
`read_orders`; the address also needs Shopify protected customer data access) and shows payment and
fulfillment status, items on both sides, and the current ship-to address. Flags
(not found, cancelled, refunded, items differ, several shipments) are for review
only and never change a shipment's age or priority. The address is shown only when
Shopify can search all orders (`read_all_orders`), so an older order with the same
name can never be mistaken for it. The address is never cached,
logged, exported or stored, and is cleared from the page when the panel closes.

## Calculated inventory (shadow check)

See `docs/V1_PLAN.md`. Off by default; set `INVENTORY_LEDGER_ENABLED=true` in the private Render settings after the Sheets connection works. Approved counts for clients without a Manual Counts tab go in `INVENTORY_BASELINES_JSON` (format in the plan). Only nine ShipSidekick columns are read; customer names and addresses are never requested.
