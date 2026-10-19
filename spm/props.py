"""Prop lines live in data/props.csv so they can also be edited in a spreadsheet."""

import csv
import math
import os
import threading
import uuid

from .data import DATA_DIR, TEAMS
from .model import STATS

PROPS_PATH = os.path.join(DATA_DIR, "props.csv")
FIELDS = ["id", "game_date", "player", "stat", "line", "over_odds", "under_odds",
          "opponent", "home", "b2b", "book", "source"]
_lock = threading.Lock()


def _bool(v):
    return str(v).strip().lower() in {"1", "true", "yes", "y", "home", "h"}


def _home(v):
    v = str(v).strip().lower()
    if v in {"", "none", "?"}:
        return None
    return _bool(v)


def clean(raw):
    """Validate one prop dict; raises ValueError with a readable message."""
    stat = str(raw.get("stat", "")).strip().upper()
    if stat not in STATS:
        raise ValueError(f"Unknown stat '{stat}'. Use one of: {', '.join(STATS)}")
    player = str(raw.get("player", "")).strip()
    if not player:
        raise ValueError("Player is required")
    try:
        line = float(raw.get("line"))
        over = int(float(raw.get("over_odds") or -110))
        under = int(float(raw.get("under_odds") or -110))
    except (TypeError, ValueError):
        raise ValueError("Line and odds must be numbers")
    for o in (over, under):
        if -100 < o < 100:
            raise ValueError("American odds must be <= -100 or >= +100")
    opp = str(raw.get("opponent") or "").strip().upper()
    if opp and opp not in TEAMS:
        raise ValueError(f"Unknown opponent '{opp}'. Use a team abbreviation like BOS")
    return {
        "id": str(raw.get("id") or uuid.uuid4().hex[:8]),
        "game_date": str(raw.get("game_date") or "").strip(),
        "player": player, "stat": stat, "line": line,
        "over_odds": over, "under_odds": under, "opponent": opp,
        "home": raw.get("home") if isinstance(raw.get("home"), bool) or raw.get("home") is None
        else _home(raw.get("home")),
        "b2b": raw.get("b2b") if isinstance(raw.get("b2b"), bool) else _bool(raw.get("b2b", "")),
        "book": str(raw.get("book") or "").strip(),
        "source": str(raw.get("source") or "manual").strip(),
    }


def load():
    if not os.path.exists(PROPS_PATH):
        return []
    with _lock, open(PROPS_PATH, newline="", encoding="utf-8") as f:
        out = []
        for row in csv.DictReader(f):
            try:
                out.append(clean(row))
            except ValueError:
                continue
        return out


def save(props):
    os.makedirs(DATA_DIR, exist_ok=True)
    with _lock:
        tmp = PROPS_PATH + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            for p in props:
                row = dict(p)
                row["home"] = "" if p["home"] is None else ("home" if p["home"] else "away")
                row["b2b"] = "yes" if p["b2b"] else ""
                w.writerow({k: row.get(k, "") for k in FIELDS})
        os.replace(tmp, PROPS_PATH)


def add(raw):
    prop = clean(raw)
    props = load()
    props.append(prop)
    save(props)
    return prop


def delete(prop_id):
    props = load()
    save([p for p in props if p["id"] != prop_id])


def remove_source(*sources):
    props = load()
    save([p for p in props if p["source"] not in sources])


def seed_examples(ds):
    """First-run sample slate so the board isn't empty.

    Lines are each player's last-season average rounded to the nearest .5 at -110,
    which is roughly where books open. They are NOT real sportsbook lines.
    """
    if os.path.exists(PROPS_PATH):
        return
    latest = max((s for s in ds.league_avg if s < ds.current_start), default=None)
    cands = []
    for p in ds.players.values():
        if not p.on_roster or not p.team:
            continue
        gs = [g for g in p.games if g.season == latest]
        if len(gs) < 45:
            continue
        avg = lambda f: sum(f(g) for g in gs) / len(gs)
        cands.append((avg(lambda g: g.pts), p, {
            "PTS": avg(lambda g: g.pts), "REB": avg(lambda g: g.reb), "AST": avg(lambda g: g.ast),
            "3PM": avg(lambda g: g.fg3m), "PRA": avg(lambda g: g.pts + g.reb + g.ast),
        }))
    cands.sort(key=lambda c: -c[0])
    teams = sorted(TEAMS)
    picks = [("PTS", "PRA"), ("PTS", "REB"), ("PTS", "AST"), ("PTS", "3PM")]
    out = []
    for i, (_, p, avgs) in enumerate(cands[:12]):
        opp = teams[(i * 7 + 3) % len(teams)]
        if opp == p.team:
            opp = teams[(i * 7 + 4) % len(teams)]
        for stat in picks[i % len(picks)]:
            out.append(clean({
                "player": p.name, "stat": stat, "line": math.floor(avgs[stat]) + 0.5,
                "over_odds": -110, "under_odds": -110, "opponent": opp,
                "home": i % 2 == 0, "b2b": False, "book": "Example", "source": "example",
            }))
    save(out)
