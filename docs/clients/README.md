# Client inventory registry

Each client has one inventory sheet and one client Slack channel. Never read from or write to another client's sheet or channel when working on a client.

| Client | Inventory sheet | Client channel |
|---|---|---|
| Fascial Labs | [1nyDTUYadQL1BF9iMbMvDn8Kmm30DAoAaxMziNUnpOdw](https://docs.google.com/spreadsheets/d/1nyDTUYadQL1BF9iMbMvDn8Kmm30DAoAaxMziNUnpOdw/edit?gid=1033324667) | #fascial-labs `C0BML8UK8BS` |
| PuraVita | [1T_zVqpjp5QxZ-qiPEslg5ZHqdJ5OSPCooOP1G1nX8Wk](https://docs.google.com/spreadsheets/d/1T_zVqpjp5QxZ-qiPEslg5ZHqdJ5OSPCooOP1G1nX8Wk/edit?gid=1033324667) | #pura-vita `C0BLVMYLJR2` |
| Neurosmile | [1YVpjBKdMCjD-DqGOb54tinFLgnVFDARshj84xkHkTvQ](https://docs.google.com/spreadsheets/d/1YVpjBKdMCjD-DqGOb54tinFLgnVFDARshj84xkHkTvQ/edit?gid=1033324667) | #neuro-smile `C0BMLB3G5B2` |
| Muravai | [154BWUxREwfwpFSBp14C25poMZ142HjLaYWPMtp5jyCk](https://docs.google.com/spreadsheets/d/154BWUxREwfwpFSBp14C25poMZ142HjLaYWPMtp5jyCk/edit?gid=1033324667) | #muravai- `C0BMLC70672` |

Internal channel shared by all clients: #inventory-updates `C0C2G9MBASH`. Warehouse updates (receipts, counts, found stock) are posted there by Orlando and the warehouse team.

Internal channel for reships, shared by all clients: #reshipment-requests `C0C7GSQ11PH` (created Oct 8, 2026; members Gly, Orlando, Carlos, Nick). Gly forwards client reship requests to the warehouse there. Read it along with #inventory-updates, act only on requests that clearly name the client, and track each request until the warehouse confirms it shipped. For Muravai, a confirmed reship goes in the sheet's Adjustments & Reships tab.

## Required reading before any inventory update or report

Before updating a client's sheet, auditing it, or drafting its report, read both:

1. **#inventory-updates** (`C0C2G9MBASH`), including threads. Act only on messages that clearly name that client or its products. Clients are sometimes misspelled (for example "Facial Labs" for Fascial Labs). If the client is unclear, ask instead of writing.
2. **The client's own channel**, including threads, for shipment tracking, quantities, ETAs, reships, and warehouse notes.
3. **#reshipment-requests** (`C0C7GSQ11PH`), including threads, for that client's reship requests and the warehouse's replies.

Log anything relevant in that client's sheet first, then make the report match the sheet. Check the channel's last posted report against the sheet, and call out any mismatch.

## End-of-day reports

Use the usual EOD format for each client: stock per product, units shipped that day, daily demand, days of cover, status, incoming shipments, and the reorder action. Use `@channel` as usual, but never tag individual people in EOD reports. Client channels are Slack Connect, so reports are saved as drafts for Gly to review and send.

**New stock received:** before an EOD reports a delivery, confirm which PO or shipment it came from: match the tracking number on the boxes, or get the PO from the warehouse (Orlando/Carlos). In the report, name the PO and tracking number. Say whether it was a full or partial delivery, and if partial, list what is still to come. If the PO isn't confirmed, report the quantities only, say the PO is being confirmed, and don't guess. Update the sheet's Incoming Stocks row for that PO at the same time.

## Client-specific rules

- Muravai: follow [`muravai/RULES.md`](muravai/RULES.md) and the sheet's own Rules & Control tab. Inventory changes only through approved Receipts, Manual Counts and Adjustments & Reships rows, and every change is logged in the Change Log tab.
- Neurosmile: orders often contain both products. Count each product's quantity from the item text (`Initial Stock` column K "Order Item Match"), never the order's total item count.
