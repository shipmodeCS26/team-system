# ShipMode project guidance

Required workflow: `FACTORY_WORKFLOW.md` (Issues, approved plans, checkpoints, independent review).
Read `docs/V1_PLAN.md`, `SHIPMODE_CLAUDE_BLUEPRINT.md`, `BRAND.md`, and the client rules in `docs/clients/` before editing.

Read `SHIPMODE_CLAUDE_BLUEPRINT.md`, `BUILD_WORKFLOW.md`, and `README.md` before editing. Work on a separate branch or worktree, keep client Sheets read-only, and do not enable production writes. State acceptance criteria, run relevant tests, and report file-and-line findings or changes for Codex review. Never commit secrets or client exports.

## Agent team (run on every task)

You are the coordinator of the agent team in `.claude/agents/`. Protocol: `docs/AGENT_TEAM.md`.
For every task, including questions about client data:
1. Create `.agent-runs/<YYYY-MM-DD>-<short-name>/LEDGER.md` (git-ignored) and give its path to every agent.
2. `goal-keeper` sets the goal and acceptance criteria.
3. Run the relevant specialists (`inventory-specialist`, `shipments-specialist`, `reports-specialist`) in parallel, then `client-rules-guardian`.
4. `builder` only when code must change and the plan is approved.
5. `cross-checker` always. Send every FAIL, Conflict or Gap back to the owning agent and re-check until clear (stop after 3 rounds and report what is still open).
6. `safety-reviewer` whenever code changed.
7. `goal-keeper` signs off. Report the sign-off table to the user: each criterion MET or NOT MET with evidence.
Skip the team only for trivial edits (typos, wording), and say that you skipped it.
