"""Negotiation policy — the ceiling logic that never leaves the server.

Anchored entirely on each load's own MAX_BUY (the hard ceiling), which the real
TMS data shows sits BELOW the posted loadboard rate. The agent opens below the
ceiling and concedes upward toward it across at most three rounds, never crossing
it and never speaking it:

  * opening offer (round 0)   = MAX_BUY * open_fraction   (below the ceiling)
  * round 1-2 counters        strictly below the ceiling
  * round 3                   accept at/under the ceiling, otherwise reject

So MAX_BUY drives every decision but is never returned to the agent. The loadboard
rate is deliberately NOT used as the anchor — anchoring on it would overpay,
because the ceiling is below it.
"""
from __future__ import annotations

DEFAULT_OPEN_FRACTION = 0.90


def evaluate_offer(max_buy, round_number, carrier_counter=None, *,
                   open_fraction: float = DEFAULT_OPEN_FRACTION) -> dict:
    """Return the agent's next move.

    round 0 / no carrier_counter -> {'action': 'offer',  'rate': opening}
    a carrier counter            -> {'action': 'accept'|'counter'|'reject', 'rate': int|None}
    """
    max_buy = int(max_buy)
    opening = round(max_buy * open_fraction)

    if carrier_counter is None or int(round_number) <= 0:
        return {"action": "offer", "rate": opening}

    round_number = max(1, min(int(round_number), 3))
    carrier_counter = int(carrier_counter)

    # Escalating offer that reaches exactly the ceiling by the final round.
    our_offer = round(opening + (max_buy - opening) * round_number / 3)
    our_offer = min(our_offer, max_buy)

    if carrier_counter <= our_offer:
        return {"action": "accept", "rate": carrier_counter}
    if round_number >= 3:
        return {"action": "reject", "rate": None}
    return {"action": "counter", "rate": int(our_offer)}
