"""Negotiation policy — the ceiling logic that never leaves the server.

Anchored entirely on each load's own MAX_BUY (the hard ceiling), which the agent
never sees. The agent OPENS below the ceiling and concedes upward along a fixed,
diminishing-step ladder over at most three rounds:

    round 0 (opening)   85%     of MAX_BUY
    round 1 counter     91%
    round 2 counter     94.75%
    round 3 counter     97%     (the most we PROACTIVELY offer)

The ladder is capped at 97% so we never volunteer the full ceiling. But the true
walk-away is MAX_BUY itself: on the final round we accept any carrier number at or
under MAX_BUY rather than lose a load we can profitably cover — only a demand ABOVE
MAX_BUY is rejected. Earlier rounds concede one rung at a time instead of jumping to
the ceiling. Accept always takes the carrier's own number, never more. MAX_BUY drives
every decision, is never returned to the agent, and the loadboard RATE is ignored.
"""
from __future__ import annotations

# Offer ladder as a fraction of MAX_BUY, by round. Diminishing steps; 0.97 (round 3)
# is the most we proactively offer — acceptance can still stretch to 1.0 on the final
# round to save a bookable load.
OFFER_LADDER = {0: 0.85, 1: 0.91, 2: 0.9475, 3: 0.97}
MAX_ROUND = 3


def evaluate_offer(max_buy, round_number, carrier_counter=None) -> dict:
    """Return the agent's next move.

    round 0 / no carrier_counter -> {'action': 'offer',  'rate': opening}
    a carrier counter            -> {'action': 'accept'|'counter'|'reject', 'rate': int|None}
    """
    max_buy = int(max_buy)
    r = int(round_number)

    # Opening: no counter yet (or round 0).
    if carrier_counter is None or r <= 0:
        return {"action": "offer", "rate": round(max_buy * OFFER_LADDER[0])}

    r = min(max(r, 1), MAX_ROUND)
    carrier_counter = int(carrier_counter)
    our_offer = round(max_buy * OFFER_LADDER[r])

    # They met or beat this round's offer -> take their (lower) number.
    if carrier_counter <= our_offer:
        return {"action": "accept", "rate": carrier_counter}

    # Final round: stretch acceptance up to the true ceiling to save the load;
    # walk only if they are asking for more than we can pay.
    if r >= MAX_ROUND:
        if carrier_counter <= max_buy:
            return {"action": "accept", "rate": carrier_counter}
        return {"action": "reject", "rate": None}

    # Earlier rounds: concede one rung up the ladder.
    return {"action": "counter", "rate": our_offer}
