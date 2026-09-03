# Build notes — decisions & deferred items

## Decisions made
- **One external service only.** The TMS adapter (this repo) is the single
  justified external service; everything else is HappyRobot-native. The platform
  speaks HTTP, the TMS speaks raw TCP, so the adapter is unavoidable.
- **Negotiation anchors on each load's own MAX_BUY, not the loadboard rate.**
  Real data shows MAX_BUY sits ~10% BELOW the posted rate, so anchoring on the
  loadboard rate would overpay. The opening offer is computed server-side too,
  because the agent must never see MAX_BUY.
- **Per-call load cache** (90s TTL) so a single negotiation's rounds read the
  ceiling once instead of hitting the flaky TMS each round. Booking bypasses it.

## Deferred to the HappyRobot workflow (Phase 4)
- **Load scarcity.** The live board is sparse/rotating — a narrow lane often
  returns zero matches. The agent must handle "no matching loads" gracefully:
  broaden the lane (drop to a single filter / adjacent states), offer a callback,
  and close politely rather than dead-ending. Do NOT invent loads.
- **FMCSA authority check** stays a native HappyRobot tool for now. Option on the
  table: proxy it through the adapter to keep the FMCSA key server-side — decide
  in Phase 4.
- **Opening pitch vs. posted rate.** Agent opens ~10% under the ceiling, which is
  below the posted loadboard rate; make sure the script frames that naturally so
  the carrier isn't startled by the drop from the posted number.

## OTP — contact-of-record source (added, still parked)
- FMCSA is an AUTHORITY source, not a CONTACT source. The `telephone` field is
  frequently `null` (confirmed live: OUZA TRANSPORTATION INC / MC 872144 / DOT
  2514144 returned phone=null while eligible=true). So the FMCSA phone CANNOT be
  the sole OTP destination.
- In a real brokerage the OTP contact comes from the broker's OWN carrier
  onboarding records (already-verified phone/email), not from FMCSA. The sandbox
  TMS does not expose carrier contact info.
- Decision to make when OTP is unparked: where does the contact-of-record come
  from? FMCSA phone is a "use-if-present" fallback only, never the only source.
- Richer FMCSA data (by DOT number) is available if useful later:
  /carriers/{dot}/authority, /basics, /cargo-carried, /operation-classification,
  /oos, /docket-numbers.
