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
    # round 1 offer ~1820; a 1800 counter is within it -> accept at 1800
    d = evaluate_offer(M, 1, 1800)
    assert d["action"] == "accept" and d["rate"] == 1800


def test_counter_stays_strictly_below_ceiling():
    d = evaluate_offer(M, 1, 1900)
    assert d["action"] == "counter"
    assert d["rate"] < M


def test_accept_at_ceiling_on_final_round():
    d = evaluate_offer(M, 3, 1950)
    assert d["action"] == "accept" and d["rate"] == 1950


def test_reject_on_final_round_above_ceiling():
    d = evaluate_offer(M, 3, 2100)
    assert d["action"] == "reject"


def test_never_offers_or_accepts_above_ceiling_across_a_sweep():
    for rnd in (0, 1, 2, 3):
        for counter in range(1500, M + 400, 25):
            d = evaluate_offer(M, rnd, counter)
            if d["rate"] is not None:
                assert d["rate"] <= M        # invariant: never above the ceiling
