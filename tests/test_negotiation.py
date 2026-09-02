"""Pure tests for the negotiation policy, incl. the ceiling invariants."""
from adapter.negotiation import evaluate_offer

L, M = 2150, 2500   # loadboard rate, hidden ceiling


def test_accept_when_counter_at_or_below_loadboard():
    d = evaluate_offer(2100, 1, L, M)
    assert d["action"] == "accept" and d["rate"] == 2100


def test_counter_steps_toward_ceiling_but_stays_below_it():
    d = evaluate_offer(2400, 1, L, M)          # round 1
    assert d["action"] == "counter"
    assert L < d["rate"] < M                    # strictly below the ceiling


def test_accept_when_counter_within_stepped_offer():
    # round 2 offer ~2383; a 2350 counter is within it -> accept at 2350
    d = evaluate_offer(2350, 2, L, M)
    assert d["action"] == "accept" and d["rate"] == 2350


def test_reject_on_final_round_above_ceiling():
    d = evaluate_offer(2600, 3, L, M)           # above ceiling, last round
    assert d["action"] == "reject"


def test_accept_at_ceiling_on_final_round():
    d = evaluate_offer(2500, 3, L, M)           # exactly the ceiling
    assert d["action"] == "accept" and d["rate"] == 2500


def test_never_offers_above_ceiling_across_a_sweep():
    for rnd in (1, 2, 3):
        for counter in range(L, M + 400, 25):
            d = evaluate_offer(counter, rnd, L, M)
            if d["rate"] is not None:
                assert d["rate"] <= M           # invariant: never above the ceiling
