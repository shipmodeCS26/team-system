# ShipMode Factory Workflow

How every change to the ShipMode Operations app is planned, built, reviewed, and accepted.
Claude Code and Codex must follow this file. The GitHub project board is the shared record;
no chat history is required to know where work stands.

## Roles

| Role | Who | Can do |
|---|---|---|
| Project lead | Gly | Approves plans, assigns work, accepts phases, merges to `main` |
| Business approver | Carlos | Approves business rules (decision sheet) |
| Builder | Claude Code | Plans, builds, tests, opens PRs, posts checkpoints |
| Reviewer | Codex | Independently reviews PRs against the blueprint |

The builder never reviews its own work and never merges.

## The shared record

- Every task is a **GitHub Issue** on the **ShipMode Build** project board.
- Board columns, in order:
  1. **Planned**: Issue exists, plan not yet approved
  2. **In progress**: plan approved and work actually started
  3. **In review**: tests pass, PR open, waiting for Codex review
  4. **Built**: Codex review passed, merged to `staging`
  5. **Checked**: Gly verified it on the staging site
  6. **Done**: Gly accepted and merged to `main` (live)
- Assigned is not started. A task moves to In progress only when work begins.
- Status comes from real events (plan approved, PR opened, review passed), never from being asked for a status update.

## 1. Shape the plan (one question at a time)

When starting a task:
1. Read `SHIPMODE_CLAUDE_BLUEPRINT.md`, `BRAND.md`, and this file.
2. Ask Gly **one** useful question at a time about anything the blueprint leaves open. Do not send a long form.
3. Recommend the smallest useful version and say what is **out of scope**.
4. Write the plan in the Issue:
   - Goal (one sentence)
   - What will be built
   - Out of scope
   - Acceptance criteria (checkable statements)
   - Tests to write
   - Open business rules (link the decision sheet item; never guess)
5. Stop. **A draft is not approval.** Wait for Gly to comment "Approved" on the Issue.

## 2. Build

- Work only on a branch named `issue-<number>-<short-name>`, in your own worktree.
- Commit and push to GitHub at least at every checkpoint, so the code on GitHub is always current.
- Never touch `main`. Never deploy. Never paste or commit secrets.
- Any business rule not approved in the decision sheet stays unresolved in code (a clear TODO plus an exception or "not set" state), never a guessed value.

## 3. Checkpoint (every session, before stopping)

Post a comment on the Issue:

```
CHECKPOINT <date>
Works: <what actually works now>
Checked: <tests run and results>
Not done: <what remains>
Blocked by: <nothing, or the exact blocker>
Next step: <the first thing the next session should do>
```

A checkpoint saves progress. It does not move the card.

## 4. Resume

A new session starts with "Continue issue #<number>". Read the Issue, the plan, and all checkpoints, then
summarize where things stand before doing anything. Do not ask Gly to paste old chats.

## 5. Hand off for review

Only when all tests pass and the acceptance criteria are met:
1. Open a PR into `staging` linked to the Issue.
2. PR description: what changed, how it was tested, and anything uncertain.
3. Move the Issue to **In review**.

## 6. Independent review (Codex)

Codex inspects the actual diff and test results, not the summary. For inventory work, it checks:

- client isolation (no client's data or rules can touch another's)
- inventory math and double counting
- incoming stock never counting as On Hand
- physical count resets and count timing
- reships, reversals, and duplicate event handling
- login and access on every page and data route
- secrets not in code
- missing tests

Findings go on the PR. Blocking findings send the Issue back to In progress.

## 7. Accept

1. Review passed → merge to `staging` → **Built**.
2. Gly tests on the staging site → **Checked**.
3. Gly merges to `main` → live → **Done**.

A phase is complete only when Gly accepts its last Issue.

## 8. App safety model (from Tomato Factory)

The app itself follows the Tomato Factory pattern: data is produced upstream, and ShipMode is the controlled
workspace where it is imported, reviewed, approved, posted, and verified.

```
Client Sheets / warehouse / ShipSidekick  (upstream, read-only)
  → Import batch (snapshot + checksum per item)
  → Review (exceptions, mismatches, unknown SKUs)
  → Approve (freezes the package)
  → Post to inventory ledger
  → Verify (reconciliation + audit)
  → Client report (simulated until cutover)
```

### Two environments, fully separate

| | Staging (sandbox) | Production (live) |
|---|---|---|
| Purpose | Test and demo lane, kept after launch | Real ShipMode operations |
| Database | Its own; seeded or copied data | Its own |
| Branch | `staging` | `main` |
| Resets | Allowed, scoped to a batch, logged | Never |

Changes move to production only by merging an explicit commit. Render keeps prior deploys for rollback.

### Safety flags (environment variables)

| Flag | Staging | Production until cutover |
|---|---|---|
| `APP_INTERNAL_WRITES_ENABLED` | `true` | `true` |
| `APP_EXTERNAL_WRITES_ENABLED` | `false` | `false` |
| `REPORT_SEND_SIMULATION_ONLY` | `true` | `true` |

- Internal writes: the app's own database (events, approvals, exceptions).
- External writes: anything outside the app (client Sheets, Slack, client emails, ShipSidekick). Blocked until a separate go/no-go decision, then enabled one integration at a time.
- Report simulation: client reports are generated and previewed with a receipt, never sent, until cutover.

### Rules the code must follow

- Imports are idempotent: the same Sheet snapshot imported twice changes nothing.
- Imports never overwrite ledger history; they create items for review.
- Approval freezes exactly what was approved (numbers, rule versions, source snapshot).
- Every mutation requires sign-in and CSRF protection.
- Every admin action (import, approve, reset, correction) writes an audit record.
- Concurrent edits use version checks so nothing is silently overwritten.
