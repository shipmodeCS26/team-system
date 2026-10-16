# ShipMode rules for AI assistants (ChatGPT / Codex / Claude)

This file is the single rulebook for any AI working on this repo. Claude Code and ChatGPT/Codex
follow the same rules so work can move between them at any time. If anything here conflicts with
`FACTORY_WORKFLOW.md`, `SHIPMODE_CLAUDE_BLUEPRINT.md`, `BRAND.md`, or `docs/clients/`, those files win.

## 0. Read before doing anything

1. `FACTORY_WORKFLOW.md` (how work is planned, built, reviewed, accepted)
2. `SHIPMODE_CLAUDE_BLUEPRINT.md` (what we are building and why)
3. `docs/V1_PLAN.md` (what is built now and what is next)
4. `BRAND.md` (logo, colors, fonts)
5. `docs/clients/<client>/RULES.md` for any client you touch (today: Muravai only)
6. `README.md` and `BUILD_WORKFLOW.md` (setup, flags, release gates)

If you cannot read a file, say so and ask for it. Never guess what it says.

## 1. Who does what

| Role | Who | Can do |
|---|---|---|
| Project lead | Gly | Approves plans, assigns work, accepts, merges to `main` |
| Business approver | Carlos | Approves business rules (decision sheet) |
| Builder | Whichever AI is building (Claude Code or ChatGPT/Codex) | Plans, builds, tests, opens PRs, posts checkpoints |
| Reviewer | The other AI | Independently reviews the PR |

- The builder never reviews its own work and never merges.
- If ChatGPT built it, Claude reviews it, and the reverse. If only one AI is available, the review
  waits or Gly reviews. Say this out loud; do not self-approve.

## 2. Handoff between Claude and ChatGPT

All state lives on GitHub, never in chat history. That is what makes switching possible.

- Every task is a GitHub Issue on the **ShipMode Build** board.
- Before stopping (limit reached, end of day, switching AI), post a checkpoint on the Issue and push the branch:

```
CHECKPOINT <date> (<Claude|ChatGPT>)
Works: <what actually works now>
Checked: <tests run and results>
Not done: <what remains>
Blocked by: <nothing, or the exact blocker>
Next step: <the first thing the next session should do>
```

- To resume: Gly says "Continue issue #<number>". Read the Issue, the approved plan, every checkpoint,
  and the branch's latest commits. Summarize where things stand **before** changing anything.
  Never ask Gly to paste old chats.
- A checkpoint saves progress. It does not move the board card.

## 3. Workflow for every task

**Plan (one question at a time)**
1. Ask Gly one useful question at a time about anything left open. No long forms.
2. Recommend the smallest useful version. Say what is out of scope.
3. Write the plan in the Issue: Goal (one sentence), What will be built, Out of scope,
   Acceptance criteria (checkable), Tests to write, Open business rules (link the decision sheet item).
4. Stop. A draft is not approval. Wait for Gly to comment **"Approved"**.

**Build**
- Branch name: `issue-<number>-<short-name>`. Never work on `main`. Never deploy.
- Commit and push at least at every checkpoint.
- A business rule that is not approved stays unresolved in code: a clear `TODO` plus an exception or
  "not set" state. Never a guessed value.
- Match the existing code style: Python/Flask, plain HTML/CSS/JS, no new frameworks or stack changes.

**Hand off for review** (only when all tests pass and acceptance criteria are met)
1. Open a PR into `staging`, linked to the Issue.
2. PR description: what changed, how it was tested (paste the test output summary), anything uncertain.
3. Move the Issue to **In review**.

**Board columns:** Planned → In progress → In review → Built → Checked → Done.
Assigned is not started. Status moves only on real events (plan approved, PR opened, review passed).

## 4. Hard rules (never break these)

- **Never** commit or paste secrets: spreadsheet IDs, service-account JSON, passwords, hashes, webhook secrets, API keys.
- **Never** commit real client exports or customer names/addresses.
- Client Google Sheets are **read-only**. Never write to them.
- **Never** enable production writes. Flags stay:
  `APP_EXTERNAL_WRITES_ENABLED=false`, `REPORT_SEND_SIMULATION_ONLY=true`.
  No live webhooks, no inventory mutations, no Slack/email to clients without a separate approved gate.
- **Never** touch `main`, deploy, or merge. Gly merges.
- **Never** invent numbers. A failed source shows an explicit error, not made-up or stale values.
- **Never** guess a business rule, SKU mapping, or client name. Ask, or leave a TODO.
- Every record, rule, query, and report is scoped to one client. No client's data or rules may touch
  another's. Muravai bundle rules must never leak into generic code.
- AI may propose; deterministic code plus human approval changes balances. AI never alters
  official inventory or sends client communications on its own.

## 5. Domain rules to remember

**Clients (6):** ClarityMD, Fascial. Labs, Muravai, Neurosmile, PuraVita, Onset.
Internal key for Neurosmile is the misspelled `nuerosmile`; keep it. "Fascial" vs "Facial" Labs is unconfirmed.

**Inventory**
- On Hand = physical sellable units. Incoming never counts as On Hand before verified receipt.
- A physical count **sets** the balance (record before/after/delta); it is not added.
- Count timing matters: don't subtract orders processed before the count.
- Possible reships: no effect. Confirmed reships deduct exactly once.
- Negative expected balances stay negative and are flagged; never silently set to 0.
- Report states: `VERIFIED`, `REVIEW`, `INCOMPLETE`. Keep `PENDING`, formula errors, and `REVIEW` visible.
- Rules in the blueprint section 6 are **proposed** until Carlos approves them.

**No Movement (shipment aging)**
- Age is from the latest **physical carrier scan**, not fulfillment or webhook time.
- Tiers (complete 24h days): 5–6 Watch, 7–9 Urgent, 10+ Critical. Never drop a shipment after day 10.
- No physical scan + pre-transit → shipped date, then label date (UI names the fallback).
  In-transit/unknown with no scan → Missing Data.
- Delivered and cancelled labels leave the queue. A human follow-up does not clear a carrier exception.
- Demo shipments are labeled `DEMO-*`.

**Muravai** — follow `docs/clients/muravai/RULES.md` exactly. Key points: a 3-pack of filters = 1 MUR001;
MUR003 kits = Teflon quantity per order; MUR004/MUR005 = excess over Teflon per order; never report MUR006;
Sep 1 2026 golden test = 681 units.

## 6. Brand (UI work)

Use `BRAND.md` tokens only. Logo `static/shipmode-logo.png` on the black sidebar. Blue `#2563D6` means
"act here", never a status. Status colors: ok `#1E8E4E`, warn `#C27A00`, critical `#C62828`, muted `#6B7280`,
always paired with a word. Fonts: Barlow Condensed 800 italic for titles/headline numbers only; Barlow for
everything else; self-hosted in `static/fonts/`. Tables use tabular numbers.

## 7. Tests

```
pip install -r requirements.txt
python -B -m unittest -v test_tracking test_inventory test_ledger test_muravai_rules
```

All tests must pass before a PR. Add tests for every new behavior. If you cannot run tests
(for example plain ChatGPT with no code runner), say so clearly and do not claim they pass.

## 8. How to review (when you are the reviewer)

Inspect the actual diff and test results, not the builder's summary. Check:
- client isolation (no cross-client data or rules)
- inventory math and double counting
- incoming stock never counted as On Hand
- physical count resets and count timing
- reships, reversals, duplicate events
- sign-in and access on every page and data route; CSRF on writes
- secrets not in code
- missing tests

Report each finding as: **severity** (blocking / non-blocking), **file:line**, which rule it breaks, proof.
Post findings on the PR. Blocking findings send the Issue back to In progress.
A review with no blockers is not production approval.

## 9. How to report back to Gly

End each work session with: files changed, test results (exact counts), what is blocked, and the next step.
Short, plain language. Be honest: if something failed or was skipped, say so.
