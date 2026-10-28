# Daily operations accuracy plan

Status: DRAFT, waiting for Gly's approval and Codex review. Nothing here is built yet.

Goal: every number we send to a client or the team is checked against its source before it goes out,
every instruction from Carlos, Orlando or a client is followed until it is confirmed done, and every
mistake becomes a rule (and a test where possible) so it does not happen twice.

## 1. What went wrong (Oct 19–28)

| # | Root cause | Examples |
|---|---|---|
| 1 | Numbers typed or calculated by hand | Onset run-out threshold sent as ~430/day (correct ~335); Muravai kit adjustment given as −1,960 with the wrong sign; status note written on the wrong sheet row |
| 2 | Late or unclear data | Fascial and Neurosmile sheets 9–10 days behind; Onset export dated in Philippine time, not ET; no data source for Vitalia |
| 3 | Incomplete product mappings | Muravai kit is a BOM of 3 parts; TrueSleep 3-pack and Vitamin D3+K2 dropped by the nightly script; Onset Dashboard counted whole orders as one product |
| 4 | Instructions not tracked to completion | Max Gummies gift (OS-MAX-7D) back on orders: 386 orders went out without it; D3+K2 "ship without"; Muravai "1 filter" SKU question unanswered for 2 days |
| 5 | Draft handling | Two drafts in one Slack channel, so an older wrong version was sent; drafts lost and redone |
| 6 | Tested checks not used | The repo already has `eod_check.py`, `ssk_check.py`, `client_rules.py`, `eod_report.py`; the nightly run used an ad-hoc script instead |

## 2. Standing instructions from Carlos and Orlando

These are rules, not one-off requests. Each one gets an owner, a check, and a place in the instruction log (section 4, Phase 0).

| ID | From | Date | Instruction | How we check it |
|---|---|---|---|---|
| CR1 | Carlos | Oct 18 | White label with Revly: never mention ShipMode in the Revly workspace or to their clients (Vitalia, Cedar Labs via Revly) | Draft check: block any Vitalia/Revly draft that contains "ShipMode" |
| CR2 | Carlos | Oct 16 | Revly Google workspace: read only, don't touch anything | No writes to Revly files |
| CR3 | Carlos | Oct 19 | Make clients order enough that we never get close to running out | Forecast always states order-by date and quantity when cover < 30 days |
| CR4 | Carlos | Oct 19 | Client-facing sheets and announcements are approved before sharing | Drafts only; Gly (or Carlos when asked) sends |
| CR5 | Carlos | Oct 21 | Onset Amazon-version gummies are the same product and SKU as Urolithin A | Mapping rule in the Onset package |
| CR6 | Carlos | Oct 22 | Last fulfilled order numbers (e.g. Vitalia) are confirmed with Sawyer, not inferred from dates | Never state a "last order" to a client without Sawyer/Orlando confirmation |
| CR7 | Carlos | Oct 26 | Muravai kits that arrive are received as components (hose, bracket, teflon tape), never as the kit | Rule in the Muravai package + SSK check: kit own stock must be 0 |
| OR1 | Orlando | Oct 19, 26 | Address changes only sync to SSK before an order is printed or committed: tell the warehouse or check SSK | Support checklist item for every address change |
| OR2 | Orlando | Oct 19 | Cancellations must be done in Shopify before the label is bought | Support checklist item |
| OR3 | Orlando | Oct 19 | For inbound stock, send SKU, boxes × units, carrier, tracking and ETA so POs can be created in advance | Incoming Stocks row is complete before the shipment is due |
| OR4 | Orlando | Oct 20 | Vitalia: hold units until more inventory; Orlando sends the last order number at end of day | Vitalia report waits for his number |
| OR5 | Orlando | Oct 26 | No-movement requests always include the date of each order (label date and days since) | Template for #no-movement posts |
| OR6 | Orlando | Oct 23 | Fascial pouch sealing: keep Ben posted on how serious it is | Open item until closed by Orlando |

## 3. Ground rules

1. No number goes out unless it passed a check. Anything not checked is labelled "not updated" or "to confirm".
2. No hand math. Totals, days of cover and forecasts come from code.
3. Every instruction (section 2 and new ones) is logged and checked until confirmed done.
4. Every mistake gets a rule in the mistake log and, where possible, a test.

## 4. Phases

### Phase 0: process fixes this week (no code)
- Ask Carlos for read-only SSK API keys for Onset and Vitalia.
- Export rules until then: Eastern Time, full day 12:00 AM–11:59 PM, loaded by 9 AM ET; written into `docs/clients/`.
- Instruction log (sheet tab): instruction, from, date, client, owner, warehouse confirmed, checked against orders. Seeded with section 2 plus Max Gummies, D3+K2 and the Muravai "1 filter" SKU.
- One Slack draft per channel, date in the title; every number re-read before saving.
- Weekly shelf count of the riskiest items, starting with Muravai kit parts (open gap: SSK +20 kits, +25 hoses, +27 brackets).

### Phase 1: nightly check gate (Issue A)
- Goal: the nightly run uses the repo's tested checks instead of a scratch script.
- Build: run `eod_check` and `ssk_check` for every client with SSK access; one table per client and product
  (shipped, remaining, Sheet vs SSK, Sheet date = yesterday, unknown SKUs); result VERIFIED / REVIEW / INCOMPLETE.
- Acceptance: replaying Oct 26–27 reproduces every mistake in section 1 rows 1–3; any unknown SKU makes the client REVIEW; tests pass.
- Out of scope: any write to Sheets or SSK.

### Phase 2: complete rule packages (Issue B)
- Goal: every SKU and bundle for all six clients in `client_rules.py`.
- Includes: Fascial packs, Bundle-3, TrueSleep 3-pack, D3+K2; Neurosmile, PuraVita and ClarityMD bundles; Onset codes; Muravai kit as parts.
- Approval: Carlos approves each package (business rule). Until then it stays PROPOSED.
- Acceptance: every SKU shipped in the last 30 days is mapped, and mapped units equal the SSK on-hand drop per day.

### Phase 3: reports from checked numbers only (Issue C)
- Goal: client posts and #inventory-updates produced by `eod_report.py` in Gly's approved format.
- A client that isn't VERIFIED gets a held draft with reasons, not numbers.
- The forecast reads the last recommendation posted to that client, so it cannot contradict it.
- Acceptance: identical to hand-checked drafts on 3 sample days; a mismatch blocks the draft.

### Phase 4: instruction compliance check (Issue D)
- Goal: open instructions are checked against real orders every night.
- Example: "386 Onset orders, 0 contain OS-MAX-7D" becomes an alert in Gly's DM.
- Acceptance: the Max Gummies case is caught on the first export after the instruction.

### Phase 5: weekly accuracy review (every Monday)
Scorecard: reports sent / VERIFIED / held; mistakes found by us vs by others; rules added; open instructions; Sheet-vs-SSK gaps.

## 5. How we know it's working

| Measure | Oct 28 | Target, end of week 2 |
|---|---|---|
| Client posts needing a correction | 2 | 0 |
| Sheets current to yesterday | 2 of 6 (Oct 27) | 6 of 6, or labelled with the reason |
| Unknown SKUs reaching a report | 2 | 0 |
| Instructions confirmed within 24 h | Max Gummies still open | 100% |
| Clients with automatic SSK checks | 4 of 6 | 6 of 6 |

## 6. Roles (from FACTORY_WORKFLOW.md)

- Gly: approves this plan and each Issue, accepts each phase.
- Carlos: approves business rules (rule packages, CR rules).
- Orlando: confirms warehouse instructions and shelf counts.
- Claude Code: builds, tests, posts checkpoints.
- Codex: independent review of this plan and every PR.

## 7. Review request for Codex

Please review this plan before any Issue is opened:
1. Is any root cause in section 1 missing a fix in section 4?
2. Are the acceptance criteria in Phases 1–4 checkable and strict enough?
3. Does anything here conflict with `SHIPMODE_CLAUDE_BLUEPRINT.md`, `BUILD_WORKFLOW.md` or the client rules in `docs/clients/`?
4. Which existing modules or tests should Phase 1 reuse, and what is risky about wiring them into the nightly run?
Reply as a PR review comment or a commit on this branch.
