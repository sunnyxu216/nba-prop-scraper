"""Turns projection factors into plain-English reasoning.

Each point carries a lean: "over" (supports the over), "under", or "neutral".
"""

from .data import season_label
from .model import stat_value, STATS


def _fmt(x, d=1):
    return f"{x:.{d}f}"


def _ordinal(n):
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def player_context(hist, stat, line=None):
    """Recent and last-season numbers for a stat, newest first."""
    fields = STATS[stat][1]
    games = hist.rev
    if not games:
        return None
    last_season = games[0].season
    season_games = [g for g in games if g.season == last_season]
    last10 = games[:10]

    def avg(gs, f=None):
        return sum((f(g) for g in gs)) / len(gs) if gs else None

    ctx = {
        "season": season_label(last_season),
        "season_n": len(season_games),
        "season_avg": avg(season_games, lambda g: stat_value(g, fields)),
        "season_min": avg(season_games, lambda g: g.min),
        "last10_avg": avg(last10, lambda g: stat_value(g, fields)),
        "last10_min": avg(last10, lambda g: g.min),
        "recent": [{"date": g.date, "opp": g.opp, "home": g.home, "min": round(g.min),
                    "value": stat_value(g, fields)} for g in last10][::-1],
    }
    if line is not None:
        ctx["season_hits"] = sum(1 for g in season_games if stat_value(g, fields) > line)
        ctx["last10_hits"] = sum(1 for g in last10 if stat_value(g, fields) > line)
    return ctx


def prop_reasoning(proj, ctx, result, player, interval):
    line, label = proj["line"], proj["label"].lower()
    mean = proj["mean"]
    pts = []

    def add(text, lean="neutral"):
        pts.append({"text": text, "lean": lean})

    def lean_of(value):
        if value is None or abs(value - line) < 0.05 * max(line, 1):
            return "neutral"
        return "over" if value > line else "under"

    ml = proj.get("ml")
    effect = {d["group"]: d["effect"] for d in ml["drivers"]} if ml else {}

    def lean_eff(x):
        return "over" if x > 0.005 else "under" if x < -0.005 else "neutral"

    src = f" (formula model: {_fmt(ml['formula_mean'])})" if ml else ""
    add(f"Projects {_fmt(mean)} {label} in ~{_fmt(proj['minutes'], 0)} minutes "
        f"(80% range {interval[0]}–{interval[1]}) against a line of {line}{src}.", lean_of(mean))

    if ctx and ctx["season_avg"] is not None:
        add(f"{ctx['season']}: averaged {_fmt(ctx['season_avg'])} over {ctx['season_n']} games "
            f"in {_fmt(ctx['season_min'])} mpg, clearing {line} in "
            f"{ctx['season_hits']}/{ctx['season_n']} ({ctx['season_hits'] / ctx['season_n']:.0%}).",
            lean_of(ctx["season_avg"]))
        n10 = len(ctx["recent"])
        trend = ""
        if ctx["season_min"] and abs(ctx["last10_min"] - ctx["season_min"]) >= 2:
            trend = (f"; minutes {'up' if ctx['last10_min'] > ctx['season_min'] else 'down'} to "
                     f"{_fmt(ctx['last10_min'])}")
        add(f"Last {n10} games: {_fmt(ctx['last10_avg'])} per game, over {line} in "
            f"{ctx['last10_hits']}/{n10}{trend}.", lean_of(ctx["last10_avg"]))

    if proj["rate_long"] > 0:
        form = proj["rate_recent"] / proj["rate_long"] - 1
        if abs(form) >= 0.06:
            add(f"Recent per-minute production is {abs(form):.0%} "
                f"{'above' if form > 0 else 'below'} the player's longer-run baseline"
                f"{'' if ml else '; the model weights recent form 70/30'}.", "over" if form > 0 else "under")

    opp = proj.get("opp")
    if opp:
        diff = opp["ratio"] - 1
        adj = proj["opp_factor"] - 1
        if ml:
            e = effect.get("Opponent defense", 0)
            add(f"Matchup: {proj['opponent']} allowed {_fmt(opp['allowed'])} {label}/game "
                f"({_ordinal(opp['rank'])}-most of {opp['teams']}, {diff:+.0%} vs league; {opp['basis']}). "
                f"Model effect {e:+.1%}.", lean_eff(e))
        else:
            add(f"Matchup: {proj['opponent']} allowed {_fmt(opp['allowed'])} {label}/game "
                f"({_ordinal(opp['rank'])}-most of {opp['teams']}, {diff:+.0%} vs league; {opp['basis']}). "
                f"Applied {adj:+.1%} after regressing half way to average.",
                "over" if adj > 0.01 else "under" if adj < -0.01 else "neutral")
    elif proj.get("opponent"):
        add(f"No defensive data for {proj['opponent']}; matchup treated as average.")

    if ml and ml.get("zone"):
        z = ml["zone"]
        e = effect.get("Shot-zone matchup", 0)
        hits = f" (makes {z['player_pct']:.0%})" if z["player_pct"] else ""
        add(f"Shot zones: takes {z['share']:.0%} of shots {z['label']}{hits}. "
            f"{proj['opponent']} allowed {z['opp_pct']:.1%} there vs {z['league_pct']:.1%} league average "
            f"({_ordinal(z['rank'])}-stingiest of {z['teams']}, {z['season']}). Model effect {e:+.1%}.", lean_eff(e))
    if ml and ml.get("playtype"):
        t = ml["playtype"]
        e = effect.get("Play-type matchup", 0)
        mine = f", {t['player_ppp']:.2f} pts/possession" if t["player_ppp"] else ""
        add(f"Play types: most-used is {t['label']} ({t['share']:.0%} of possessions{mine}). "
            f"{proj['opponent']} allowed {t['opp_ppp']:.2f} per possession on it vs {t['league_ppp']:.2f} league "
            f"({_ordinal(t['rank'])}-best of {t['teams']}, {t['season']}). Model effect {e:+.1%}.", lean_eff(e))

    lu = ml.get("lineup") if ml else None
    if lu:
        if lu.get("self_status"):
            s = lu["self_status"]
            add(f"Injury report: {s['status']}{' (ruled out: most books void the bet, so no play)' if s['out'] else ''}. "
                f"{s['comment'][:140]}", "under" if s["out"] else "neutral")
        fmt = lambda xs: ", ".join(f"{x['name']} ({x['status']})" for x in xs)
        if lu["own_missing"]:
            e = effect.get("Teammates out", 0)
            add(f"Teammates out: {fmt(lu['own_missing'])}. They normally produce {lu['own_missing_pts']:.0%} of the "
                f"team's scoring{'; the top scorer is among them' if lu['top_out'] else ''}. Model effect {e:+.1%}.",
                lean_eff(e))
        if lu["opp_missing"]:
            e = effect.get("Opponent lineup", 0)
            add(f"{proj['opponent']} missing: {fmt(lu['opp_missing'])} "
                f"({lu['opp_missing_def']:.2f} defensive win shares per game of value). Model effect {e:+.1%}.",
                lean_eff(e))
        if not lu["own_missing"] and not lu["opp_missing"] and proj.get("opponent"):
            add("Full strength: no rotation players are ruled out for either team on the latest injury report.")
        if lu["questionable"]:
            add("Questionable: " + ", ".join(f"{q['name']} ({q['team']}, {q['status']})" for q in lu["questionable"])
                + ". The projection assumes they play; re-check closer to tip-off.", "neutral")

    if ml:
        rest = [d for d in ml["drivers"] if d["group"] in ("Rest & schedule", "Home/road", "Role & experience")
                and abs(d["effect"]) >= 0.005]
        if rest:
            add("Other situational effects: " + ", ".join(f"{d['group'].lower()} {d['effect']:+.1%}" for d in rest) + ".",
                lean_eff(sum(d["effect"] for d in rest)))

    if proj["home"] is not None and not ml:
        hf = proj["home_factor"] - 1
        add(f"{'Home' if proj['home'] else 'Road'} game: {hf:+.1%} venue adjustment "
            f"(league home edge plus 20% of the player's own split).", "over" if hf > 0 else "under")

    if proj["b2b"] and not ml:
        add("Second night of a back-to-back: projected minutes trimmed 3.5%.", "under")

    if not ml and proj["dev_pct"] and abs(proj["dev_pct"]) >= 0.005:
        exp = proj["experience"]
        why = "young-player development" if proj["dev_pct"] > 0 else "veteran aging curve"
        add(f"Year {exp + 1} in the league: {proj['dev_pct']:+.1%} for {why}, applied to the "
            f"{proj['prior_share']:.0%} of the estimate built from prior seasons.",
            "over" if proj["dev_pct"] > 0 else "under")

    if proj["team_change"]:
        add(f"Changed teams ({player.last_team} → {player.team}); role and "
            f"usage are uncertain, so the outcome range was widened 20% and a higher EV bar is required.",
            "neutral")

    if proj["prior_share"] > 0.8:
        add(f"Early in the {season_label(proj['target_season'])} season: the projection leans on last "
            f"season's data and is updated as new games are logged.", "neutral")

    cv = proj["sd"] / mean if mean else 0
    if cv > 0.45:
        add(f"High volatility (std dev {_fmt(proj['sd'])}, {cv:.0%} of the mean); results swing a lot "
            f"game to game, which compresses the edge.", "neutral")

    sides = result["sides"]
    o = sides["OVER"]
    add(f"Market: the book's prices imply {o['market_prob']:.1%} over after removing "
        f"{result['vig']:.1%} vig; the model says {o['model_prob']:.1%} "
        f"({o['model_prob'] - o['market_prob']:+.1%}).",
        "over" if o["model_prob"] > o["market_prob"] + 0.01 else
        "under" if o["model_prob"] < o["market_prob"] - 0.01 else "neutral")

    return {"summary": _summary(result, line), "points": pts}


def _summary(result, line):
    d = result["decision"]
    best = result["sides"][result["best_side"]]
    odds = f"{best['odds']:+d}"
    if d == "PASS":
        return (f"PASS. The best side ({result['best_side'].lower()} at {odds}) is worth "
                f"{best['ev']:+.1%} EV, below the {result['threshold']:.1%} bar. "
                f"Model and market are too close to justify a bet.")
    return (f"{result['tier']} {d.lower()} {line} at {odds}. Win chance {best['blended_prob']:.1%} vs "
            f"{best['breakeven']:.1%} needed to break even, so {best['ev']:+.1%} EV "
            f"(${best['ev_per_100']:+.2f} per $100).")


def season_reasoning(proj, ctx, player):
    """Short explanation for a season-long per-game projection."""
    pts = []
    if ctx and ctx["season_avg"] is not None:
        pts.append(f"{ctx['season']}: {_fmt(ctx['season_avg'])} per game in {ctx['season_n']} games "
                   f"({_fmt(ctx['season_min'])} mpg); last 10: {_fmt(ctx['last10_avg'])}.")
    pts.append(f"Per-minute rate {proj['rate'] * 36:.1f} per 36 (recent {proj['rate_recent'] * 36:.1f}, "
               f"long-run {proj['rate_long'] * 36:.1f}, league {proj['league_rate'] * 36:.1f}) "
               f"x {_fmt(proj['minutes'])} projected minutes.")
    if proj["dev_pct"] and abs(proj["dev_pct"]) >= 0.005:
        pts.append(f"Experience adjustment {proj['dev_pct']:+.1%} (entering year {proj['experience'] + 1}).")
    if proj["team_change"]:
        pts.append(f"New team ({player.last_team} → {player.team}); role uncertainty widens the range.")
    return pts
