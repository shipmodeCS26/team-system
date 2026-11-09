# Running this with GPT (or any other assistant)

Use this when Claude is unavailable.

## What you need
1. **This runbook:** `docs/runbooks/INVENTORY_OPS_RUNBOOK.md`.
2. **Gly's private reference file** (`OPS_PRIVATE_REFERENCE.md`, *not* in GitHub). It holds:
   - sheet IDs, Slack channel and person IDs, and row positions
   - the latest stock numbers and open items
3. **Access to the data.** GPT can only work with what it can reach:
   - If GPT has Google Sheets and Slack connectors, it can read them itself.
   - If not, paste or upload: yesterday's Slack messages, the relevant sheet tabs (as CSV), and the SSK numbers.
4. **SSK numbers.** The `ops/nightly/nightly.py` script needs a machine with the ShipSidekick keys set up.
   - Without that, open ShipSidekick → Inventory and copy on hand / committed / available per SKU for Muravai, Fascial, Neurosmile and PuraVita.
   - Then open Shipments and copy yesterday's units per SKU.

## Prompt to paste into GPT (start of each session)
```
You are my inventory-ops assistant for six e-commerce stores (Muravai, Fascial Labs, Neurosmile, PuraVita, Onset, Vitalia) at a 3PL warehouse.
Follow the runbook below exactly, including the standing rules and the mistake log.
Accuracy is critical: every number must come from data I give you in this chat. Re-add every total. If something can't be confirmed, say so and ask me instead of guessing.
Never present a draft as sent. Client messages are drafts for me to review and send.
Write in short, plain sentences. Never write "8)" or "B)".

[paste INVENTORY_OPS_RUNBOOK.md here]

[paste OPS_PRIVATE_REFERENCE.md here: current stock, last report sent, open items]
```

## Nightly run, one prompt at a time
1. "Here are all Slack messages since the last run: [paste]. Step 0: list what's new, what I already sent, and any inventory info to log."
2. "Here are yesterday's shipments per SKU and current SSK levels: [paste]. Step 1: check every on-hand drop matches shipments and flag any increase."
3. "Here are the Dashboard/Daily Sales tabs: [paste]. Step 2–3: tell me exactly which cells to write, then run the audit."
4. "Step 4: write each client report and the #inventory-updates post, using the last report I sent as Opening: [paste last sent report]."
5. "Step 5: give me the summary, the SSK changes needed, and any new mistakes to add to the log."

## Hourly Slack check, one prompt
"Here are the new Slack messages since [time]: [paste]. Follow section 2 of the runbook: which ones need a reply (write the draft), which have inventory info (tell me which sheet row and cells to update), and which need no action."

## After each session
Copy any new open items, numbers Gly sent, and new mistakes back into `OPS_PRIVATE_REFERENCE.md`. Add new mistakes to the runbook's mistake log too, so the next session starts current.
