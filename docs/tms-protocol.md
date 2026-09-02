# Legacy TMS — protocol notes

Distilled from the HappyRobot Legacy TMS Protocol Reference (doc HR-LTMS-PR-001).
The wire is authoritative; where the manual is silent, behavior is
implementation-defined. Host/port/token come from the candidate portal.

## Transport & framing
- TCP, ASCII, lines terminated `\r\n`, **4096-byte** max frame, **30s** idle
  timeout (server closes), **one request per connection** (no reuse).
- Request: `CMD:<cmd>|AUTH:<token>|<FIELD>:<VALUE>|...\r\n` — `CMD` first, `AUTH`
  on every request; values may not contain `|` or CR/LF; unknown fields discarded.
- Success: zero or more record lines, then `END\r\n`.
- Error: `ERR|CODE:<code>|MSG:<msg>\r\n`.
- Records are `|`-delimited `KEY:VALUE`; fixed-width, space-padded right.
  **Field order is not stable across builds → parse by key, not position.**

## Encodings
- `RATE`, `MAX_BUY`, `WEIGHT`, `PIECES`, `MILES` — zero-padded integers
  (`0002150` → 2150).
- `PICKUP_DT`, `DELIVERY_DT`, `TIMESTAMP` — `YYYYMMDDHHMMSS` (TIMESTAMP is UTC).
- `COMMODITY`, `DIMS`, `NOTES` — free text, no controlled vocabulary; blank
  `NOTES` is space padding that collapses to `""` when trimmed.
- `LOAD_ID` = `LD` + 10 digits. `BOOKING_REF` is opaque — never parse it.

## Commands
- **DEBUG_ECHO** — `MSG` echoed; success line leads with a bare `ECHO` marker:
  `ECHO|AUTH:OK|FIELDS_PARSED:n|MSG:...`. `FIELDS_PARSED` counts accepted
  KEY:VALUE pairs (incl. CMD+AUTH) — use it to validate the encoder.
  **Bypasses fault injection** — proves auth/framing only, never health.
- **LOAD_QUERY** — ≥1 filter required (`ORIG_/DEST_ CITY|STATE|ZIP`, `EQTYPE`,
  pickup, `MAX_RESULTS`). City match forgiving; state/ZIP strict. Returns summary
  rows (LOAD_ID, ORIG/DEST, PICKUP_DT, EQTYPE, RATE, MILES, STATUS). No MAX_BUY.
- **LOAD_GET** — by `LOAD_ID`; one full record + END. Adds DELIVERY_DT, WEIGHT,
  COMMODITY, PIECES, DIMS, NOTES, STATUS, and **MAX_BUY**. Resolves regardless
  of STATUS (booked / past-pickup still return).
- **LOAD_BOOK** — `LOAD_ID` + `MC_NUM` + `AGREED_RATE` → BOOKING_REF, STATUS,
  TIMESTAMP.

## Gotchas that are graded
- **MAX_BUY** (the hidden ceiling) appears **only on flagged tokens**, **only on
  LOAD_GET**, and is **absent** otherwise. Fetch it in the adapter, keep it
  server-side, never return it toward the agent. Detection is by presence.
- **Booking idempotency**: a token's view of a load is **monotonic** — once
  `ALREADY_BOOKED`, always `ALREADY_BOOKED` for that token. A timed-out
  `LOAD_BOOK` may have **succeeded** server-side → never blind-retry; confirm with
  `LOAD_GET` (`STATUS == BOOKED`) before retrying.
- **INVALID_RATE** covers more than rate 0. Acceptable `AGREED_RATE` range is not
  advertised — bound it from observation (>0 and ≤ ceiling).
- Error codes seen: AUTH_FAILED, UNKNOWN_CMD, MISSING_FIELD, UNKNOWN_LOAD,
  ALREADY_BOOKED, INVALID_RATE, MALFORMED, SERVER_ERROR — **not exhaustive**.

## Fault injection (operational commands only; unsignalled)
- **Timeout** — connection accepted, request read, no response; idle-closed.
- **Partial** — a prefix of a valid response, connection closed with no `END`.
- **Malformed** — framing violations: extra delimiters, unterminated lines,
  over-width values.
- **Delayed termination** — complete response, then connection held open.
- Not signalled — detect from the wire. Client contract: require `END`, validate
  framing, timeout well under 30s, retry on a fresh connection, bound retries.
