"""EV math (spm/ev.py): odds conversion, vig removal, EV, Kelly sizing, decisions.

Expected values are worked out by hand in the comments so each test documents the formula.
"""

import pytest

from spm import ev


@pytest.mark.parametrize("odds, profit", [
    (100, 1.0),          # even money: win $1 per $1
    (150, 1.5),          # +150: win $1.50 per $1
    (-110, 100 / 110),   # -110: risk $1.10 to win $1 -> 0.909 per $1
    (-200, 0.5),
])
def test_profit_per_unit(odds, profit):
    assert ev.profit_per_unit(odds) == pytest.approx(profit)


@pytest.mark.parametrize("odds, prob", [
    (100, 0.5),
    (-110, 110 / 210),   # 0.5238
    (150, 100 / 250),    # 0.40
    (-200, 200 / 300),   # 0.6667
])
def test_implied_prob(odds, prob):
    assert ev.implied_prob(odds) == pytest.approx(prob)


def test_no_vig_symmetric_line():
    over, under, vig = ev.no_vig(-110, -110)
    assert over == pytest.approx(0.5)
    assert under == pytest.approx(0.5)
    assert vig == pytest.approx(2 * 110 / 210 - 1)  # ~4.76% overround


def test_no_vig_probabilities_sum_to_one():
    over, under, _ = ev.no_vig(-140, 115)
    assert over + under == pytest.approx(1.0)
    assert over > under


def proj(p_over, p_under, p_push=0.0):
    return {"p_over": p_over, "p_under": p_under, "p_push": p_push}


def test_ev_without_market_blend():
    # 60% to win at -110: EV = 0.6 * (100/110) - 0.4 = 0.14545...
    r = ev.evaluate(proj(0.6, 0.4), -110, -110, {"market_blend": 0.0})
    assert r["sides"]["OVER"]["ev"] == pytest.approx(0.6 * 100 / 110 - 0.4)
    assert r["sides"]["UNDER"]["ev"] == pytest.approx(0.4 * 100 / 110 - 0.6)
    assert r["decision"] == "OVER"
    assert r["tier"] == "Strong"  # EV >= 8%


def test_market_blend_pulls_toward_no_vig_price():
    # 50% blend of model 60% with a 50/50 market -> 55% win chance
    r = ev.evaluate(proj(0.6, 0.4), -110, -110, {"market_blend": 0.5})
    assert r["sides"]["OVER"]["blended_prob"] == pytest.approx(0.55)


def test_push_returns_stake():
    # Whole-number line: 10% push. Pushes neither win nor lose, so EV = p_win*b - p_lose.
    r = ev.evaluate(proj(0.5, 0.4, 0.1), 100, 100, {"market_blend": 0.0})
    assert r["sides"]["OVER"]["ev"] == pytest.approx(0.5 * 1.0 - 0.4)
    assert r["push_prob"] == pytest.approx(0.1)


def test_pass_below_threshold():
    # 53% at -110: EV = 0.53*0.909 - 0.47 = +1.2%, below the 3% bar
    r = ev.evaluate(proj(0.53, 0.47), -110, -110, {"market_blend": 0.0})
    assert r["decision"] == "PASS"
    assert r["tier"] is None


def test_low_confidence_raises_the_bar():
    # 56% at -110: EV = +6.9%. Passes the normal 3% bar and 1.75x = 5.25% bar...
    r = ev.evaluate(proj(0.56, 0.44), -110, -110, {"market_blend": 0.0}, low_confidence=True)
    assert r["threshold"] == pytest.approx(0.03 * 1.75)
    assert r["decision"] == "OVER"
    # ...but 55% (EV +5.0%) only clears the normal bar.
    r = ev.evaluate(proj(0.55, 0.45), -110, -110, {"market_blend": 0.0}, low_confidence=True)
    assert r["decision"] == "PASS"


def test_kelly_is_fractional_and_capped():
    # Full Kelly at 60%/-110: (b*p - q)/b = (0.909*0.6 - 0.4)/0.909 = 0.16
    # Quarter Kelly = 0.04, capped at 3% of bankroll -> $30 on $1,000
    r = ev.evaluate(proj(0.6, 0.4), -110, -110, {"market_blend": 0.0, "bankroll": 1000})
    assert r["sides"]["OVER"]["kelly_pct"] == pytest.approx(0.03)
    assert r["sides"]["OVER"]["stake"] == pytest.approx(30.0)


def test_no_stake_on_negative_ev_side():
    r = ev.evaluate(proj(0.6, 0.4), -110, -110, {"market_blend": 0.0})
    assert r["sides"]["UNDER"]["stake"] == 0.0
    assert r["sides"]["UNDER"]["kelly_pct"] == 0.0
