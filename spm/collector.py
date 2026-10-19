"""Free line collection: snapshot every available player-prop line, then grade it.

Sources
* Action Network public feed (free, no key). Unofficial: it can change without
  notice. Raw responses are saved so they can be re-parsed if the format moves.
* The Odds API, when a key is configured.

Every snapshot is appended to data/lines/lines.csv. Once games are played,
grade() joins the last snapshot before tip-off (the closing line) with the
box score, giving the real-line history needed to test the model for profit.
"""

import csv
import datetime as dt
import json
import os
import re
import threading
import time

from . import odds_api
from .data import DATA_DIR, TEAMS, _http_json, _read_cache, _write_cache
from .model import STATS, stat_value

def _lines_dir():
    """data/lines by default; set "lines_dir" in config.json to keep collected lines elsewhere (e.g. OneDrive)."""
    path = os.path.join(os.path.dirname(DATA_DIR), "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            custom = json.load(f).get("lines_dir")
        if custom:
            return os.path.expandvars(os.path.expanduser(custom))
    return os.path.join(DATA_DIR, "lines")


LINES_DIR = _lines_dir()
RAW_DIR = os.path.join(LINES_DIR, "raw")
LINES_CSV = os.path.join(LINES_DIR, "lines.csv")      # one row per new line or price change
CURRENT_JSON = os.path.join(LINES_DIR, "current.json")  # latest known price for every open line
GRADED_CSV = os.path.join(LINES_DIR, "graded.csv")
FIELDS = ["snapshot", "source", "game_date", "tipoff", "player", "player_id", "team", "opponent", "home",
          "stat", "book", "line", "over_odds", "under_odds"]
AN = "https://api.actionnetwork.com/web"
AN_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
              "Accept": "application/json"}
AN_MARKETS = {
    "core_bet_type_27_points": "PTS", "core_bet_type_23_rebounds": "REB", "core_bet_type_26_assists": "AST",
    "core_bet_type_21_3fgm": "3PM", "core_bet_type_24_steals": "STL", "core_bet_type_25_blocks": "BLK",
    "core_bet_type_580_turnovers": "TOV", "core_bet_type_85_points_rebounds_assists": "PRA",
    "core_bet_type_86_points_rebounds": "PR", "core_bet_type_87_points_assists": "PA",
    "core_bet_type_88_rebounds_assists": "RA", "core_bet_type_89_steals_blocks": "SB",
}
_lock = threading.Lock()


# --------------------------------------------------------------------------- Action Network

def _an(path, params=None):
    return _http_json(f"{AN}{path}", params, headers=AN_HEADERS, retries=2, timeout=30)


def _an_books():
    cached = _read_cache("an_books.json")
    if cached and time.time() - cached["fetched"] < 7 * 86400:
        return {int(k): v for k, v in cached["books"].items()}
    books = {b["id"]: b["display_name"] for b in _an("/v1/books").get("books", [])}
    _write_cache("an_books.json", {"fetched": time.time(), "books": books})
    return books


def an_games(day):
    d = _an("/v1/scoreboard/nba", {"date": day.strftime("%Y%m%d")})
    out = []
    for g in d.get("games", []):
        teams = {t["id"]: t.get("abbr") for t in g.get("teams", [])}
        out.append({"id": g["id"], "start": g.get("start_time"), "type": g.get("type"),
                    "home": teams.get(g.get("home_team_id")), "away": teams.get(g.get("away_team_id"))})
    return out


NOT_BOOKS = {"consensus", "open", "polymarket"}  # reference lines / exchange prices, not sportsbook odds
BOOK_ALIASES = {"DK": "DraftKings", "MGM": "BetMGM", "ESPNBet": "ESPN BET"}


def _book_name(raw):
    """'DK NJ' -> 'DraftKings', 'FanDuel NJ' -> 'FanDuel'."""
    name = re.sub(r"\s+[A-Z]{2}$", "", raw.strip())
    return BOOK_ALIASES.get(name, name)


def parse_an_props(payload, books):
    """One market's props from /v2/games/{id}/props?customPickType=... ->
    list of (player_name, stat, book, line, over_odds, under_odds)."""
    players = payload.get("players") or []
    if isinstance(players, dict):
        players = list(players.values())
    names = {p.get("id"): p.get("full_name") or "" for p in players}
    out = {}
    for market, entries in (payload.get("player_props") or {}).items():
        stat = AN_MARKETS.get(market)
        if not stat:
            continue
        for entry in entries:
            for book_id, outcomes in (entry.get("lines") or {}).items():
                book = _book_name(books.get(int(book_id), str(book_id)))
                if book.lower() in NOT_BOOKS:
                    continue
                for o in outcomes:
                    if o.get("is_alt_market") or o.get("is_live") or o.get("side") not in ("over", "under"):
                        continue
                    name = names.get(o.get("player_id") or entry.get("player_id"))
                    if not name or o.get("odds") is None or o.get("value") is None:
                        continue
                    key = (name, stat, book, float(o["value"]))
                    out.setdefault(key, {})[o["side"]] = int(o["odds"])
    return [(n, s, b, l, px["over"], px["under"]) for (n, s, b, l), px in out.items()
            if "over" in px and "under" in px]


def fetch_action_network(ds, days=2, log=print, state="NJ"):
    """Every player prop for games today and tomorrow (one request per game per market)."""
    books = _an_books()
    os.makedirs(RAW_DIR, exist_ok=True)
    rows = []
    today = dt.date.today()
    stamp = int(time.time())
    for i in range(days):
        day = today + dt.timedelta(days=i)
        for g in an_games(day):
            if g["type"] != "reg":
                continue
            raw = {}
            for market in AN_MARKETS:
                try:
                    payload = _an(f"/v2/games/{g['id']}/props", {"customPickType": market, "stateCode": state})
                except Exception as e:
                    log(f"  {g['away']}@{g['home']} {market}: {e}")
                    continue
                time.sleep(0.25)
                if not payload.get("player_props"):
                    continue
                raw[market] = payload
                for name, stat, book, line, over, under in parse_an_props(payload, books):
                    p = ds.find_player(name)
                    if not p:
                        continue
                    home = p.team == g["home"] if p.team in (g["home"], g["away"]) else None
                    rows.append({"source": "action_network", "game_date": day.isoformat(), "tipoff": g["start"] or "",
                                 "player": p.name, "player_id": p.id, "team": p.team,
                                 "opponent": (g["away"] if home else g["home"]) if home is not None else "",
                                 "home": "" if home is None else int(home), "stat": stat, "book": book,
                                 "line": line, "over_odds": over, "under_odds": under})
            if raw:
                with open(os.path.join(RAW_DIR, f"an_{g['id']}.json"), "w", encoding="utf-8") as f:
                    json.dump(raw, f)
    return rows


# --------------------------------------------------------------------------- snapshot + grade

def snapshot(ds, log=print):
    """Collect every available line now and append it to lines.csv. Returns a summary."""
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    rows, notes = [], []
    try:
        an = fetch_action_network(ds, log=log)
        rows += an
        notes.append(f"Action Network: {len(an)} lines")
    except Exception as e:
        notes.append(f"Action Network failed: {e}")
    if odds_api.enabled():
        try:
            fetched, _ = odds_api.fetch_props(ds)
            for p in fetched:
                pl = ds.find_player(p["player"])
                rows.append({"source": "odds_api", "game_date": p["game_date"], "tipoff": "", "player": p["player"],
                             "player_id": pl.id if pl else "", "team": pl.team if pl else "",
                             "opponent": p["opponent"], "home": "" if p["home"] is None else int(p["home"]),
                             "stat": p["stat"], "book": p["book"], "line": p["line"],
                             "over_odds": p["over_odds"], "under_odds": p["under_odds"]})
            notes.append(f"The Odds API: {len(fetched)} lines")
        except Exception as e:
            notes.append(f"The Odds API failed: {e}")
    os.makedirs(LINES_DIR, exist_ok=True)
    with _lock:
        changed = _record(rows, stamp)
    _prune_raw()
    summary = {"snapshot": stamp, "lines": len(rows), "changed": changed, "notes": notes}
    _write_cache("collector_last.json", summary)
    log(f"Line snapshot {stamp}: " + "; ".join(notes) + f" ({changed} new or moved)")
    return summary


RAW_KEEP_DAYS = 3


def _prune_raw():
    """Raw feed copies are only for repairing the parser if the format changes; keep a few days."""
    cutoff = time.time() - RAW_KEEP_DAYS * 86400
    for name in os.listdir(RAW_DIR) if os.path.isdir(RAW_DIR) else []:
        path = os.path.join(RAW_DIR, name)
        if os.path.getmtime(path) < cutoff:
            os.remove(path)


def _key(r):
    return f"{r['player_id']}|{r['stat']}|{r['book']}|{r['game_date']}"


def _record(rows, stamp):
    """Append only lines that are new or whose line/price moved; track every open line in current.json."""
    current = {}
    if os.path.exists(CURRENT_JSON):
        with open(CURRENT_JSON, encoding="utf-8") as f:
            current = json.load(f)
    cutoff = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    current = {k: v for k, v in current.items() if v["game_date"] >= cutoff}
    new = not os.path.exists(LINES_CSV)
    changed = 0
    with open(LINES_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        for r in rows:
            r = {k: str(v) for k, v in {**r, "snapshot": stamp}.items()}
            k = _key(r)
            old = current.get(k)
            if not old or (old["line"], old["over_odds"], old["under_odds"]) != (r["line"], r["over_odds"], r["under_odds"]):
                w.writerow({f: r.get(f, "") for f in FIELDS})
                changed += 1
            current[k] = {**r, "last_seen": stamp}
    with open(CURRENT_JSON, "w", encoding="utf-8") as f:
        json.dump(current, f)
    return changed


def latest_rows():
    """Lines seen in the most recent snapshot (to show on the props board)."""
    if not os.path.exists(CURRENT_JSON):
        return []
    with open(CURRENT_JSON, encoding="utf-8") as f:
        current = json.load(f)
    if not current:
        return []
    last = max(v["last_seen"] for v in current.values())
    return [v for v in current.values() if v["last_seen"] == last]


def grade(ds):
    """Closing line (last snapshot before tip) per player/stat/book/game, settled against the box score."""
    if not os.path.exists(LINES_CSV):
        return {"collected": 0, "graded": 0, "days": 0}
    with open(LINES_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    def tip_local(t):
        # tip-off times are UTC; snapshots are local time
        return dt.datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone().replace(tzinfo=None).isoformat(timespec="seconds")

    # rows are price changes; the last change before tip-off is the closing price
    closing = {}
    for r in rows:
        if r["tipoff"] and r["snapshot"] >= tip_local(r["tipoff"]):
            continue  # moved after tip-off
        key = (r["player_id"], r["stat"], r["book"], r["game_date"])
        if key not in closing or r["snapshot"] > closing[key]["snapshot"]:
            closing[key] = r
    graded = []
    for r in closing.values():
        p = ds.players.get(int(r["player_id"])) if r["player_id"] else None
        g = next((g for g in (p.games if p else []) if g.date == r["game_date"]), None)
        if not g:
            continue
        actual = stat_value(g, STATS[r["stat"]][1])
        line = float(r["line"])
        result = "over" if actual > line else "under" if actual < line else "push"
        graded.append({**r, "actual": actual, "result": result})
    if graded:
        with open(GRADED_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS + ["actual", "result"])
            w.writeheader()
            w.writerows(graded)
    return {"collected": len(rows), "closing_lines": len(closing), "graded": len(graded),
            "days": len({r["game_date"] for r in rows}),
            "last": _read_cache("collector_last.json")}


class Scheduler:
    """Takes a snapshot every few hours while the app is running."""

    def __init__(self, get_ds, every_hours=3, log=print):
        self.get_ds, self.every, self.log = get_ds, every_hours * 3600, log
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        time.sleep(60)
        while True:
            ds = self.get_ds()
            if ds is not None:
                try:
                    snapshot(ds, log=self.log)
                except Exception as e:
                    self.log(f"Line snapshot failed: {e}")
            time.sleep(self.every)
