"""Free line collection: snapshot every available player-prop line, then grade it.

Sources come from spm/odds (one interface; ODDS_SOURCES picks which). Every row
keeps its `source` so unlicensed (scraped) data can be excluded from commercial
use. Raw responses are kept a few days so parsers can be fixed if a format moves.

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

from . import odds
from .data import DATA_DIR, _read_cache, _write_cache
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
_lock = threading.Lock()


# --------------------------------------------------------------------------- offers -> rows

def _to_row(offer, ds):
    """Resolve an Offer's player name to our NBA player id/team and flatten it for storage."""
    p = ds.find_player(offer.player_name)
    if not p:
        return None
    team = p.team or offer.team
    home = team == offer.home_team if team in (offer.home_team, offer.away_team) else None
    tip = offer.start_time
    game_date = (dt.datetime.fromisoformat(tip.replace("Z", "+00:00")).astimezone().date().isoformat()
                 if tip else dt.date.today().isoformat())
    return {"source": offer.source, "game_date": game_date, "tipoff": tip, "player": p.name, "player_id": p.id,
            "team": team, "opponent": (offer.away_team if home else offer.home_team) if home is not None else "",
            "home": "" if home is None else int(home), "stat": offer.market, "book": offer.book,
            "line": offer.line, "over_odds": offer.over_odds, "under_odds": offer.under_odds}


def _save_raw(source):
    os.makedirs(RAW_DIR, exist_ok=True)
    for event_id, payload in source.raw.items():
        with open(os.path.join(RAW_DIR, f"{source.name}_{event_id}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)


# --------------------------------------------------------------------------- snapshot + grade

def snapshot(ds, log=print):
    """Collect every available line now and append it to lines.csv. Returns a summary."""
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    rows, notes = [], []
    for source in odds.enabled_sources():
        try:
            offers = source.fetch("nba", days=2)
        except Exception as e:
            notes.append(f"{source.name} failed: {e}")
            continue
        _save_raw(source)
        matched = [r for r in (_to_row(o, ds) for o in offers) if r]
        rows += matched
        note = f"{source.name}: {len(matched)} lines"
        if len(matched) < len(offers):
            note += f" ({len(offers) - len(matched)} unmatched players)"
        if source.errors:
            note += f", {len(source.errors)} failed requests"
            for err in source.errors[:5]:
                log(f"  {err}")
        notes.append(note)
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
