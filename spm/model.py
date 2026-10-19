"""Projection model.

For one player and one stat it estimates a full outcome distribution:

  mean = per-minute rate x projected minutes x development x matchup x venue

* Rate: recency-weighted (exponential decay by game) per-minute production,
  blended 70/30 between a short and long memory, then shrunk toward the league
  rate in proportion to how many minutes back it up.
* Minutes: blend of recent (half-life 10 games) and longer-run (40 games) role,
  trimmed on the second night of a back-to-back.
* Development: experience-based aging curve, applied only to the share of the
  estimate that comes from prior seasons.
* Matchup: how much of the stat the opponent allowed vs league average,
  regressed 50% toward average (defenses change a lot year to year).
* Distribution: negative binomial with the player's own observed dispersion
  (variance / mean) at similar minutes, widened when the role is uncertain.
"""

import math

STATS = {
    "PTS": ("Points", ("pts",)),
    "REB": ("Rebounds", ("reb",)),
    "AST": ("Assists", ("ast",)),
    "3PM": ("3-Pointers Made", ("fg3m",)),
    "STL": ("Steals", ("stl",)),
    "BLK": ("Blocks", ("blk",)),
    "TOV": ("Turnovers", ("tov",)),
    "PRA": ("Pts + Reb + Ast", ("pts", "reb", "ast")),
    "PR": ("Pts + Reb", ("pts", "reb")),
    "PA": ("Pts + Ast", ("pts", "ast")),
    "RA": ("Reb + Ast", ("reb", "ast")),
    "SB": ("Steals + Blocks", ("stl", "blk")),
}

# Pseudo-minutes of league-average production mixed into each rate.
SHRINK_K = {"PTS": 10, "REB": 15, "AST": 15, "3PM": 40, "STL": 80, "BLK": 80, "TOV": 40,
            "PRA": 8, "PR": 10, "PA": 10, "RA": 12, "SB": 60}
# Ceiling on variance/mean so a few blowouts can't explode the spread.
DISP_CAP = {"PTS": 4.5, "REB": 2.5, "AST": 2.5, "3PM": 2.0, "STL": 1.6, "BLK": 2.0, "TOV": 1.6,
            "PRA": 5.5, "PR": 5.0, "PA": 5.0, "RA": 3.0, "SB": 2.0}

MIN_GAMES = 8
MAX_GAMES = 220
B2B_MINUTES = 0.965
HOME_EDGE = 0.01
OPP_SHRINK = 0.5


def dev_adjust(experience):
    """Expected year-over-year change in per-minute production by NBA experience."""
    if experience is None:
        return 0.0
    return {1: 0.06, 2: 0.04, 3: 0.025, 4: 0.01}.get(experience, 0.0 if experience < 10
                                                    else -0.015 if experience < 12 else -0.035)


def stat_value(g, fields):
    return sum(getattr(g, f) for f in fields)


def _weights(n, half_life):
    return [0.5 ** (i / half_life) for i in range(n)]


def _wmean(vals, w):
    s = sum(w)
    return sum(v * x for v, x in zip(vals, w)) / s if s else 0.0


class History:
    """A player's games before a date, newest first, with shared decay weights."""

    def __init__(self, player, asof=None, target=None):
        games = [g for g in player.games_before(asof) if g.min >= 1][-MAX_GAMES:]
        self.player = player
        self.rev = games[::-1]
        self.n = len(games)
        self.mins = [g.min for g in self.rev]
        self.w10, self.w25 = _weights(self.n, 10), _weights(self.n, 25)
        self.w40, self.w100 = _weights(self.n, 40), _weights(self.n, 100)
        if self.n:
            m_recent, m_long = _wmean(self.mins, self.w10), _wmean(self.mins, self.w40)
            # Before a player has logged games this season, lean on the full-season role:
            # end-of-season minutes are distorted by rest days, tanking and blowouts.
            cur = sum(1 for g in self.rev[:10] if target is None or g.season >= target)
            w_recent = 0.3 + 0.3 * min(cur, 10) / 10
            self.min_base = w_recent * m_recent + (1 - w_recent) * m_long
            self.m_recent, self.m_long = m_recent, m_long
        else:
            self.min_base = self.m_recent = self.m_long = 0.0


# --------------------------------------------------------------------------- distribution

def _pmf_fn(mu, var):
    mu = max(mu, 1e-6)
    if var <= mu * 1.0001:
        lm = math.log(mu)
        return lambda k: math.exp(k * lm - mu - math.lgamma(k + 1))
    r = mu * mu / (var - mu)
    p = r / (r + mu)
    c = r * math.log(p) - math.lgamma(r)
    lq = math.log(1 - p)
    return lambda k: math.exp(c + math.lgamma(k + r) - math.lgamma(k + 1) + k * lq)


def probabilities(mu, var, line):
    """P(over), P(under), P(push) for a count stat against a line."""
    pmf = _pmf_fn(mu, var)
    floor = math.floor(line)
    cdf = sum(pmf(k) for k in range(int(floor) + 1))
    if float(line).is_integer():
        push = pmf(int(line))
        return max(0.0, 1 - cdf), cdf - push, push
    return max(0.0, 1 - cdf), cdf, 0.0


def quantile(mu, var, q):
    pmf = _pmf_fn(mu, var)
    acc, k = 0.0, 0
    while k < 400:
        acc += pmf(k)
        if acc >= q:
            return k
        k += 1
    return k


# --------------------------------------------------------------------------- projection

def project(ds, hist, stat, opponent=None, home=None, b2b=False, asof=None, live=True):
    """Return a projection dict for one stat, or None when history is too thin."""
    if hist.n < MIN_GAMES:
        return None
    label, fields = STATS[stat]
    player = hist.player
    target = ds.current_start if asof is None else _season_of(asof)
    vals = [stat_value(g, fields) for g in hist.rev]
    mins = hist.mins

    # Minutes
    min_proj = hist.min_base * (B2B_MINUTES if b2b else 1.0)

    # Per-minute rate
    def rate(w):
        den = sum(wi * m for wi, m in zip(w, mins))
        return sum(wi * v for wi, v in zip(w, vals)) / den if den else 0.0

    r_recent, r_long = rate(hist.w25), rate(hist.w100)
    rate_raw = 0.7 * r_recent + 0.3 * r_long
    eff_min = sum(wi * m for wi, m in zip(hist.w25, mins))
    league_rate = ds.league_rate_for(fields, target)
    k = SHRINK_K[stat]
    rate_adj = (rate_raw * eff_min + k * league_rate) / (eff_min + k)

    # Development / aging, weighted by how much of the estimate is last-season data
    total_w = sum(hist.w25)
    prior_share = sum(w for w, g in zip(hist.w25, hist.rev) if g.season < target) / total_w
    experience = target - player.from_year if player.from_year else None
    dev_pct = dev_adjust(experience) * prior_share
    dev = 1 + dev_pct

    # Matchup
    opp_info = ds.opp_factor(opponent, fields, target, include_current=live) if opponent else None
    opp_f = 1 + OPP_SHRINK * (opp_info["ratio"] - 1) if opp_info else 1.0

    # Venue: league home edge plus 20% of the player's own home/away split
    home_f, split = 1.0, None
    if home is not None:
        def split_rate(want_home):
            num = den = 0.0
            for w, g, v in zip(hist.w100, hist.rev, vals):
                if g.home == want_home:
                    num += w * v
                    den += w * g.min
            return num / den if den else None
        hr, ar = split_rate(True), split_rate(False)
        home_games = sum(1 for g in hist.rev if g.home)
        if hr and ar and home_games >= 8 and hist.n - home_games >= 8:
            split = hr / ar
        personal = math.sqrt(split) if split else 1.0
        personal = personal if home else 1 / personal
        home_f = (1 + HOME_EDGE if home else 1 - HOME_EDGE) * (1 + 0.2 * (personal - 1))
        home_f = min(1.05, max(0.95, home_f))

    mean = rate_adj * min_proj * dev * opp_f * home_f

    # Dispersion from games at a similar minutes load
    lo, hi = 0.7 * hist.min_base, 1.3 * hist.min_base
    sel = [(v, w) for v, m, w in zip(vals, mins, hist.w40) if lo <= m <= hi]
    if len(sel) < 12:
        sel = [(v, w) for v, m, w in zip(vals, mins, hist.w40) if m >= 0.5 * hist.min_base]
    if len(sel) < 5:
        sel = list(zip(vals, hist.w40))
    sw = sum(w for _, w in sel)
    mu_h = sum(v * w for v, w in sel) / sw
    var_h = sum(w * (v - mu_h) ** 2 for v, w in sel) / sw
    disp = min(DISP_CAP[stat], max(1.0, var_h / mu_h if mu_h > 0 else 1.0))

    team_change = live and player.changed_team
    infl = 1.0
    if team_change:
        infl *= 1.2
    if prior_share > 0.8:
        infl *= 1.08
    if hist.n < 30:
        infl *= 1.1
    var = max(mean * disp * infl, mean + 1e-6)

    return {
        "stat": stat, "label": label, "mean": mean, "var": var, "sd": math.sqrt(var),
        "minutes": min_proj, "n_games": hist.n,
        "rate_recent": r_recent, "rate_long": r_long, "rate": rate_adj, "league_rate": league_rate,
        "experience": experience, "dev_pct": dev_pct, "prior_share": prior_share,
        "opp": opp_info, "opp_factor": opp_f, "opponent": opponent,
        "home": home, "home_factor": home_f, "home_split": split, "b2b": b2b,
        "team_change": team_change, "dispersion": disp, "inflation": infl,
        "target_season": target,
    }


def with_line(proj, line):
    p_over, p_under, p_push = probabilities(proj["mean"], proj["var"], line)
    return {**proj, "line": line, "p_over": p_over, "p_under": p_under, "p_push": p_push}


def interval(proj, lo=0.1, hi=0.9):
    return quantile(proj["mean"], proj["var"], lo), quantile(proj["mean"], proj["var"], hi)


def _season_of(date):
    y, m = int(date[:4]), int(date[5:7])
    return y if m >= 8 else y - 1
