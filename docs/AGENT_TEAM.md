# ShipMode agent team

Every task in this repo runs through a small team of Claude Code subagents
(`.claude/agents/`). The main Claude session is the **coordinator**. It never
does the work alone: it hands each part to the specialist that owns it, then
has the other agents check that work against the goal.

The pattern works like multi-agent "heavy" modes (several agents work, then
compare answers), plus one shared goal ledger so nothing gets dropped.

## The team

| Agent | Owns | Never does |
|---|---|---|
| `goal-keeper` | Turns the request into a goal and numbered acceptance criteria; signs off last | Write code |
| `inventory-specialist` | Sheets Dashboard, ledger, counts, SKU/SSK/Shopify checks | Change another client's rules |
| `shipments-specialist` | Tracking, No Movement, ShipSidekick shipments, order checks | Touch inventory math |
| `reports-specialist` | EOD, daily update, Slack drafts, dashboard image, brand voice | Send anything |
| `client-rules-guardian` | Client isolation and approved vs. proposed business rules | Invent a rule value |
| `builder` | Edits code and tests for one approved plan | Review its own work |
| `cross-checker` | Compares every agent's output with the ledger and with each other | Edit code |
| `safety-reviewer` | The FACTORY_WORKFLOW s.6 review checklist on the real diff | Approve or merge |

## The run (coordinator follows this every time)

```
1. goal-keeper        → writes GOAL + acceptance criteria to the run ledger
2. specialists        → run in parallel, each on its part, each writes findings to the ledger
3. client-rules-guardian → checks every finding for isolation / unapproved rules
4. builder            → (only if code changes are needed and the plan is approved)
5. cross-checker      → reads everything; flags contradictions, gaps, unmet criteria
     └─ any FAIL → coordinator sends the item back to the owning agent, then repeat 5
6. safety-reviewer    → (only if code changed) checklist on `git diff`
7. goal-keeper        → final sign-off: each criterion MET with evidence, or NOT MET
```

Small tasks can skip steps 4 and 6. Steps 1, 5 and 7 are never skipped: these
are the steps that stop the goal from being missed.

## The run ledger (how agents cross-reference)

Subagents cannot talk to each other directly. They share one file:
`.agent-runs/<YYYY-MM-DD>-<short-name>/LEDGER.md` (git-ignored, never committed,
because it may hold real client numbers).

```markdown
# GOAL
<one sentence>

## Acceptance criteria
- [ ] AC1 <checkable statement>  — owner: <agent>
- [ ] AC2 ...

## Findings
### <agent> — <time>
- AC1: <what was found> — evidence: `file.py:42` / test name / command output
- Depends on: <another agent's finding, quoted>
- Open question: <anything only Gly or Carlos can answer>

## Cross-check
- AC1: PASS | FAIL — <reason>
- Conflict: <agent A says X, agent B says Y>

## Sign-off
- AC1: MET — <evidence>  |  NOT MET — <what is missing>
```

Rules for every agent:
- Read the whole ledger before starting; quote any other agent's finding you rely on.
- Append only. Never edit or delete another agent's section.
- Every claim cites evidence (file and line, test, or command output). No evidence → mark it UNVERIFIED.
- An unapproved business rule is an open question, never a guessed value.

## Using it

Ask for work normally ("check Muravai inventory against SSK", "add a warning for
blank as-of dates"). The coordinator runs the team. You can also call one agent
directly: "use the inventory-specialist to …".

This team helps Claude Code (the Builder in FACTORY_WORKFLOW). It does not
replace Codex review, Gly's approvals, or Carlos's decision sheet.
