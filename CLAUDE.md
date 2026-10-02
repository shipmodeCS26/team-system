# ShipMode project guidance

Required workflow: `FACTORY_WORKFLOW.md` (Issues, approved plans, checkpoints, independent review).
Read `docs/V1_PLAN.md`, `SHIPMODE_CLAUDE_BLUEPRINT.md`, `BRAND.md`, and the client rules in `docs/clients/` before editing.

Read `SHIPMODE_CLAUDE_BLUEPRINT.md`, `BUILD_WORKFLOW.md`, and `README.md` before editing. Work on a separate branch or worktree, keep client Sheets read-only, and do not enable production writes. State acceptance criteria, run relevant tests, and report file-and-line findings or changes for Codex review. Never commit secrets or client exports.

## Client-facing replies

Slack DMs, internal channels and shift notes contain internal-only details: warehouse or carrier mix-ups, staff names and contacts, guesses about cause, and internal discussion. When drafting a reply to a client:
- Share only the outcome the client needs: what arrived, what is still pending, what happens next. Mask internal details in general wording (e.g. "rerouted in transit, now sorted out") rather than explaining what went wrong internally.
- Reassure the client that the issue is handled or being handled.
- Never state anything false or deny something that happened; masking means leaving out internal details, not misinforming the client.
- Before drafting, check what the internal team (e.g. Carlos) said in earlier conversations so the reply matches the confirmed facts, and leave out anything still unconfirmed.
- Keep it short. Draft for the user to post; do not post to the client without being asked.
