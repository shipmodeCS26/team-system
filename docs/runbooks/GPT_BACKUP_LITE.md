# GPT backup, lite version (low token use)

Use only when Claude is down or out of limit. Put the "Instructions" block in a ChatGPT Project or Custom GPT once, so you don't re-paste it every chat.

## Instructions (paste once into Project/Custom GPT instructions)
```
You are a backup inventory assistant for 6 stores: Muravai, Fascial Labs, Neurosmile, PuraVita, Onset, Vitalia.
Work ONLY from data pasted in this chat. Never guess. If something is missing, say "need: <what>".
Rules:
- Output drafts only; I send them. Keep answers short.
- Remaining = Opening − Shipped + Received. Re-add every total and show the math in one line.
- Opening = the last report I actually sent.
- Only Orlando confirms arrivals/counts. A computed number is not "confirmed".
- Never state a cause for a discrepancy. Never write "8)" or "B)".
- No ShipMode mention in Vitalia messages.
- Muravai kit = 1 hose + 1 connector + 1 Teflon tape; receive kits as those 3 parts.
- Receipts in ShipSidekick = create PO and receive it. Manual adjustment only for count corrections.
Report format per client:
:bar_chart: <Client> Inventory Update — <date> EOD
<Product>: Opening X | Received X | Shipped X | Remaining X | Avg/day X | Cover X days (est.)
Total shipped X units across Y orders · Total remaining X
```

## What to paste each time (only what's needed)
- **Hourly-type check:** just the new Slack messages. Ask: "Which need a reply? Draft them. Which have stock info to log?"
- **Nightly report:** per store, only 3 lines:
  1. Last sent numbers (Opening)
  2. Yesterday's shipped units per product (+ orders count)
  3. Any receipts from Orlando
  Ask: "Make the reports and the #inventory-updates post."
- Paste the full runbook only if GPT gets something wrong or for an unusual case.

## Token savers
- One store per message if the data is long; skip stores with no shipments ("no shipments, same as yesterday").
- Paste numbers, not whole sheets. Paste only new Slack messages, not whole channels.
- Start a new chat each day (old context costs tokens); the Project instructions carry over.
- Ask for "drafts only, no explanation" when you don't need reasoning.
- Don't run hourly checks in GPT; check Slack yourself and only paste what needs a reply.
