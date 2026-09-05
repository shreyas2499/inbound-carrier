"""Pure tests for the negotiation policy — anchored on MAX_BUY (the real data has
the ceiling BELOW the posted rate, so 1950 is used as a realistic ceiling)."""
from adapter.negotiation import evaluate_offer

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
