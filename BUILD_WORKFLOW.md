# ShipMode build workflow

This project uses the [Tomato Factory workflow](https://kevin.readtomato.com/hte-content-workflow-tomato-factory/) as a process reference. ShipMode's domain rules and data boundaries remain specific to inventory and shipment tracking.

## Data path

1. Keep the six client Google Sheets as read-only inventory sources. Fetch displayed values server-side with a service account limited to those workbooks. Show each source's as-of date, status, and errors.
2. Keep No Movement and Inventory as separate views in one authenticated workspace. A failed source shows an explicit error; it never falls back to invented or stale numbers.
3. Record any future edits, approvals, or inventory events in a separate application store with actor, source, time, and an audit trail. No Movement demo data and read-only Inventory must not imply that production write workflows exist.
4. Require a deliberate, separately reviewed release before enabling shipment webhooks, inventory mutations, or other external writes.

## Build and release gates

For each scoped change, write the expected behavior and source of truth, implement it on a branch, run relevant tests, and have a reviewer inspect the diff against those expectations. A passing code review is not deployment approval.

Use a private test deployment with synthetic shipments and read-only Sheet access. Verify each of the six client mappings, per-client failure behavior, authentication, and source timestamps there. Promote an identified commit to production only after the test deployment passes. Keep a known working commit available for rollback. Never commit spreadsheet IDs, service-account keys, passwords, or real client exports.

## Current stage

- The No Movement and Inventory views and read-only Sheets adapter are in a draft pull request.
- The public Render app has not been connected to the six Sheets or verified with live data.
- Google Cloud service-account access, private Render settings, a private test deployment, and deployment verification remain open.
- Shipment tracking remains in demo mode; live webhooks and inventory writes remain disabled.

Claude Code may implement or review a scoped branch. Codex independently reviews the diff and tests. Use separate worktrees when both are editing, and exchange findings through commits or pull requests rather than assuming their chats share context.
