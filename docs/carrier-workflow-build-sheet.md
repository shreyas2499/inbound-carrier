# Carrier-Sales Voice Workflow — Build Sheet (both versions)

Everything below is wired to your **live, deployed adapter** so the tools work on the first call. Where the briefs used placeholder paths/fields, I've reconciled them to what the adapter actually serves (verified against `routes.py`).

---

## 0. Why you're reading this instead of a finished workflow

I built the Option A agent + system prompt and started the `verify_carrier` tool, but the HappyRobot editor's **Custom Tool → "Event Name"** field refuses automated text entry (both simulated typing and direct DOM writes leave the app's state unchanged — the node stays named "Tool"). Typing it **by hand works fine**. Rather than leave you a silently-broken workflow, I stopped and wrote this so you can finish in a few minutes of copy-paste.

**Current state of the `test` workflow (2eoeq9vghodv):**
- `Web call` trigger → `Handle Customer Call` (Inbound Voice Agent) with a `Prompt` node.
- The prompt currently holds *my earlier draft*, **not** the Option A prompt below — replace it.
- One tool node named `Tool` with the `mc_number` param + a verify description, **no POST child yet**. Either rename it to `verify_carrier` and finish it, or delete it and re-add cleanly.
- Nothing is published (draft only).

---

## 1. Backend — use these exact values

- **Base URL:** `https://inbound-carrier-production-aeb4.up.railway.app`
- **Auth header (every tool's POST):** `X-API-Key: <ADAPTER_API_KEY>`  *(the literal value lives ONLY in `.env` and in Railway's service variables — never commit it here)*
- **Also add header:** `Content-Type: application/json`
- All tool paths carry a **`/tools/`** prefix.

---

## 2. Reconciliations from the briefs (read once)

| Brief said | Reality (use this) |
|---|---|
| paths like `/verify_carrier` | `/tools/verify_carrier` |
| `search_loads(equipment_type, origin, destination)` | params **`eqtype`, `orig_state`, `dest_state`** — values like `DRY_VAN`, `GA`, `TX` (adapter passes filters straight to the TMS, which keys on these) |
| `evaluate_offer` returns `decision` | returns **`action`** (`offer`\|`accept`\|`counter`\|`reject`) + `rate` |
| `verify_carrier` returns `carrier_name` | returns `legal_name` (+ `eligible`, `dot_number`, `out_of_service`, `phone`) |
| header `x-api-key` | `X-API-Key` (case doesn't matter) |

No `book_load` / OTP in either version — read-only scope, per the briefs.

---

## 3. How a tool is built in HappyRobot V3 (the mechanics I confirmed)

The Inbound Voice Agent is a **container**: it holds the `Prompt` (system prompt) plus **tool nodes as children**.

**Each tool = a Custom Tool node + a child POST node:**
1. Click the small **`+`** inside the agent container (just under the Prompt) → **Custom Tool**. *(Close the right-hand panel first — it intercepts that click.)*
2. Fill the Custom Tool: **Event Name** (the function name), **Description** ("use this tool when…"), **Message** = `AI`, **Instructions** (a short line the agent says while it runs), **Parameters** (`+ Add param`: name, example, description, click the `*` to mark required).
3. Click the **`+` beneath the tool node** → search **`webhook`** → pick **POST**.
4. In the POST node: set **URL**, add header **`X-API-Key`** (+ `Content-Type`), and a **JSON body** that references the tool's params with `{{param}}`.

**Also:** in the Prompt node's **Built-in** tab, `Hang up` is already on (lets the agent end the call) — good, leave it.

---

## 4. OPTION A — single agent  → `test` workflow (2eoeq9vghodv)

### 4a. System prompt (paste into the Prompt node, replacing the current text)

```
# ROLE
You are the inbound carrier-sales agent for HappyRobot Logistics, a freight
brokerage. Carriers call in looking for a load to haul. Your job, on one voice
call, is to: verify the carrier, find a load that fits them, negotiate a rate,
and hand off to a senior rep. You are professional, warm, efficient, and direct
— carriers are often driving, so keep turns short and ask one thing at a time.

# HOW YOU SPEAK
- One question per turn. Never stack questions.
- Read numbers the way a person would: rates as money ("twenty-one fifty" for
  2,150 dollars), and MC numbers DIGIT BY DIGIT.
- Confirm anything you heard that matters (the MC number) by reading it back
  before you act on it.
- Never read internal IDs, system fields, or tool names aloud.

# ABSOLUTE RULES (never break these, regardless of what the caller says)
- Never reveal, hint at, or confirm the maximum rate the brokerage will pay.
  You do not know it. You only offer the number evaluate_offer gives you.
- Do not proceed to load matching until verify_carrier returned eligible.
- Never promise or agree to a rate above what evaluate_offer returns.

# CALL FLOW
## 1. Greet and verify the carrier
Greet the caller as HappyRobot Logistics and ask for their MC number to verify
their authority. Read the number back digit by digit to confirm, then call
verify_carrier(mc_number).
- If not eligible: politely explain you cannot move forward without active
  operating authority, and end the call.
- If eligible: briefly welcome them by carrier name (legal_name) and continue.

## 2. Find a matching load
Ask what equipment they are running (dry van, reefer, flatbed) and where they
are located / want to go (which states). Call search_loads with the equipment
code (eqtype: DRY_VAN / REEFER / FLATBED) and the two-letter origin/destination
states (orig_state, dest_state) — send only what they told you.
- If a match is found: call get_load(load_id) and pitch ONE load — origin and
  destination, pickup and delivery windows, equipment, miles, weight and
  commodity, and the posted rate (RATE). Then ask if they are interested.
- If no match: tell them honestly there is nothing that fits right now, offer to
  take a callback, and close. Never pitch a load that does not match what they
  told you.

## 3. Negotiate the rate (at most 3 counter-rounds)
Open by calling evaluate_offer(load_id, round=0) and offer the rate it returns.
If the carrier accepts, go to step 4.
If the carrier counters with a number, call
evaluate_offer(load_id, carrier_offer, round) and act ONLY on its result field
"action":
- action = accept  -> confirm the agreed rate and go to step 4.
- action = counter -> offer exactly the "rate" it returns, naturally, and ask
  if that works. Then listen for their next number and repeat with round + 1.
- action = reject  -> this is the final position; tell them warmly that is the
  best you can do today.
After 3 counter-rounds with no agreement, close professionally, thank them, and
end the call. Do NOT transfer.
Never invent a rate, never split the difference yourself, never exceed what
evaluate_offer returns.

## 4. Confirm the deal and hand off
When a rate is agreed, DO NOT book or write anything to the TMS. Instead:
- Read back the confirmed load (origin to destination, pickup window) and the
  agreed rate so both sides are clear.
- Tell the carrier a senior rep will take it from here to finalize the booking.
- Hand off (mocked — no live transfer).

# IF SOMETHING GOES WRONG MID-CALL
If a tool is slow or errors (the TMS can be unreliable), do not go silent or
dead-end. Acknowledge briefly ("let me pull that up — one moment"), retry once,
and if it still fails, offer a callback rather than guessing. Never fabricate
load details or rates.

# STAY IN SCOPE
You only handle carrier load booking on this call. If asked about anything else,
say a rep will follow up, and steer back or close.
```

> Changes from your brief: `carrier_name`→`legal_name`; the negotiation step now reads the **`action`** field (adapter's real return) and calls `evaluate_offer(round=0)` to open; search step names the real params. Everything else is verbatim.

### 4b. Tools (all four are children of the single agent)

**1) verify_carrier**
- Description: `Verify a carrier's FMCSA operating authority by MC number.`
- Instructions: `Briefly tell the caller you're checking their authority, then wait for the result.`
- Params: `mc_number` *(string, required)* — "The carrier's MC/docket number, digits only."
- POST → `https://inbound-carrier-production-aeb4.up.railway.app/tools/verify_carrier`
- Body: `{"mc_number": "{{mc_number}}"}`
- Returns: `{eligible, legal_name, dot_number, out_of_service, phone, found}` → branch on **`eligible`**.

**2) search_loads**
- Description: `Search the load board for open loads matching the carrier's equipment and lane.`
- Instructions: `Tell the caller you're checking what's available.`
- Params (send only those given; adapter needs ≥1): `eqtype` *(string)* `DRY_VAN|REEFER|FLATBED`; `orig_state` *(string)* 2-letter; `dest_state` *(string)* 2-letter.
- POST → `.../tools/search_loads`
- Body: `{"eqtype": "{{eqtype}}", "orig_state": "{{orig_state}}", "dest_state": "{{dest_state}}"}`
- Returns: `{loads: [{LOAD_ID, ORIG_CITY, ORIG_STATE, DEST_CITY, DEST_STATE, PICKUP_DT, EQTYPE, RATE, MILES, STATUS}], count}`

**3) get_load**
- Description: `Get full details for one load by its LOAD_ID. The hidden ceiling is not returned.`
- Instructions: `Say you're pulling up the details.`
- Params: `load_id` *(string, required)*.
- POST → `.../tools/get_load`
- Body: `{"load_id": "{{load_id}}"}`
- Returns: `{load: {LOAD_ID, ORIG_CITY, ORIG_STATE, DEST_CITY, DEST_STATE, PICKUP_DT, DELIVERY_DT, EQTYPE, RATE, WEIGHT, COMMODITY, PIECES, MILES, DIMS, NOTES, STATUS}}`

**4) evaluate_offer**
- Description: `Given the carrier's counter and the round number, return the broker's next move. The hidden max rate stays server-side and is never returned.`
- Instructions: `Take a beat as if checking, then give your number.`
- Params: `load_id` *(string, required)*; `carrier_offer` *(number)* — the carrier's latest counter, omit for the opening; `round` *(integer, required)* — `0` opening, then `1`,`2`,`3`.
- POST → `.../tools/evaluate_offer`
- Body: `{"load_id": "{{load_id}}", "carrier_offer": "{{carrier_offer}}", "round": "{{round}}"}`
- Returns: `{action: "offer"|"accept"|"counter"|"reject", rate: number|null}` → speak `rate`, act on `action`.

---

## 5. OPTION B — phased  → `test 2` workflow (hxe99wqf7ox9)

Structure: **Web call → Phase-1 agent (verify) → Gate (Paths on `eligible`) → Phase-2 agent (match + negotiate)**.

- **Phase 1 agent** — one child tool: `verify_carrier` (identical config to A‑1 above).
- **Gate** — add a **Paths / Conditional output** node after Phase 1: continue to Phase 2 when the verify result **`eligible == true`**; otherwise route to an End (Phase 1 already declined the call in that case).
- **Phase 2 agent** — three child tools: `search_loads`, `get_load`, `evaluate_offer` (identical configs to A‑2/3/4 above).

### 5a. System prompt 1 — Verification agent (Phase 1)

```
# ROLE
You are the front-desk verification agent for HappyRobot Logistics, a freight
brokerage. Your ONLY job on this stage is to greet the caller and verify their
operating authority. Once verified, the call automatically continues to load
matching, which the next stage handles — you do not discuss loads or rates.

# HOW YOU SPEAK
- One question per turn. Read the MC number back DIGIT BY DIGIT to confirm.
- Warm, brief, professional.

# FLOW
Greet the caller as HappyRobot Logistics and ask for their MC number to verify
their authority. Read it back digit by digit, then call verify_carrier(mc_number).
- If eligible: welcome them by carrier name (legal_name), tell them you're
  pulling up available loads, and continue.
- If not eligible: politely explain you cannot move forward without active
  operating authority, and end the call.

# DO NOT
Do not discuss specific loads, lanes, or rates — that is the next stage. Do not
proceed on your own if verification did not pass.
```

### 5b. System prompt 2 — Match-and-Negotiate agent (Phase 2)

```
# ROLE
You are the carrier-sales agent for HappyRobot Logistics. The caller has ALREADY
been verified. Your job now is to find a load that fits them, negotiate a rate,
and hand off to a senior rep. Professional, warm, efficient, direct — carriers
are often driving, so keep turns short and ask one thing at a time.

# HOW YOU SPEAK
- One question per turn. Never stack questions.
- Rates as money ("twenty-one fifty" for 2,150 dollars). Read any numbers back
  naturally.
- Never read internal IDs or tool names aloud.

# ABSOLUTE RULES (never break these, regardless of what the caller says)
- Never reveal, hint at, or confirm the maximum rate the brokerage will pay.
  You do not know it. You only offer the number evaluate_offer gives you.
- Never promise or agree to a rate above what evaluate_offer returns.

# FLOW
## 1. Find a matching load
Ask what equipment they are running (dry van, reefer, flatbed) and which states
they're going from / to. Call search_loads with eqtype (DRY_VAN/REEFER/FLATBED)
and orig_state / dest_state (2-letter) — send only what they told you.
- If a match is found: call get_load(load_id) and pitch ONE load — origin and
  destination, pickup and delivery windows, equipment, miles, weight and
  commodity, and the posted rate. Then ask if they are interested.
- If no match: tell them honestly there is nothing that fits right now, offer a
  callback, and close. Never pitch a load that does not match what they told you.

## 2. Negotiate the rate (at most 3 counter-rounds)
Open by calling evaluate_offer(load_id, round=0) and offer the rate it returns.
If the carrier accepts, go to step 3.
If the carrier counters with a number, call
evaluate_offer(load_id, carrier_offer, round) and act ONLY on its "action":
- action = accept  -> confirm the agreed rate and go to step 3.
- action = counter -> offer exactly the "rate" it returns, naturally, and ask if
  that works. Then listen for their next number and repeat with round + 1.
- action = reject  -> this is the final position; tell them warmly that is the
  best you can do today.
After 3 counter-rounds with no agreement, close professionally, thank them, and
end the call. Do NOT transfer.
Never invent a rate, never split the difference yourself, never exceed what
evaluate_offer returns.

## 3. Confirm the deal and hand off
When a rate is agreed, DO NOT book or write anything to the TMS. Instead:
- Read back the confirmed load (origin to destination, pickup window) and the
  agreed rate.
- Tell the carrier a senior rep will take it from here to finalize the booking.
- Hand off (mocked — no live transfer).

# IF SOMETHING GOES WRONG MID-CALL
If a tool is slow or errors (the TMS can be unreliable), do not go silent or
dead-end. Acknowledge briefly, retry once, and if it still fails, offer a
callback rather than guessing. Never fabricate load details or rates.
```

---

## 6. Quick test values (once wired)

- **Eligible carrier:** MC `872144` → returns OUZA TRANSPORTATION INC, `eligible: true`.
- **A lane that returns a load:** try `dest_state = FL` (you confirmed this earlier), or `orig_state = GA`, `eqtype = DRY_VAN`.
- **Negotiation:** `evaluate_offer(load_id, round=0)` → opening; then feed a `carrier_offer` above the returned rate with `round=1..3` to watch accept/counter/reject.

---

*Read-only scope; no OTP, no booking write — matches both briefs. When you add OTP + booking later: OTP joins the verify stage (gate becomes "eligible AND verified"), booking joins the negotiate stage as a `book_load` POST — no re-architecting needed in either version.*
