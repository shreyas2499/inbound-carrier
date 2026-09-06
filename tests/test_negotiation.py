"""Pure tests for the negotiation policy — anchored on MAX_BUY (the real data has
the ceiling BELOW the posted rate, so 1950 is used as a realistic ceiling)."""
from adapter.negotiation import (LAST_LADDER_ROUND, OFFER_LADDER,
                                 evaluate_offer)

M = 1950   # this load's hidden ceiling (below its 2150 loadboard rate)


def test_opening_offer_is_below_ceiling():
    d = evaluate_offer(M, 0)                 # round 0, no carrier counter
    assert d["action"] == "offer"
    assert d["rate"] < M                     # opens under the ceiling, leaving room


def test_missing_counter_returns_opening_regardless_of_round():
    assert evaluate_offer(M, 2, None)["action"] == "offer"


def test_accept_when_counter_within_our_stepped_offer():
    # round 1 offer ~1774 (91% of 1950); a 1750 counter is within it -> accept at 1750
    d = evaluate_offer(M, 1, 1750)
    assert d["action"] == "accept" and d["rate"] == 1750


def test_lowball_far_below_opening_asks_to_clarify():
    # 69 is far below the 1658 opening (85%) -> re-confirm, never book
    d = evaluate_offer(M, 1, 69)
    assert d["action"] == "clarify" and d["rate"] == 69


def test_number_at_the_opening_is_valid_not_clarified():
    # exactly the opening (round(1950*0.85)=1658) is a legit number, accepted
    d = evaluate_offer(M, 1, 1658)
    assert d["action"] == "accept" and d["rate"] == 1658


def test_counter_stays_strictly_below_ceiling():
    d = evaluate_offer(M, 1, 1900)
    assert d["action"] == "counter"
    assert d["rate"] < M


def test_round3_offers_the_97pct_rung():
    # round 3 now COUNTERS at 97% (1892); it no longer accepts/rejects here
    d = evaluate_offer(M, 3, 1900)
    assert d["action"] == "counter" and d["rate"] == 1892


def test_accept_at_ceiling_after_final_offer():
    # the carrier's response to the 97% offer is round 4: accept up to the ceiling
    d = evaluate_offer(M, 4, 1950)
    assert d["action"] == "accept" and d["rate"] == 1950


def test_reject_after_final_offer_above_ceiling():
    # above the ceiling -> reject, but hand back our 97% final offer (1892), never
    # the carrier's over-ceiling number
    d = evaluate_offer(M, 4, 2100)
    assert d["action"] == "reject" and d["rate"] == 1892


def test_stretches_accept_up_to_ceiling_after_final_offer():
    # round 4 (response to the 97% offer): 1920 is above 97% but <= ceiling -> accept
    d = evaluate_offer(M, 4, 1920)
    assert d["action"] == "accept" and d["rate"] == 1920


def test_never_offers_or_accepts_above_ceiling_across_a_sweep():
    for rnd in (0, 1, 2, 3, 4):
        for counter in range(1500, M + 400, 25):
            d = evaluate_offer(M, rnd, counter)
            if d["rate"] is not None:
                assert d["rate"] <= M        # invariant: never above the ceiling


# --- the ratchet: a carrier who keeps RAISING must not walk to the ceiling ------

def test_ceiling_stretch_is_offered_once_not_every_round():
    """Round 4 is the carrier's answer to our final 97% offer, so it stretches to
    the true ceiling. Round 5+ is a re-trade and does not get the stretch again --
    otherwise a caller who keeps raising is accepted at each step all the way up
    (real bug, run 0752609b: 790 -> 820 -> 860 -> 900 against a 901 ceiling)."""
    max_buy = 901
    final_rung = round(max_buy * OFFER_LADDER[LAST_LADDER_ROUND])   # 874

    # round 4: the one stretch. Anything under the ceiling closes.
    assert evaluate_offer(max_buy, 4, 900) == {"action": "accept", "rate": 900}

    # round 5+: held at the final rung, whatever they ask for.
    for rnd in (5, 6, 7, 9):
        assert evaluate_offer(max_buy, rnd, 900) == {
            "action": "reject", "rate": final_rung}
        assert evaluate_offer(max_buy, rnd, 880) == {
            "action": "reject", "rate": final_rung}


def test_a_ratcheting_carrier_is_capped_at_the_final_rung():
    """Replay of run 0752609b's escalation under the fixed policy.

    The cap is the 97% rung, NOT a blanket refusal: we would still happily pay
    860 (under 874). What must never happen again is the climb continuing past it
    -- 900 was accepted live, one dollar under the ceiling."""
    max_buy = 901
    final_rung = round(max_buy * OFFER_LADDER[LAST_LADDER_ROUND])   # 874
    escalation = [(3, 790), (4, 820), (5, 860), (6, 900), (7, 920)]

    for rnd, ask in escalation:
        res = evaluate_offer(max_buy, rnd, ask)
        if res["action"] == "accept":
            # after the single round-4 stretch, nothing above the rung closes
            if rnd > LAST_LADDER_ROUND + 1:
                assert res["rate"] <= final_rung, (rnd, ask, res)
            assert res["rate"] <= max_buy

    # the two the live run got wrong are now held
    assert evaluate_offer(max_buy, 6, 900) == {"action": "reject", "rate": final_rung}
    assert evaluate_offer(max_buy, 7, 920) == {"action": "reject", "rate": final_rung}


def test_below_the_final_rung_still_closes_after_the_stretch():
    """Holding at the rung must not mean refusing money we would happily take."""
    assert evaluate_offer(901, 7, 850) == {"action": "accept", "rate": 850}
