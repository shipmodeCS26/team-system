# ShipMode project guidance

Required workflow: `FACTORY_WORKFLOW.md` (Issues, approved plans, checkpoints, independent review).
Read `docs/V1_PLAN.md`, `SHIPMODE_CLAUDE_BLUEPRINT.md`, `BRAND.md`, and the client rules in `docs/clients/` before editing.

Before any client inventory update or report, read the internal #inventory-updates channel and that client's own Slack channel (see `docs/clients/README.md` for each client's sheet and channel IDs). Keep clients separate: one client's sheet and channel only.

Read `SHIPMODE_CLAUDE_BLUEPRINT.md`, `BUILD_WORKFLOW.md`, and `README.md` before editing. Work on a separate branch or worktree, keep client Sheets read-only, and do not enable production writes. State acceptance criteria, run relevant tests, and report file-and-line findings or changes for Codex review. Never commit secrets or client exports.

Accuracy gate (Gly, Oct 21): before any client report is drafted, recount the day from the ShipSidekick API (`ssk_daily.py`, read-only) and check every product's remaining stock against ShipSidekick on hand (available + committed, adding back shipments created after the report day). Any product that does not match exactly is held, not reported, until the gap is explained. Never send a number that might need a correction later.
Allowed client-sheet writes: warehouse receipts and counts in each sheet's established receiving layout, Muravai EOD Sales/Receipts/Change Log rows, and loading a verified day of sales. Everything else stays read-only.
Times: convert every Slack timestamp to America/New_York yourself; tool labels such as "CST" are not reliable.
