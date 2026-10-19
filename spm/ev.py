"""Expected value, no-vig market probabilities, Kelly sizing and the bet decision."""

DEFAULTS = {
    "ev_threshold": 0.03,   # minimum EV (per $1 risked) to recommend a bet
    "market_blend": 0.35,   # weight given to the no-vig market probability
    "bankroll": 1000.0,
    "kelly_mult": 0.25,     # quarter Kelly
    "max_stake_pct": 0.03,
}


def profit_per_unit(odds):
    """Profit on a $1 winning bet at American odds."""
    return odds / 100 if odds > 0 else 100 / abs(odds)


def implied_prob(odds):
    return 100 / (odds + 100) if odds > 0 else abs(odds) / (abs(odds) + 100)


def no_vig(over_odds, under_odds):
    a, b = implied_prob(over_odds), implied_prob(under_odds)
    return a / (a + b), b / (a + b), a + b - 1


def evaluate(proj, over_odds, under_odds, settings=None, low_confidence=False):
    """proj must carry p_over/p_under/p_push (see model.with_line)."""
    s = {**DEFAULTS, **(settings or {})}
    blend = s["market_blend"]
    m_over, m_under, vig = no_vig(over_odds, under_odds)
    live = 1 - proj["p_push"]
    p_over = (1 - blend) * proj["p_over"] + blend * m_over * live
    p_under = (1 - blend) * proj["p_under"] + blend * m_under * live

    sides = {}
    for side, p_win, p_lose, odds, mkt, raw in (
        ("OVER", p_over, p_under, over_odds, m_over, proj["p_over"]),
        ("UNDER", p_under, p_over, under_odds, m_under, proj["p_under"]),
    ):
        b = profit_per_unit(odds)
        ev = p_win * b - p_lose
        kelly = max(0.0, (b * p_win - p_lose) / b) * s["kelly_mult"]
        kelly = min(kelly, s["max_stake_pct"])
        sides[side] = {
            "odds": odds, "model_prob": raw, "blended_prob": p_win, "market_prob": mkt,
            "edge": p_win - mkt * live, "ev": ev, "ev_per_100": ev * 100,
            "kelly_pct": kelly if ev > 0 else 0.0,
            "stake": round(s["bankroll"] * kelly, 2) if ev > 0 else 0.0,
            "breakeven": implied_prob(odds),
        }

    best = max(sides, key=lambda k: sides[k]["ev"])
    threshold = s["ev_threshold"] * (1.75 if low_confidence else 1.0)
    if sides[best]["ev"] >= threshold:
        decision = best
        tier = "Strong" if sides[best]["ev"] >= max(0.08, 2 * threshold) else "Lean"
    else:
        decision, tier = "PASS", None
    return {"decision": decision, "tier": tier, "best_side": best, "best_ev": sides[best]["ev"],
            "threshold": threshold, "sides": sides, "vig": vig, "push_prob": proj["p_push"],
            "low_confidence": low_confidence}
