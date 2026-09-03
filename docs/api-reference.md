# TMS Adapter — API Reference

Base URL (local): `http://localhost:8000`
Auth: every `/tools/*` endpoint requires header `X-API-Key: <ADAPTER_API_KEY>`. `/health` is open.
Content type: requests and responses are `application/json`.

## Summary

| Endpoint | R/W | Request body | Success response body |
|---|---|---|---|
| `GET /health` | read | — | `{"status":"ok"}` |
| `POST /tools/search_loads` | read | `{"orig_state":"FL","eqtype":"REEFER","max_results":5}` (≥1 filter) | `{"count":2,"loads":[ {load summary}, … ]}` |
| `POST /tools/get_load` | read | `{"load_id":"LD1001"}` | `{"load":{ …full load, no MAX_BUY… }}` |
| `POST /tools/evaluate_offer` | read | `{"load_id":"LD1001","round":1,"carrier_offer":1900}` | `{"action":"counter","rate":1820}` |
| `POST /tools/book_load` | **write** | `{"load_id":"LD1001","mc_number":"872144","agreed_rate":1800}` | `{"status":"booked","booking_ref":"BR…"}` |

`book_load` is the only endpoint that mutates state — and the mutation lives in the TMS, keyed to your token. It is irreversible (no cancel command exists).

---

## GET /health

Liveness probe. No auth, no body.

**Response `200`**

| Field | Type | Notes |
|---|---|---|
| `status` | string | always `"ok"` |

---

## POST /tools/search_loads  (read)

Searches the open board. At least one filter is required.

**Request body** — any combination of the following (keys are case-insensitive):

| Field | Type | Required | Notes |
|---|---|---|---|
| `orig_state` / `dest_state` | string | ≥1 filter | 2-letter state; matched strictly |
| `orig_city` / `dest_city` | string | — | matched loosely |
| `orig_zip` / `dest_zip` | string | — | matched strictly |
| `eqtype` | string | — | e.g. `DRY_VAN`, `REEFER` |
| `pickup_dt` | string | — | `YYYYMMDDHHMMSS` |
| `max_results` | integer | — | caps the result count |

**Response `200`**

| Field | Type | Notes |
|---|---|---|
| `count` | integer | number of loads returned |
| `loads` | array | list of load **summaries** (fields below) |

Each load summary:

| Field | Type | Notes |
|---|---|---|
| `LOAD_ID` | string | e.g. `LD0000045821` |
| `ORIG_CITY` / `ORIG_STATE` / `ORIG_ZIP` | string | origin |
| `DEST_CITY` / `DEST_STATE` / `DEST_ZIP` | string | destination |
| `PICKUP_DT` | string (ISO 8601) | pickup datetime |
| `EQTYPE` | string | equipment type |
| `RATE` | integer | posted loadboard rate, whole dollars |
| `MILES` | integer | distance |
| `STATUS` | string | e.g. `OPEN` |

`MAX_BUY` never appears in search results (the TMS doesn't return it on queries).

**Errors:** `400 missing_field` (no filter), `401 unauthorized`, `502` (TMS error), `503 tms_unavailable`.

---

## POST /tools/get_load  (read)

Full record for one load. `MAX_BUY` is stripped before it leaves the server.

**Request body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `load_id` | string | yes | e.g. `LD0000045821` |

**Response `200`** — `{"load":{…}}` where the load carries the summary fields **plus**:

| Field | Type | Notes |
|---|---|---|
| `DELIVERY_DT` | string (ISO 8601) | delivery datetime |
| `WEIGHT` | integer | lbs |
| `COMMODITY` | string | free text |
| `PIECES` | integer | count |
| `DIMS` | string | free text |
| `NOTES` | string | free text (may be empty) |
| `MAX_BUY` | — | **never present** (stripped) |

Field presence and order can vary by load/server build; parse by key.

**Errors:** `400 missing_field`, `401 unauthorized`, `404 UNKNOWN_LOAD`, `502`, `503 tms_unavailable`.

---

## POST /tools/evaluate_offer  (read)

The negotiation brain. Reads the load's hidden ceiling server-side and returns only the next move. `MAX_BUY` is never returned.

**Request body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `load_id` | string | yes | the load being negotiated |
| `round` | integer | — | `0` (or omit `carrier_offer`) = opening pitch; `1`–`3` = counter rounds |
| `carrier_offer` | integer | — | the carrier's number this round; omit for the opening |

**Response `200`**

| Field | Type | Notes |
|---|---|---|
| `action` | string | `offer` (opening), `accept`, `counter`, or `reject` |
| `rate` | integer or null | the number to say; `null` when `action` is `reject` |

Behaviour: opening ≈ 90% of the ceiling; counters step upward but stay strictly below the ceiling; round 3 accepts at/under the ceiling or rejects. Never returns a rate above the ceiling.

**Errors:** `400 missing_field` (no `load_id`, or non-numeric `carrier_offer`), `401 unauthorized`, `404 UNKNOWN_LOAD`, `409 no_ceiling` (token not flagged for a ceiling), `503 tms_unavailable`.

---

## POST /tools/book_load  (write)

Commits a booking on the TMS. Irreversible. Uses never-blind-retry + confirm-on-ambiguity logic internally.

**Request body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `load_id` | string | yes | the load to book |
| `mc_number` | string | yes | carrier MC number |
| `agreed_rate` | integer | yes | the agreed rate (should be a number `evaluate_offer` returned) |

**Response `200`**

| Field | Type | Notes |
|---|---|---|
| `status` | string | `"booked"` |
| `booking_ref` | string | opaque TMS reference (do not parse) |
| `note` | string | present only when confirmed after an ambiguous/timed-out book |

**Errors:**

| Status | Body | Meaning |
|---|---|---|
| `400` | `{"error":"missing_field",…}` | a required field is missing |
| `401` | `{"error":"unauthorized"}` | missing/invalid API key |
| `409` | `{"error":"ALREADY_BOOKED",…}` | that load is already booked for your token |
| `409` | `{"error":"INVALID_RATE",…}` | `agreed_rate` outside the TMS's accepted band |
| `503` | `{"status":"uncertain","error":"book_ambiguous",…}` | book timed out and could not be confirmed — needs review |

---

## Common error shape

All errors return JSON `{"error":"<code>","message":"<text>"}` (booking's uncertain case adds `"status":"uncertain"`).

| Status | When |
|---|---|
| `400` | bad/missing request fields |
| `401` | missing or wrong `X-API-Key` |
| `404` | `UNKNOWN_LOAD` |
| `409` | `ALREADY_BOOKED`, `INVALID_RATE`, `no_ceiling` |
| `502` | TMS returned a structured error on a read |
| `503` | TMS unreachable/timed out, or an unconfirmable booking |
