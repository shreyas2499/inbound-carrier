# P6 — Twin runbook: `call_records`

Start with **one** table. Get a row landing from a real call, confirm the shape,
then add `event_log` / `carriers` / `otp_challenges`.

Schema of record is `adapter/twin_models.py`; this file is the how-to.

---

## 1. Why two writers on one row

`call_records` is keyed on the workflow's `run_id` and written twice:

| Writer | Columns | Why it has to be this one |
|---|---|---|
| Workflow (Write to Twin) | everything except the four below | it has the call context |
| Adapter | `loadboard_rate`, `agreed_rate`, `margin_vs_ceiling`, `rounds` | all four derive from `MAX_BUY`, which is stripped from every agent-facing response by design |

Handing those four back to the agent so the workflow could write them would leak
the ceiling: `agreed_rate + margin_vs_ceiling == MAX_BUY`. That is the one number
the negotiation must never expose. So the adapter writes them itself — which is
also the concrete reason every tool body needs to carry `run_id`.

**Write to Twin, not a Workflow Dump table.** A dump only fires on a *successful*
run, so abandoned calls — the ones we most want to count — would silently never
land. `run_id` as the PK makes the upsert idempotent.

---

## 2. Create the tables

**Status: all four created via the SQL Console** (Twin reads the Postgres
catalog, so `CREATE TABLE` registers properly in the Graph tab — no need to use
the Create-table form). Indexes deliberately not added yet.

Naming decision applied across the codebase: the workflow run id is called
**`run_id`** everywhere — `call_records.run_id`, `event_log.run_id`,
`otp_challenges.run_id`, and the key emitted by `obs.call_context()`. The old
`call_id` spelling survives only as an accepted *input* alias on the tool body,
so the workflow can name the chip either way. One name, no translation layer,
joins that just work.

The OTP code column is `code_hash NOT NULL`, not `code`. A column called `code`
with a comment saying "hash this first" is how live secrets end up stored in the
clear; the column name is the constraint.

`id` / `ts` / `first_seen` / `last_seen` carry DB defaults
(`gen_random_uuid()`, `now()`) because the adapter helpers deliberately don't
send them. Those defaults are load-bearing — without them every adapter write
fails on a null primary key.

### call_records

Twin → **Create table** → type **Empty** → name `call_records` → PK `run_id` (text).

Twin column types available: `int8 int4 float8 float4 text boolean timestamp uuid jsonb`

| Column | Type | Null? |
|---|---|---|
| `run_id` | text | **PK** |
| `started_at` | timestamp | |
| `environment` | text | |
| `mc_number` | text | ✓ |
| `carrier_name` | text | ✓ |
| `authority_eligible` | boolean | ✓ |
| `otp_verified` | boolean | ✓ |
| `load_id` | text | ✓ |
| `equipment` | text | ✓ |
| `origin_state` | text | ✓ |
| `dest_state` | text | ✓ |
| `last_action` | text | ✓ |
| `last_rate` | int4 | ✓ |
| `outcome` | text | |
| `outcome_reason` | text | ✓ |
| `handle_time_sec` | int4 | ✓ |
| `call_end_initiator` | text | ✓ |
| `num_tool_calls` | int4 | ✓ |
| `assistant_cut_ratio` | float4 | ✓ |
| `p90_latency_ms` | int4 | ✓ |
| `extracted_agreed_rate` | int4 | ✓ |
| `extracted_rounds` | int4 | ✓ |
| `loadboard_rate` | int4 | ✓ (adapter) |
| `agreed_rate` | int4 | ✓ (adapter) |
| `margin_vs_ceiling` | int4 | ✓ (adapter) |
| `rounds` | int4 | ✓ (adapter) |

Every adapter column is nullable on purpose: the workflow's row lands first and
the adapter fills them in after. A row with nulls there is a call that never got
to money, or one where the adapter write failed — both worth seeing.

---

## 3. Wire the Write to Twin node

Add it **after** the Extract node (`negotiation`), at the end of the post-call
chain. Mode: **Insert or upsert a row**. Table: `call_records`. Conflict key:
`run_id`. Response node: **off** (nothing to return — the call is over).

Map each column to this variable — all inserted with `@`, not `{{ }}`:

| Column | Variable |
|---|---|
| `run_id` | `Current.run_id` |
| `started_at` | `Handle Customer Call.now.iso` |
| `environment` | `Current.execution_environment` |
| `mc_number` | `Verify Carrier Details.mc_number` |
| `carrier_name` | `Verify Carrier Details.legal_name` |
| `authority_eligible` | `Verify Carrier Details.eligible` |
| `otp_verified` | `Verify OTP.verified` |
| `load_id` | `evaluate_offer.load_id` |
| `equipment` | `search_loads.eqtype` |
| `origin_state` | `search_loads.orig_state` |
| `dest_state` | `search_loads.dest_state` |
| `last_action` | `Evaluate Offers.action` |
| `last_rate` | `Evaluate Offers.rate` |
| `outcome` | `call_outcome.response.classification` |
| `outcome_reason` | `call_outcome.response.reasoning` |
| `handle_time_sec` | `Handle Customer Call.duration` |
| `call_end_initiator` | `Handle Customer Call.call_end_initiator` |
| `num_tool_calls` | `Handle Customer Call.num_tool_calls` |
| `assistant_cut_ratio` | `Handle Customer Call.assistant_cut_message_ratio` |
| `p90_latency_ms` | `Handle Customer Call.p90_latency_ms` |
| `extracted_agreed_rate` | `negotiation.response.agreed_rate` |
| `extracted_rounds` | `negotiation.response.negotiation_rounds` |

Leave the four adapter columns unmapped.

Two gotchas already paid for once each:

- `mc_number` comes from the **webhook response**, not `verify_carrier.mc_number`.
  The response is the adapter's normalized digits; the tool input is whatever the
  agent heard.
- A variable only appears in the picker once its node has a **generated output
  schema**. Both AI nodes have one. Re-generate after any change to their params
  or the new field will silently not be mappable.

---

## 4. run_id in the tool bodies  — DONE

All five POST nodes carry `"run_id": @Current.run_id`. That one field is what
lets the adapter know which call it is serving.

**It also broke search.** `search_loads` builds its LOAD_QUERY filters out of the
whole request body, so `run_id` went to the legacy TMS as a filter named `RUN_ID`.
Fixed by stripping `obs.CORRELATION_KEYS` before building filters; regression test
in `tests/test_twin_wiring.py`.

## 5. Adapter side  — DONE

| What | Where |
|---|---|
| `loadboard_rate`, `agreed_rate`, `margin_vs_ceiling`, `rounds` | written on `action == "accept"` in `routes.evaluate_offer` via `twin_helper.update_call_record` |
| true round count | `adapter/call_state.py` — SQLite, keyed `(run_id, load_id)` |
| `carriers` upsert | `routes.verify_carrier` on a found lookup |
| `event_log` | one hook in `obs.register_observability`, covering every `/tools/*` and `/debug/*` call including failures |

Two deliberate calls:

- **event_log is wired in the obs hook, not per route.** Six call sites would each
  have to remember the error paths; the first one anybody forgets is a silent hole.
- **`event_log.response` stores the ADAPTER's response, not the raw upstream
  record.** Narrower than `twin_models.EventLog` originally described, and better:
  the adapter's response has already had MAX_BUY stripped, so no ceiling is written
  to Twin in any form. Raw upstream payloads stay in container logs and `/debug/*`.

## 6. Open questions

- **`book_load` is deliberately deferred to LAST.** The endpoint exists and
  returns a `booking_ref`, but no tool node invokes it, so `outcome='booked'`
  currently means "a rate was agreed", not "the load is booked in the TMS".
  Decision: leave it unwired until everything else is finished — it is the only
  *write* against the TMS, and each successful booking permanently consumes one
  of the finite test loads in the sandbox. Wiring it early would burn the
  inventory the rest of the testing depends on. When it does go in, it needs the
  tool node, a `booking_ref text` column, and the definition of `outcome='booked'`
  tightened to mean an actual TMS commit.
- **Twin REST shape is unverified.** `twin_helper.py` assumes
  `POST /twin/tables/{table}/rows` with `{"values": {...}}`, marked "confirmed
  from docs" — but the docs are gated and no call has ever been made. Smoke-test
  one insert before wiring `log_event` / `upsert_carrier`.
- **Does the post-call chain run when the caller hangs up?** The whole case for
  Write to Twin over Workflow Dump is capturing abandoned calls. Worth proving
  early — test-scenarios 9.6 / 9.7.

---

## 7. Northstars

Every KPI filters `environment = 'production'`. Twin is one database per
workspace with no dev/staging/prod separation of its own, so without that filter
every test call pollutes the numbers.
