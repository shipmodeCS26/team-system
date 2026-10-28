# Daily operations accuracy plan

Status: DRAFT, revised after Codex review (PR #32). Waiting for Gly's approval. Nothing here is built yet.

Goal: every number we send to a client or the team is checked against its source before it goes out,
every instruction from Carlos, Orlando or a client is followed until evidence confirms it is done, and
every mistake becomes a rule (and a test where possible) so it does not happen twice.

## 1. What went wrong (Oct 19–28)

| # | Root cause | Examples |
|---|---|---|
| 1 | Numbers typed or calculated by hand, and writes not checked | Onset run-out threshold sent as ~430/day (correct ~335); Muravai kit adjustment given as −1,960 with the wrong sign; status note written on the wrong sheet row |
| 2 | Late or unclear data | Fascial and Neurosmile sheets 9–10 days behind; Onset export dated in Philippine time, not ET; no data source for Vitalia |
| 3 | Incomplete product mappings | Muravai kit is a BOM of 3 parts in SSK; TrueSleep 3-pack and Vitamin D3+K2 dropped by the nightly script; Onset Dashboard counted whole orders as one product |
| 4 | Instructions not tracked to completion | Max Gummies gift (OS-MAX-7D) back on orders: 386 orders went out without it; D3+K2 "ship without"; Muravai "1 filter" SKU question unanswered for 2 days |
| 5 | Draft handling | Two drafts in one Slack channel, so an older wrong version was sent; drafts lost and redone |
| 6 | Tested checks not used | The repo already has `eod_check.py`, `ssk_check.py`, `sku_check.py`, `client_rules.py`, `eod_report.py`; the nightly run used an ad-hoc script instead |

## 2. Standing instructions from Carlos and Orlando

These are rules, not one-off requests. Each one gets an owner, an evidence source, and an entry in the instruction log (Phase 0).

| ID | From | Date | Instruction | Evidence that it is followed |
|---|---|---|---|---|
| CR1 | Carlos | Oct 18 | White label with Revly: never mention ShipMode in the Revly workspace or to their clients (Vitalia, Cedar Labs via Revly) | Revly-facing drafts use an approved white-label template; the gate inspects every artifact (text, images such as the logo `dashboard_image.py` embeds, attachment names and metadata, links) and blocks any ShipMode reference |
| CR2 | Carlos | Oct 16 | Revly Google workspace: read only, don't touch anything | No writes to Revly files (no log or tab created there either) |
| CR3 | Carlos | Oct 19 | Make clients order enough that we never get close to running out | Forecast states order-by date and quantity whenever cover is below the client's approved reorder threshold. The threshold, cover target and lead time per client/SKU are an open business rule for Carlos (UNKNOWN until approved); the 30 days used in today's manual reports is not an approved rule |
| CR4 | Carlos | Oct 19 | Client-facing sheets and announcements are approved before sharing | Drafts only; Gly (or Carlos when asked) sends |
| CR5 | Carlos | Oct 21 | Onset Amazon-version gummies are the same product and SKU as Urolithin A | Mapping rule in the Onset package |
| CR6 | Carlos | Oct 22 | Last fulfilled order numbers (e.g. Vitalia) are confirmed with Sawyer, not inferred from dates | No "last order" stated to a client without Sawyer/Orlando confirmation |
| CR7 | Carlos | Oct 26 | In ShipSidekick, Muravai kits that arrive are received as components (hose, bracket, teflon tape), not as the kit SKU | PENDING APPROVAL as a rule revision (see Phase 2): it may conflict with `docs/clients/muravai/RULES.md`, which counts an explicit Connector Kit SKU directly and defines MUR003 case packs. The Sheet keeps counting physical kits as MUR003. No automated "kit own stock = 0" check until the revision is approved. |
| OR1 | Orlando | Oct 19, 26 | Address changes only sync to SSK before an order is printed or committed: tell the warehouse or check SSK | Support checklist; evidence = SSK order address matches Shopify, or Orlando's confirmation |
| OR2 | Orlando | Oct 19 | Cancellations must be done in Shopify before the label is bought | Support checklist; evidence = no label on a cancelled order |
| OR3 | Orlando | Oct 19 | For inbound stock, send SKU, boxes × units, carrier, tracking and ETA so POs can be created in advance | Incoming Stocks row has all five fields before the shipment is due |
| OR4 | Orlando | Oct 20 | Vitalia: hold units until more inventory; Orlando sends the last order number at end of day | Vitalia report waits for his number |
| OR5 | Orlando | Oct 26 | No-movement requests always include each order's label date and days since | Template for #no-movement posts |
| OR6 | Orlando | Oct 23 | Fascial pouch sealing: keep Ben posted on how serious it is | Open until Orlando closes it |

## 3. Ground rules

1. No number goes out unless it passed a check. Anything not checked is labelled "not updated" or "to confirm", and held drafts contain no unchecked numbers at all.
2. No hand math. Totals, days of cover and forecasts come from code.
3. Every instruction is logged and stays open until its evidence source confirms completion.
4. Every mistake gets a rule in the mistake log and, where possible, a test.
5. External writes stay disabled (`FACTORY_WORKFLOW.md` safety flags): no automated Slack sends, Sheet writes or SSK writes come out of this plan. Anything that would write outside the app is a separate go/no-go decision.

## 4. Phases

### Phase 0: process fixes this week (no code)
- Ask Carlos for read-only SSK API keys for Onset and Vitalia, and to confirm whether Vitalia (Revly white label) is in scope as a ShipMode-tracked client.
- Export rules until then: Eastern Time, full day 12:00 AM–11:59 PM, loaded by 9 AM ET; written into `docs/clients/`.
- Instruction log kept in a ShipMode-owned, access-controlled store (an internal ShipMode workbook now, the app's internal store later). Never created or updated inside any client or Revly workbook. Columns: instruction, from, date, client, type (section Phase 4), owner, evidence source, status, confirmed by, confirmed on. Seeded with section 2 plus Max Gummies, D3+K2 and the Muravai "1 filter" SKU.
- One Slack draft per channel, date in the title; every number re-read before saving.
- Weekly shelf count of the riskiest items, starting with Muravai kit parts (open gap: SSK +20 kits, +25 hoses, +27 brackets).

### Phase 1: nightly check gate (Issue A)
- Goal: the nightly run uses the repo's tested checks instead of a scratch script.
- Build: run the gate for every in-scope client, every night. A client without SSK credentials or data still gets a result: INCOMPLETE with a source-specific reason (e.g. "no SSK key", "SSK request failed", "truncated"). Run `eod_check` and `ssk_check`; one table per client and product
  (shipped, remaining, Sheet vs SSK, Sheet date, unknown SKUs, rule-package status); one overall result VERIFIED / REVIEW / INCOMPLETE.
- Date rule: the expected Sheet date is yesterday in America/New_York. An older or future Dashboard date gives INCOMPLETE (stale) or REVIEW (future). Tests cover no-shipment days and runs just before and after midnight ET.
- Gate rule: a client can only be VERIFIED when its rule package is APPROVED. A PROPOSED or missing package gives INCOMPLETE (missing) or REVIEW (proposed), never VERIFIED.
- Aggregate truth table (each row tested):

| Condition | Overall result |
|---|---|
| Missing credentials, SSK source error, truncated SSK data, missing rule package, stale Sheet date, no data for the date | INCOMPLETE |
| `eod_check` REVIEW, any SSK row DIFFERENT or REVIEW, unmatched / duplicate / blank SKU, PROPOSED package, future date, wrong-row write | REVIEW |
| `eod_check` VERIFIED, every SSK row MATCH, none of the above, package APPROVED, date = yesterday ET | VERIFIED |

  When several apply, INCOMPLETE wins over REVIEW, and REVIEW wins over VERIFIED.
- Sheet-write check: every write the nightly run or a person makes for a report (EOD rows, status notes, Incoming Stocks rows) is checked against an approved expected-cell map (product row → cell) by re-reading after the write; a value in the wrong product row makes the client REVIEW.
- Fixtures: sanitized, immutable test fixtures (synthetic values shaped like the real Sheet, shipment and SSK data, no real client exports or customer data) for Oct 26–27, with golden expected results.
- Acceptance:
  - Replaying the fixtures reproduces every mistake in section 1 rows 1–3: wrong threshold, wrong-sign adjustment, wrong-row note, stale Sheet, unknown SKU, whole-order count.
  - Every PROPOSED or missing package yields INCOMPLETE or REVIEW (tested for Fascial, PuraVita, NeuroSmile as they stand today).
  - Any unknown SKU makes the client REVIEW.
  - Tests pass in CI without network access.
- Out of scope: any write to Sheets, SSK or Slack.

### Phase 2: complete rule packages (Issue B)
- Goal: every SKU and bundle in `client_rules.py`, for the six clients in `SHIPMODE_CLAUDE_BLUEPRINT.md`, plus Vitalia as an explicit, isolated seventh package if Carlos confirms it is in scope (otherwise Vitalia gets its own separately scoped workflow and is reported as INCOMPLETE).
- Includes: Fascial packs, Bundle-3, TrueSleep 3-pack, D3+K2; Neurosmile, PuraVita and ClarityMD bundles; Onset codes; Muravai CR7 as a proposed revision to `docs/clients/muravai/RULES.md`.
- Coverage source: the complete active Shopify catalog (`sku_check.py`), the SSK product and inventory catalog, every Dashboard product row, and historical shipments, not just recent sales. Every discrepancy is resolved or explicitly held.
- Mapping validation: against shipment rows with approved expected per-SKU quantities (e.g. FAC3XBDL = 3 TrueForm), not against daily SSK stock changes. Stock balances are reconciled separately using all inventory events (shipments, receipts, adjustments, damage, counts); which SSK number equals the Sheet's "Remaining" (available vs available + committed) is an open business rule for Carlos.
- Approval: Carlos approves each package and the CR7 revision. Until then they stay PROPOSED, and Phase 1 keeps those clients out of VERIFIED.
- Acceptance: every SKU in all four coverage sources is mapped or held; mapping tests pass on the approved expected quantities.

### Phase 3: reports from checked numbers only (Issue C)
- Goal: client posts and #inventory-updates produced by `eod_report.py` in Gly's approved format.
- Snapshot binding: each report records the source snapshot checksum and rule-package version that passed the gate; drafting or approval is rejected if any checked input changed after the check.
- One as-of time: the nightly run captures a snapshot of Incoming Stocks together with the Dashboard and SSK data, so every section of the report shares the same as-of time. Today `app.py` reads Incoming Stocks live and `eod_report.py` holds any back-dated report; this phase replaces that with the captured snapshot and tests that a correctly dated nightly report is not held for that reason.
- Forecasts: demand, days of cover, run-out date, order-by date and suggested quantity come from one deterministic, tested function using approved inputs (demand window, lead time, cover target). Until Carlos approves the formula and inputs, forecast fields are kept out of VERIFIED report text and drafted separately, labelled "estimate, not verified", for Gly's manual review.
- Last recommendation: when Gly approves a client post, its recommendation (quantities, dates, assumptions, formula version) is stored as structured, versioned data. The next forecast is recomputed from approved inputs and compared with that record to show an explicit "changed from <date>" warning; a posted Slack message is never parsed to set a quantity.
- Held drafts: a client that isn't VERIFIED gets a held draft with reasons only. Held text, images and Slack draft payloads contain no unchecked client numbers (today `eod_report.build_report` still renders them; this phase fixes that).
- Acceptance: identical to hand-checked drafts on 3 fixture days; a changed input after the check is rejected; tests assert that held outputs contain no inventory or forecast numbers; forecast tests reproduce the Onset ~430 vs ~335/day error as a failure; Revly-facing fixtures contain no ShipMode text, logo or link.
- Delivery stays preview-only (`REPORT_SEND_SIMULATION_ONLY`).

### Phase 4: instruction compliance check (Issue D)
- Goal: every open instruction is checked every night against its evidence source and stays open until that evidence confirms completion.

| Instruction type | Example | Evidence source | Pass | Fail |
|---|---|---|---|---|
| Order content (add/remove an item) | OS-MAX-7D back as gift; ship Fascial without D3+K2 | Order export after the instruction date | Every unshipped/new order has (or lacks) the item | Any order after the cut-off breaks it |
| SKU setup | Muravai "1 filter" | SSK product catalog + reply to the client | SKU exists with barcode/dims and the client was answered | Missing field or no reply |
| Receiving rule | CR7 kits as parts | SSK PO lines and levels | Lines match the approved rule | Kit line on a PO, or own stock above the approved rule |
| Address change / cancellation | OR1, OR2 | SSK order vs Shopify order | Address/cancel matches before label date | Label printed with the old state |
| Inbound completeness | OR3 | Incoming Stocks row | All five fields present before ETA | Any field blank at ETA |
| People follow-up / approval | Vitalia shelf count, pouch sealing, client approvals | Named person's confirmation in Slack | Confirmation recorded in the log | No confirmation by the due date |

- Alerts are internal only: shown in the app and written as an unsent DM preview for Gly. Automated Slack delivery needs its own approval and cutover criteria.
- Acceptance: one fixture scenario per type above (pass and fail), including the Max Gummies case caught on the first export after the instruction; an instruction never closes without its evidence.

### Phase 5: weekly accuracy review (every Monday)
Scorecard: reports drafted / VERIFIED / held; mistakes found by us vs by others; rules added; open instructions by age; Sheet-vs-SSK gaps.

## 5. How we know it's working

| Measure | Oct 28 | Target, end of week 2 |
|---|---|---|
| Client posts needing a correction | 2 | 0 |
| Sheets current to yesterday | 2 of 6 (Oct 27) | 6 of 6, or labelled with the reason |
| Unknown SKUs reaching a report | 2 | 0 |
| Instructions confirmed within 24 h | Max Gummies still open | 100% |
| Clients with automatic SSK checks | 4 of 6 | 6 of 6 (plus Vitalia if in scope) |

## 6. Open business rules (Carlos)

1. Is Vitalia (Revly white label) in scope as a ShipMode-tracked client?
2. CR7: does the Oct 26 kit-receiving instruction revise `docs/clients/muravai/RULES.md`?
3. Which SSK number equals the Sheet's "Remaining": available, or available + committed?
4. Reorder threshold, cover target, lead time and forecast formula per client/SKU.
5. Approval of each client rule package (Fascial, PuraVita, NeuroSmile, ClarityMD, Onset, and Vitalia if in scope).
6. The approved white-label template for Revly-facing reports.

## 7. Workflow

Per `FACTORY_WORKFLOW.md`, planning Issue #33 on the ShipMode Build board is the record for this plan: it links this document and PR #32, lists the open business rules above, and is where Gly comments "Approved". Implementation Issues A–D are opened only after that approval.

## 8. Roles (from FACTORY_WORKFLOW.md)

- Gly: approves this plan and each Issue, accepts each phase.
- Carlos: approves business rules (rule packages, CR7 revision, Vitalia scope, available vs available + committed).
- Orlando: confirms warehouse instructions and shelf counts.
- Claude Code: builds, tests, posts checkpoints.
- Codex: independent review of this plan and every PR.

## 9. Review request for Codex

All 21 round-1 findings are addressed in this revision. Please check:
1. Is any root cause in section 1 still missing a fix in section 4?
2. Are the Phase 1–4 acceptance criteria checkable and strict enough?
3. Any remaining conflict with `SHIPMODE_CLAUDE_BLUEPRINT.md`, `BUILD_WORKFLOW.md`, `FACTORY_WORKFLOW.md` or `docs/clients/`?
