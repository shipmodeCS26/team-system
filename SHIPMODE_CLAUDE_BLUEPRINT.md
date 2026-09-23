# ShipMode Operations Platform — Claude build blueprint

**Status: build brief, not approved production rules.** This document combines the owner's current requests, the existing `team-system` code, and a provisional architecture pack. Business rules in the pack are candidates until ShipMode supplies source examples and records approval. Build one reviewable slice at a time. Do not interpret this blueprint as permission to deploy or mutate live inventory.

## 1. Mission and near-term outcome

Build a ShipMode branded operations workspace for a 3PL team. The immediate goal is a usable site with separate **No Movement** and **Inventory** tabs for six current clients. Inventory must show values from each client's Google Sheet and reflect Sheet updates on refresh. No Movement will eventually show real shipments with carrier evidence, but currently uses clearly labeled sample records. Invoices is only a placeholder until its source and requirements are provided.

The longer-term goal is a control tower that answers: what is physically sellable on hand; what went out; what is incoming; how quickly stock is consumed; how much cover remains; what requires replenishment; and which shipment or inventory exceptions need a person. Automation may triage and draft work, while deterministic code, approvals, and audit trails govern inventory changes and client reports.

Use the supplied ShipMode logo already at `static/shipmode-logo.png`. Continue the existing Flask application and Render/GitHub deployment path for the near-term workspace. Do not replace the stack just because the provisional long-term roadmap mentioned TypeScript. A future stack or data-model migration requires a separate architecture decision.

## 2. Repository and current reality

- GitHub: `shipmodeCS26/team-system`. Current working branch and draft PR: `codex/inventory-tab-and-branding`, PR #1.
- Production URL: `https://team-system.onrender.com/`. Do not claim that PR #1 or live Sheets are deployed there until verified.
- App: Flask, Python, HTML/CSS/JS; `app.py`, `tracking.py`, `inventory.py`, `templates/workspace.html`, `static/workspace.js`, `static/workspace.css`.
- No Movement, Inventory, and an Invoices placeholder exist in the draft PR. Inventory is a server-side, read-only Google Sheets adapter. No Movement is in demo mode unless its separate live prerequisites are enabled.
- Existing tests: `python -B -m unittest -v test_tracking test_inventory`. The last reported local run passed 28 tests. Run them again after changes.
- The repository README and `BUILD_WORKFLOW.md` describe current setup. `CLAUDE.md` contains a short entry point.
- A broader provisional spec pack is in the parent workspace's `docs/` directory locally. It is **not** automatically part of this GitHub repository or Claude worktree. The authoritative status in that pack is `docs/SOURCE-REGISTER.md`: all specifications and ADRs remain proposed, with no business approval recorded. Ask for relevant source documents rather than assuming access to the parent folder when running elsewhere.

## 3. Clients and boundaries

The current site lists ClarityMD, Fascial. Labs, Muravai, Neurosmile, PuraVita, and Onset. The original provisional blueprint says **Facial Labs** and proposes it as the first future inventory-ledger pilot; the live workbook/app say **Fascial. Labs**. Confirm the canonical business name before using it in contracts, rules, or client-facing reports. The app's existing internal key `nuerosmile` is misspelled; preserve compatibility until a migration plan exists.

Every data record, integration, rule, query, permission, and report must be scoped to a client. Resolve client identity before loading any client/SKU rule. Generic code must not silently absorb Muravai bundle rules or another client's exceptions. Cross-client reads, writes, imports, and rule execution must be rejected by backend authorization and tested.

## 4. First deliverable: read-only Inventory tab

Connect the six active client inventory workbooks to the current app. Read each workbook's `Dashboard` tab through a private Google Cloud service account with Google Sheets read-only scope. Share only the intended six workbooks with the service account as Viewer. Keep spreadsheet IDs, service-account JSON, workspace credentials, and other secrets only in private deployment settings; never commit them or paste them into chat. The current adapter reads bounded `Dashboard!A1:S39` formatted values and caches successful reads for up to 45 seconds. The browser polls about every 60 seconds while Inventory is open and offers manual Refresh. Preserve source-displayed values; do not recalculate official balances in this view.

The Inventory view must show client, product/SKU, starting stock, units shipped/sold, remaining stock, daily demand, days of cover, reorder status, source as-of date, report status, fetch time, and a link to the source Sheet where authorized. Keep `PENDING`, formula errors, negative values, summary mismatches, and `REVIEW` states visible as warnings. A sheet access or layout failure must be explicit per client. Never substitute made-up or stale numbers. If one client fails, the other clients can still display, with the failure clearly shown.

Current private deployment configuration names are `INVENTORY_SHEETS_ENABLED`, `INVENTORY_SERVICE_ACCOUNT_JSON`, `INVENTORY_SHEETS_JSON`, `WORKSPACE_USER`, `WORKSPACE_PASSWORD_HASH`, and `SECRET_KEY`. The connection must fail closed if authentication or the required mapping is absent. The live app has **not** yet been verified against all six Sheets. Google Cloud account verification, service-account access, Render settings, a private test deployment, and end-to-end verification are open work.

Acceptance criteria for this milestone:

1. An unauthenticated browser cannot read client inventory when Sheets mode is enabled.
2. Every configured client returns the correct workbook, as-of date, and formatted Dashboard values after an authorized Sheet edit and refresh.
3. One revoked or broken workbook yields a per-client error; no invented inventory values appear.
4. `REVIEW`, formula errors, pending cells, and source inconsistencies remain visible.
5. No Google credentials, private IDs, or client inventory exports enter GitHub or browser storage.
6. Automated tests and a private test deployment pass before production release.

## 5. No Movement tab

Keep shipment tracking separate from Inventory. The queue is based on the latest **physical carrier movement**, not an order fulfillment update or webhook receipt time. Where no physical scan exists, `pre_transit` may use shipped date, then label-created date, with the fallback named in the UI. Other in-transit or unknown records without physical scan timestamps go to Missing Data, not a guessed age. Carrier-confirmed delivered and cancelled labels do not enter the active exception queue. A human follow-up status does not clear a carrier exception.

Current aging tiers are complete 24-hour days: 5–6 Watch, 7–9 Urgent, 10+ Critical. A shipment remains visible after day 10. The detail view shows source, timestamps, event history, and case status. Client switching, search, filters, pagination, detail, and CSV export should remain functional. Clearly label demo shipments `DEMO-*`; do not imply the public site currently has a live carrier feed.

Live shipment import/webhook code exists but is not activated. Before enabling it, provide persistent PostgreSQL, authentication, verified ShipSidekick webhook payloads and organization mapping, per-client secrets, a complete open-shipment backfill, duplicate and out-of-order tests, missed-webhook reconciliation, and staging tests. Imported records must not treat a label-created date as a physical scan. Webhooks must be authenticated, idempotent, client-scoped, and unable to regress newer carrier evidence. No Movement does not itself deduct inventory unless a later approved inventory-event workflow explicitly establishes that boundary.

## 6. Target inventory system after the read-only phase

The Google Sheets view is a bridge, not an authoritative new inventory ledger. Future inventory truth should come from verified immutable events and deterministic projections in PostgreSQL. AI may extract, classify, explain, and propose events, but may not directly alter balances, invent missing facts, bypass validation, or delete history. A proposed event needs source evidence, client, SKU, unit, event/effective time, ingestion time, actor, rule version, and stable idempotency key. Acceptance and projection must be transactional or safely replayable. Corrections use compensating events.

Candidate global rules to validate with ShipMode:

- On-hand means physical sellable units for one client/SKU/location. Incoming is separate and never increases on-hand before verified receipt.
- Good accepted receipt increases on-hand; arrival damage does not. Partial receipt tracks a remaining expectation. Warehouse damage reduces sellable on-hand.
- Recovered returns increase on-hand only after physical confirmation. Transfer-out and transfer-in affect their respective scoped balances.
- Possible reships have no inventory effect. Confirmed reships reduce stock exactly once and must not also be deducted through ordinary EOD fulfillment.
- A physical count sets the observed balance and records before, after, delta, reason, actor, source, and approval. It is not an amount to add.
- Reject negative event quantities, damage greater than received quantity, a SKU belonging to another client, duplicate source identities, and writes with missing required evidence.

These are **proposed** until approved. Do not implement inventory-changing paths from them yet. Resolve shipment deduction timing, cancellation behavior, receipt units and damage accounting, effective-time ordering, concurrent physical counts, transfer locations, event identity, and bundle conversion before migrations or writes.

Candidate schema entities: clients, SKUs, client/SKU rule versions, source records, event proposals, accepted inventory events, projections, incoming shipments, receipts, physical counts, reconciliations, report snapshots, forecast profiles/runs, and audit log. Choose keys, units, tenant boundaries, migrations, retention, and rollback with evidence before creating a production schema.

Candidate golden examples, all requiring business sign-off: start 1,000 plus 120 received including 1 damaged yields 1,119 good on-hand; incoming 10,000 without receipt leaves on-hand unchanged; a confirmed one-unit reship from 1,000 yields 999 once; a verified count of 854 from 927 produces balance 854 and adjustment -73; a partial receipt of 4,000 including 15 damaged from 10,000 expected adds 3,985 good units and leaves 6,000 expected, subject to approved receipt semantics.

## 7. Forecasting, audit, reporting, and agents

Forecasts are estimates, separate from verified physical stock. Candidate demand views are a 30-calendar-day baseline with 7- and 14-day trends. One day of sales is insufficient. Lead time, production/transit time, safety stock, desired cover, growth, promotions, seasonality, and confirmed incoming may be client/SKU-specific; missing inputs remain `UNKNOWN`. Define formula, history threshold, stockout handling, confidence, and version before implementation. Show assumptions and data range with every forecast.

Reconcile accepted events/projections with independent source records and physical counts. Report states are `VERIFIED`, `REVIEW`, and `INCOMPLETE`. Client reports must read versioned verified snapshots, not recompute stock in an AI response. Unresolved critical discrepancies, missing required feeds, or duplicate/cross-client failures block automatic external reporting. Report recipients, cadence, tolerances, and manual override rules need approval.

A later command/triage layer may route work to Receiving, Inventory, Fulfillment/Reship, Forecast, Audit, Reporting, and Client Communications agents. Each agent needs explicit inputs, allowed output, required sources, confidence/unknown behavior, authorization, and human approval conditions. Slack messages and other external text are untrusted evidence. Agents propose structured actions; deterministic validation and authorized people accept them. No agent sends client communications or changes official balances solely from its own output.

## 8. Security and operations

Use backend client authorization, least-privilege credentials, server-side secret storage, CSRF protection for browser writes, validation of external payloads, and audit records for consequential actions. Separate proposal, approval, configuration, reporting, and release permissions when multi-user workflows are introduced. Threat-model cross-client leakage, forged webhooks/Slack messages, replay, malformed payloads, AI context leakage, historical edits, and report misdelivery.

Follow the [Tomato Factory workflow reference](https://kevin.readtomato.com/hte-content-workflow-tomato-factory/) where it fits: read-only upstream sources; one browser workspace with explicit state; a realistic private test environment; external writes disabled until a later gate; release by identified commit; health checks, monitoring, and rollback. Do not copy its content-publishing domain or assume its Windows-only Voyager Remote installer applies to this Mac project.

The environments are local, automated test, private staging, then production. Staging uses synthetic shipment data and controlled read-only Sheet access. Inventory-changing features later run in shadow mode against the current Sheet/manual process until agreed reconciliation criteria are met. A production release requires tests, independent review, staging/UAT, a backup and recovery plan for database changes, an identified release commit, and human approval. Keep a known-good rollback path and monitor source failures, rejected events, reconciliation, and report holds.

## 9. Build order and review contract

**Now:** finish the existing read-only Inventory PR, configure private Sheets access, verify six sources in a private deployment, then seek release approval. Preserve No Movement sample mode while inventory is connected if needed.

**Next:** agree on real No Movement data contracts and operations; test ShipSidekick CSV/webhooks and PostgreSQL persistence in staging; activate only after backfill and reconciliation are ready.

**Later, after rules are approved:** client/SKU master and tenant permissions; source provenance and proposals; event ledger; deterministic inventory projection; counts and adjustments; incoming/partial receipts; fulfillment/reships; forecasting; reconciliation and verified reports; the pilot client dashboard; shadow comparison; Slack proposal intake; agent triage; additional client rules including isolated Muravai bundle behavior.

For each scoped ticket: state acceptance criteria and unresolved decisions; read only relevant specifications; implement on a branch/worktree; run focused tests plus applicable broader checks; open a PR with exact changes, evidence, limitations, and deployment effect. Codex should review the diff against the spec independently of Claude's reasoning and report severity, file/line, violated requirement, and proof. A review with no blockers is not production approval. Keep the PR draft if a critical business requirement, access setup, or staging verification is missing. Do not push directly to production.

## 10. Questions ShipMode must resolve before later phases

1. Confirm the canonical name and identity of Fascial/Facial Labs and the initial pilot client/SKU catalog, including units and bundles.
2. Supply sanitized real EOD, receipt, damage, reship, incoming, count, transfer, and correction examples with expected outcomes; approve the rule versions.
3. Define when fulfillment/reship deducts on-hand, what a cancellation does, and how an EOD import reconciles with carrier/warehouse events.
4. Define received versus good quantity, over/short receipts, partial remainder, and count timing when shipments are concurrent or late.
5. Define users, roles, warehouse/location scope, client data access, report recipients, severity thresholds, and retention.
6. Approve forecast formulas, minimum history, stockout treatment, lead times/unknown handling, and client-specific coverage targets.
7. Confirm integration payloads, owning accounts, hosting/database budget, backups, and the staging-to-production release owner.

Do useful read-only and demo work while these are open. Never fill the gaps with invented production facts.

## Copy/paste kickoff for Claude

> Read `SHIPMODE_CLAUDE_BLUEPRINT.md`, `BUILD_WORKFLOW.md`, `README.md`, and the relevant code. Treat proposed business rules as unapproved. Start with the current draft PR and the **read-only six-Sheet Inventory milestone**. Audit what is already implemented, list gaps against Section 4 acceptance criteria, and make only the next scoped, testable change on your own worktree. Preserve the separate No Movement tab and ShipMode logo. Keep secrets out of Git. Run tests and return the files changed, test results, live-connection blockers, and the PR/diff for independent Codex review. Do not enable live inventory writes, live shipment webhooks, or production deployment without their later gates.
