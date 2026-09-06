"""Negotiation policy — the ceiling logic that never leaves the server.

Anchored entirely on each load's own MAX_BUY (the hard ceiling), which the agent
never sees. The agent OPENS below the ceiling and concedes upward along a fixed,
diminishing-step ladder, offering every rung in turn as long as the carrier keeps
countering:

    round 0 (opening)   85%     of MAX_BUY
    round 1 counter     91%
    round 2 counter     94.75%
    round 3 counter     97%     (the final rung we proactively OFFER)

Rounds 1-3 always counter along the ladder (accepting early only if the carrier
already meets that rung). Only AFTER the 97% offer -- the carrier's response to
it, round 4+ -- does acceptance stretch to the true ceiling: any number at or
under MAX_BUY is accepted; a demand above MAX_BUY is rejected, and the policy
hands back our 97% offer as the final take-it-or-leave-it number.

Sanity floor: a "counter" below our own opening offer (85%) is not a real bid --
no carrier hauls below the opening -- so it is almost always a mis-heard figure.
It is NEVER booked; the policy returns action="clarify" so the agent re-confirms
the number before acting.

MAX_BUY drives every decision, is never returned to the agent, and the loadboard
RATE is ignored entirely.
"""
from __future__ import annotations

# Offer ladder as a fraction of MAX_BUY, by round. Diminishing steps; 0.97 (round 3)
# is the last rung we proactively offer. Acceptance stretches to 1.0 only on the
# carrier's response to that final offer (round 4+).
OFFER_LADDER = {0: 0.85, 1: 0.91, 2: 0.9475, 3: 0.97}
LAST_LADDER_ROUND = 3


def evaluate_offer(max_buy, round_number, carrier_counter=None) -> dict:
    """Return the agent's next move.

    round 0 / no carrier_counter -> {'action': 'offer', 'rate': opening}
    a carrier counter -> {'action': 'accept'|'counter'|'reject'|'clarify', 'rate': int|None}
    """
    max_buy = int(max_buy)
    r = int(round_number)
    opening = round(max_buy * OFFER_LADDER[0])

    # Opening: no counter yet (or round 0).
    if carrier_counter is None or r <= 0:
        return {"action": "offer", "rate": opening}

    r = max(1, r)
    carrier_counter = int(carrier_counter)

    # Sanity floor: a number below our own opening offer is not a real counter
    # (a carrier never bids below the opening) -- almost always a mis-heard figure.
    # Never book it; ask the agent to re-confirm the number.
    if carrier_counter < opening:
        return {"action": "clarify", "rate": carrier_counter}

    # Rounds 1-3: proactively concede one rung up the ladder (91% -> 94.75% -> 97%),
    # accepting straight away only if the carrier already meets that rung.
    if r <= LAST_LADDER_ROUND:
        our_offer = round(max_buy * OFFER_LADDER[r])
        if carrier_counter <= our_offer:
            return {"action": "accept", "rate": carrier_counter}
        return {"action": "counter", "rate": our_offer}

    final_rung = round(max_buy * OFFER_LADDER[LAST_LADDER_ROUND])

    # Round 4 -- and ONLY round 4 -- is the carrier's response to our final 97%
    # offer, so acceptance stretches to the true ceiling to save the load.
    if r == LAST_LADDER_ROUND + 1 and carrier_counter <= max_buy:
        return {"action": "accept", "rate": carrier_counter}

    # Round 5+ is a RE-TRADE, and the stretch is not on offer twice. A carrier who
    # keeps raising after we have already come up to meet them is not negotiating,
    # they are ratcheting -- and because each call is stateless, "is this under the
    # ceiling?" would happily say yes to 790, then 820, then 860, then 900 in a row
    # (real bug, run 0752609b: six accepts climbing to $900 against a $901 ceiling).
    # Past the stretch we hold at our final rung: take anything at or below it,
    # refuse anything above it, and keep quoting the same number.
    if carrier_counter <= final_rung:
        return {"action": "accept", "rate": carrier_counter}
    return {"action": "reject", "rate": final_rung}
