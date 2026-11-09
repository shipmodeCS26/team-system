# Inventory Ops Runbook (nightly EOD run + hourly Slack watch)

This is the full procedure the assistant follows for the six stores. Any assistant (Claude, GPT, or a person) can follow it.

**Keep private data out of this repository.** The repository is public. Sheet IDs, Slack channel and user IDs, order numbers, customer details and stock numbers live in a private reference file that Gly keeps (`OPS_PRIVATE_REFERENCE.md`, not committed). Wherever this runbook says `<…>`, look the value up there.

Stores (6): **Muravai, Fascial Labs, Neurosmile, PuraVita, Onset, Vitalia.**
- The first four have a ShipSidekick (SSK) API key. Their sales and stock are pulled automatically.
- Onset and Vitalia have **no SSK key**. Their sales come from exports Gly loads into their Google Sheets. Their stock comes from those sheets plus Orlando's physical counts and receipts.

---

## 1. Standing rules (never break these)

1. **Client channels are DRAFTS ONLY.** Never send or post in a client channel. Always put the full text of every draft in the reply to Gly too, because drafts don't always show up for her. Slack Connect channels (Muravai, Fascial) block posting entirely: draft + text only.
2. **Only `#inventory-updates` (internal) may be sent automatically**, and only when every auto-send gate passes (section 4).
3. **Accuracy first** ("triple check", "we need a sure answer"):
   - Every fact in a draft must trace to a source checked *this run*: a Slack message, a sheet row, or a fresh SSK pull. That covers quantities, dates, POs, tracking, and words like "last", "complete" or "still waiting on".
   - Re-add every total (opening − shipped + received = remaining) before saving.
   - If you can't confirm something, leave it out. Write "we're confirming with the warehouse and will update you", or ask Gly.
4. **Only Orlando (warehouse) confirms** arrivals, shelf counts and backlog. A computed number is never "confirmed" until Orlando's physical count matches it.
5. **Never state the cause of a discrepancy** unless it has been confirmed.
6. **Never change Onset "Initial Stock"** (the starting count) without Gly's OK. Count resets need Gly's approval.
7. **Every receipt row needs a received date.**
8. **Muravai kits:** the "Shower Hose & Connector Set" is a bundle = 1 Shower Hose + 1 Shower connector + 1 "1x Teflon Tape".
   - Arriving kits are received as the **3 components**, never into the kit SKU.
   - In SSK: kits = Teflon Tape; standalone hoses = hose − tape; brackets = connector − tape.
9. **SSK changes:**
   - Receipts are entered by **creating a PO and receiving it** (keeps a record of when stock arrived).
   - Manual inventory adjustments are used **only** for count corrections.
   - Before any SSK instruction: re-pull levels and give before → change → after per SKU.
10. **No ShipMode mention** in the Vitalia channel or other Revly spaces.
11. **Never write "8)" or "B)"** (Slack turns them into emoji). Never put a closing bracket right after an 8 or B: write "on Oct 8" or "Oct 8 –", not "(Oct 8)".
12. **One draft per channel or thread.** Fold unsent drafts into the new one. After saving, check the result has a `draft_id`. If not, an older draft is blocking: give Gly the full text and name the channel.
13. **Time zones:** Slack "CST" labels = UTC+8. ET = UTC−5 (since Nov 1). Report day = yesterday in ET.
14. **Simple answers** when Gly asks for simple answers.
15. Never commit secrets or client data. Never paste or ask for API keys.

---

## 2. Hourly Slack watch

1. Read the time of the last check (saved Unix timestamp).
2. Search Slack for every message since then: all channels, all DMs and group DMs, and threads (including replies under our own posts). Also read `#inventory-updates` and the Orlando DM directly.
   - Search tip: a date filter like `after:<date>` works on calendar days in the workspace time zone. Always search from *yesterday's* date and then keep only messages newer than the last check, or late-evening messages get missed.
3. Ignore Gly's own messages and bots, but note what Gly already answered so you don't duplicate it.
4. Sort each new message:
   - **(a)** needs a reply from us: client question, a request from Orlando, Carlos or Nick, or anything addressed to Gly or ShipMode.
   - **(b)** inventory information to log: tracking, ETA, carton counts, receipts, counts, PO changes, SKU changes, exports.
   - **(c)** no action.
5. **Log inventory info first.** Update the right client sheet's Incoming Stocks rows (tracking, cartons, status, notes, received date). Re-read every write.
6. **For each (a):** build the reply only from sources checked this run. Save it as a **thread reply draft**, never sent.
7. Save the new check time.
8. Report to Gly:
   - For each item: who and where, what they asked, the draft id (or the text to paste), sheet changes made, and anything that needs her decision.
   - If nothing is new, say only: "No new Slack items."

---

## 3. Nightly EOD run (12:10 AM ET, for yesterday)

### Step 0: Read all Slack first
- Read every channel, DM and thread since the last run.
- Check what Gly actually **sent** since then: her sent numbers become "the last report" (the Opening numbers for the next report).
- Apply inventory updates to the sheets before computing anything.
- Answer unanswered requests with a draft.

### Step 1: Pull SSK
Run (from a checkout that has `ssk_source.py` and the SSK keys in the environment):
```
cd ops/nightly
PYTHONPATH=<repo root> python3 nightly.py <YYYY-MM-DD> > n<MMDD>.out
```
Output: `nightly_<date>.json` (`units_by_day`, `orders_by_day`) and `LV` lines in the `.out` file with SSK on hand / available / committed / incoming per SKU.

Checks (stop if any fails):
- **Unmapped SKU** → stop and add it to the map in `nightly.py`.
- **Empty inventory list** for a client that shipped → stop and re-pull.
- **On-hand drop** since yesterday must equal yesterday's mapped units. For Muravai, the hose drop = kits + standalone hoses, and the connector drop = kits + brackets.
- **On-hand increase** = a receipt. Find it in Slack or Incoming Stocks before reporting it.

14-day average = sum of `units_by_day` over the last 14 calendar days ÷ 14.

### Step 2: Roll every sheet to yesterday (re-read every write)
- **Muravai:**
  - EOD Sales rows A:E: date serial, SKU code MUR001–005, name, qty, "EOD". Five rows per day, next empty rows.
  - Dashboard `B4` = yesterday (date serial).
  - Receipts tab: only APPROVED rows change on hand.
- **Fascial / Neurosmile:**
  - One labelled summary row per day in Daily Sales:
    - A: `SSK-API-DAILY-TOTAL-YYYY-MM-DD`
    - B: m/d/yy
    - C: org
    - D: `DAILY TOTAL: N orders (ShipSidekick API …)`
    - S: `summary`
    - U: `No`
    - X: total units
    - Y: `Nx <name> (<SKU>)`
  - Fascial bundles are exploded into units: 2-pack = 2 TrueForm, 3-pack = 3 TrueForm, Bundle-3 = 2 TrueForm + 1 TrueSleep, TrueSleep x3 = 3 TrueSleep.
  - Days with no shipments get no row.
  - Fascial's grid is full: add rows by appending (append_values).
  - Dashboard `B4` = yesterday.
- **PuraVita:** Dashboard `B4` = yesterday.
- **Onset / Vitalia:**
  - Check the latest Daily Sales date and Dashboard `B4`. Set `B4` = yesterday **only** if sales are loaded through yesterday.
  - Onset Daily Sales has one row per order. Items are in column Y, separated by `;`. Parse with `(\d+)x .*\(([^)]+)\)`. Check the parsed total equals column X (Num Items) on every row.
  - Onset receipts show immediately (no as-of-date filter).
  - **Vitalia Incoming Stocks rows 2–10 have merged cells.** Write new rows below them.

### Step 3: Daily audit
- (a) Every Dashboard "remaining" = SSK on hand per product, or the difference is a documented receipt not yet in SSK or a documented open gap. Name each one.
- (b) `B4` and the latest sales row = yesterday for **all six** stores, with no missing dates.
- (c) Recount yesterday's units per SKU against the Dashboard "Today's Orders".
- (d) SSK sanity checks:
  - available ≤ on hand
  - committed backlog with no labels on a working day
  - unexplained incoming
  - products shipped with 0 stock
- (e) Re-read every number in every draft against its source.
- (f) Report the audit as: ✅ matched / 🔧 fixed / ❗ needs Gly or Orlando (exact place).

### Step 4: Reports (drafts + full text in the reply)
One post per client channel. Onset and Vitalia get a post only with confirmed info.

Format:
```
:bar_chart: <Client> Inventory Update — <date> EOD / <date> Opening @channel
<Product>
Opening: … | Received: … (only if any) | Shipped: … | Remaining: …
Average demand (last 14 days): …/day | Estimated cover: … days (est.)
Total shipped X units across Y orders
Total remaining …
:calendar: Weekly forecast (3–5 lines, each marked (est.))
```
- Opening = the last report Gly actually **sent**.
- **Consistency rule:** don't contradict our last posted recommendation unless the 7-day pace moved more than ~15% or there is new confirmed info. If so, write "Updated from our <date> recommendation because …".

`#inventory-updates` (internal):
```
Good morning team! Inventory for <today> (EOD <report day>).
✅ Updated: final numbers for all six stores
⏳ Not updated / to confirm: anything not current
:memo: Update: receipts still to enter in SSK, run-outs, orders needed
```

### Step 5: Summary for Gly
Cover:
- Slack updates and sheet changes
- the audit
- whether `#inventory-updates` was sent (link), or which gate/store failed
- SSK changes still needed (before → change → after; PO receipt vs manual adjustment)
- recommendation changes
- open questions
- unsent drafts
- full client report texts
- new mistakes and the rule added for each

---

## 4. Auto-send gates for `#inventory-updates`
Send automatically **only if all pass**. Otherwise save a draft and tell Gly exactly which gate and store failed.
1. **All six stores** rolled to the report day and re-read. Onset and Vitalia need sales loaded through the report day, `B4` = report day, and every Orlando receipt or count applied with a date.
2. Every product's sheet remaining = SSK on hand, or the difference is exactly a documented receipt not yet in SSK or a documented open gap (named in the post).
3. Every total re-added.
4. SSK pull complete: no unmapped SKUs, no empty level list, no truncation.
5. Client reports use the same numbers as the post.
6. Nothing unconfirmed in the post. Onset and Vitalia numbers trace to their sheets plus Orlando's counts.
7. Slack read since the last run, and any new receipt or count applied.

---

## 5. Mistake log (never repeat)
1. Received Muravai kits into the kit SKU → always receive components.
2. Gave a kit adjustment without checking bundle stock vs buildable kits.
3. Two drafts in one channel, and the older wrong one got sent → one draft per channel.
4. Onset dashboard parsing missed NAD+ and Canister → parse per SKU by item code.
5. Left sheets days behind → roll every sheet every night.
6. Onset threshold math wrong → recompute before saving.
7. Note written on the wrong row → re-read after every write.
8. Missed the Receipts tab / double-counted a kit box → check Receipts first.
9. Assumption in a client reply, and the wrong source named → facts only, true source.
10. Missed Orlando's real-time updates → always do step 0.
11. Wrong CST conversion → CST = UTC+8.
12. Unmapped SKUs silently dropped → map them and reconcile to the SSK on-hand drop.
13. Inventory-level filter read the wrong fields and returned nothing → an empty list stops the run.
14. Guessed "last box of the order" → verification rule.
15. "Oct 8)" turned into an emoji → never write "8)".
16. Called Onset "updated" with a date missing → check for missing dates.
17. Stated an unconfirmed SKU action → only confirmed actions.
18. Drafts without a draft_id never appeared → check draft_id, give the text if it's missing.
19. Carried "still waiting on" lines forward when the client had already sent the info → re-read the client channel and Incoming Stocks before every report.
20. Total shipped off by 1 → re-add totals.
21. Almost reported stock that conflicted with a physical count → never post stock that conflicts with a count.
22. Slack Connect drafts never appeared → always give the text in the reply.
23. A receipt row with no received date broke a reset → every receipt row needs a date.
24. Called a computed number "likely right" before the recount → computed ≠ confirmed.
25. Stated SSK status for a client we can't pull → only state SSK status for clients pulled this run.
26. The auto-send gate listed only 4 stores → every gate and audit covers all six.
27. A Slack date filter missed late-evening messages → search from yesterday's date and filter by timestamp.
28. Writes into merged cells (Vitalia Incoming Stocks rows 2–10) were swallowed → write below merged blocks and re-read.
29. Split Onset items on "," instead of ";" → parse with `[;,]` and check against column X on every row.
