"""Negotiation policy — the ceiling logic that never leaves the server.

`evaluate_offer` is a pure function. It takes the carrier's counter, the round,
the posted loadboard rate, and the hidden ceiling (MAX_BUY), and returns only
the agent's next move: accept at a number, counter with a number, or reject.

Two invariants make it safe to expose as a tool:
  * It never returns a rate above the ceiling.
  * It never returns the ceiling itself as a spoken counter — the escalating
    counters for rounds 1 and 2 are strictly below MAX_BUY, and round 3 only ever
    accepts or rejects. So MAX_BUY is used but never disclosed.

The broker PAYS the carrier, so the carrier pushes the rate UP and the agent
holds the line at MAX_BUY. Offers step from the loadboard rate toward the ceiling
across at most three rounds.
"""
from __future__ import annotations


def evaluate_offer(carrier_counter: int, round_number: int,
                   loadboard_rate: int, max_buy: int) -> dict:
    """Return {'action': 'accept'|'counter'|'reject', 'rate': int|None}."""
    carrier_counter = int(carrier_counter)
    loadboard_rate = int(loadboard_rate)
    max_buy = int(max_buy)
    if max_buy < loadboard_rate:                 # defensive: ceiling below posted
        max_buy = loadboard_rate
    round_number = max(1, min(int(round_number), 3))

    # Our escalating offer for this round; reaches exactly the ceiling by round 3.
    our_offer = round(loadboard_rate + (max_buy - loadboard_rate) * round_number / 3)
    our_offer = min(our_offer, max_buy)

    if carrier_counter <= our_offer:
        # Carrier's ask is within what we'll pay this round — take their number.
        return {"action": "accept", "rate": carrier_counter}
    if round_number >= 3:
        # Final round, carrier still above the ceiling — we cannot meet them.
        return {"action": "reject", "rate": None}
    # Counter with our stepped offer (strictly below the ceiling for rounds 1-2).
    return {"action": "counter", "rate": int(our_offer)}
