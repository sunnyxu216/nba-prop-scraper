"""Run the NBA model where stats.nba.com is unreachable (GitHub Actions blocks are by IP).

The model needs two kinds of data:

* history that never changes (past seasons' game logs, shot zones, play types, defense,
  debut years, the season's roster) plus the trained model files. A home PC packs these into
  one bundle (`make_bundle`, scripts/publish_nba_model.py) stored privately in the database;
  cloud jobs download it once per version (`install_bundle`).
* this season's game logs, which grow daily. They are rebuilt from the ESPN box scores the
  grader already loads into `results` (`current_season_logs`), fetched incrementally.

`prepare()` writes both into the normal cache files, stamped fresh, so data.load_dataset()
finds everything locally and never tries stats.nba.com.
"""

import datetime as dt
import glob
import io
import os
import tarfile
import time
import zlib

from . import data
from .store import grading

BUNDLE = "nba_model"
VERSION_FILE = "bundle_version.txt"
RESULTS_CACHE = "results_nba.json"


# --------------------------------------------------------------------------- bundle (home PC)

def bundle_paths(data_dir=None, current=None):
    """Files that go into the bundle, relative to the data folder."""
    data_dir = data_dir or data.DATA_DIR
    current = current or data.season_start()
    names = [f"cache/logs_{s}.json" for s in range(data.FIRST_SEASON, current)]
    names += [f"cache/{kind}_{s}.json" for kind in ("advanced", "defense") for s in range(data.FIRST_SEASON, current)]
    names += ["cache/debut_years.json", f"cache/roster_{current}.json"]
    names += [os.path.relpath(p, data_dir).replace(os.sep, "/")
              for p in sorted(glob.glob(os.path.join(data_dir, "models", "*")))
              if p.endswith((".txt", "report.json"))]
    missing = [n for n in names if not os.path.exists(os.path.join(data_dir, n))]
    if missing:
        raise FileNotFoundError(f"missing from {data_dir}: {', '.join(missing)}")
    return names


def make_bundle(data_dir=None, current=None):
    data_dir = data_dir or data.DATA_DIR
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=9) as tar:
        for name in bundle_paths(data_dir, current):
            info = tar.gettarinfo(os.path.join(data_dir, name), arcname=name)
            info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""   # same files -> same hash
            with open(os.path.join(data_dir, name), "rb") as f:
                tar.addfile(info, f)
    return buf.getvalue()


# --------------------------------------------------------------------------- bundle (cloud)

def installed_version(data_dir=None):
    path = os.path.join(data_dir or data.DATA_DIR, VERSION_FILE)
    return open(path, encoding="utf-8").read().strip() if os.path.exists(path) else None


def install_bundle(blob, version, data_dir=None):
    """Unpack into the data folder. Only plain files under cache/ and models/ are accepted."""
    data_dir = data_dir or data.DATA_DIR
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for m in tar.getmembers():
            parts = m.name.split("/")
            if not m.isfile() or parts[0] not in ("cache", "models") or ".." in parts or m.name.startswith("/"):
                raise ValueError(f"unexpected file in bundle: {m.name}")
            dest = os.path.join(data_dir, *parts)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with tar.extractfile(m) as src, open(dest, "wb") as out:
                out.write(src.read())
    with open(os.path.join(data_dir, VERSION_FILE), "w", encoding="utf-8") as f:
        f.write(version)


def ensure_bundle(conn, log=print):
    """Download the bundle unless this version is already installed. Returns the version."""
    from .store import artifacts
    version = artifacts.version_of(conn, BUNDLE)
    if version is None:
        raise RuntimeError("no NBA model bundle in the database: run scripts/publish_nba_model.py at home")
    if installed_version() != version:
        log(f"Downloading NBA model bundle {version} ...")
        version, blob = artifacts.get(conn, BUNDLE)
        install_bundle(blob, version)
    return version


# --------------------------------------------------------------------------- this season's logs

def fetch_results(conn, season_start_date, cache=None):
    """Box-score rows for this season, fetching only rows loaded since the cached copy (stat
    corrections re-load rows, so they come through too). Returns the updated cache dict."""
    cache = cache or {"loaded_at": None, "rows": {}}
    if cache.get("season_start") != season_start_date.isoformat():
        cache = {"loaded_at": None, "rows": {}, "season_start": season_start_date.isoformat()}
    with conn.cursor() as cur:
        cur.execute("""select game_date, source_game_id, player_key, player_name, team, opponent, minutes::float,
                              pts, reb, ast, fg3m, stl, blk, tov, loaded_at from results
                       where sport = 'nba' and game_date >= %s and loaded_at > coalesce(%s::timestamptz, '-infinity')
                       order by loaded_at""", (season_start_date, cache["loaded_at"]))
        for r in cur.fetchall():
            cache["rows"][f"{r[1]}|{r[2]}"] = [r[0].isoformat(), *r[1:14]]
            cache["loaded_at"] = r[14].isoformat()
        cur.execute("""select start_time, home_team from events where sport = 'nba' and start_time >= %s""",
                    (season_start_date,))
        cache["home"] = sorted({f"{grading.et_date(s).isoformat()}|{h}" for s, h in cur.fetchall()} |
                               set(cache.get("home", [])))
    return cache


def name_index(season_rows, roster):
    """Normalized name -> player id: rostered players first, then the most recent season's."""
    idx = {}
    for start in sorted(season_rows):
        for r in season_rows[start]:
            idx[data.norm_name(r[1])] = r[0]
    for p in roster or []:
        idx[data.norm_name(p["name"])] = p["id"]
    return idx


def synthetic_id(key):
    """A stable id for a player with no stats.nba.com history (e.g. a rookie): negative, never clashes."""
    return -(zlib.crc32(key.encode()) or 1)


def current_season_logs(results, home_games, index, before=None):
    """results rows ([date, game_id, key, name, team, opp, min, pts..tov]) -> data.py log rows.

    home_games: {"YYYY-MM-DD|TEAM"} for home teams. before: only games before this date (backfills).
    """
    rows = []
    for date, gid, key, name, team, opp, mins, *stats in results:
        if before and date >= before:
            continue
        pid = index.get(key)
        if pid is None:
            alt = grading.match_name(key, index)
            pid = index[alt] if alt else synthetic_id(key)
        rows.append([pid, name, team, gid, date, opp, 1 if f"{date}|{team}" in home_games else 0,
                     round(float(mins), 2), *stats])
    rows.sort(key=lambda r: (r[4], r[3]))
    return rows


def refresh_roster(roster, logs):
    """The roster snapshot with each player's team updated to the team of his latest game (trades),
    plus players who appeared this season but weren't on it."""
    latest = {}
    for r in logs:
        latest[r[0]] = (r[1], r[2])
    out, seen = [], set()
    for p in roster or []:
        name, team = latest.get(p["id"], (p["name"], p["team"]))
        out.append({**p, "team": team})
        seen.add(p["id"])
    out += [{"id": pid, "name": name, "team": team, "from_year": None}
            for pid, (name, team) in latest.items() if pid not in seen]
    return out


def prepare(conn, before=None, log=print):
    """Bundle + this season's logs into the cache, stamped fresh. Returns the bundle version."""
    version = ensure_bundle(conn, log)
    current = data.season_start()
    cache = fetch_results(conn, dt.date(current, 8, 1), data._read_cache(RESULTS_CACHE))
    data._write_cache(RESULTS_CACHE, cache)
    season_rows = {s: data._read_cache(f"logs_{s}.json")["rows"] for s in range(data.FIRST_SEASON, current)}
    roster = data._read_cache(f"roster_{current}.json")["players"]
    logs = current_season_logs(cache["rows"].values(), set(cache["home"]), name_index(season_rows, roster), before)
    now = time.time()
    data._write_cache(f"logs_{current}.json", {"fetched": now, "rows": logs})
    data._write_cache(f"roster_{current}.json", {"fetched": now, "players": refresh_roster(roster, logs)})
    debut = data._read_cache("debut_years.json")
    data._write_cache("debut_years.json", {**debut, "fetched": now})
    log(f"This season: {len(logs):,} player-games from {len({r[3] for r in logs})} games")
    return version


def b2b_teams(logs, day):
    """Teams that played the day before `day` (YYYY-MM-DD)."""
    prev = (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()
    return {r[2] for r in logs if r[4] == prev}


# --------------------------------------------------------------------------- scoring

class Scorer:
    """Projection, EV and decision for collected offers, shaped for spm/store/paper.record()."""

    def __init__(self, version, date=None, injuries=None, log=print, settings=None):
        from . import service
        self.service = service
        quiet = lambda *a: None
        self.ds = data.load_dataset(log=quiet)
        self.engine = service.Engine(self.ds, log=quiet, date=date, injuries=injuries)
        if not self.engine.predictor:
            raise RuntimeError("the bundle has no trained model")
        trained = self.engine.predictor.report.get("trained_at")
        self.version = f"nba-{time.strftime('%Y%m%d', time.gmtime(trained)) if trained else 'unknown'}-{version[:8]}"
        self.settings = settings or {}
        self.logs = data._read_cache(f"logs_{self.ds.current_start}.json")["rows"]
        log(f"NBA scorer ready (model {self.version})")

    def score(self, offers):
        """offers: paper.open_offers() rows. Returns one result per offer (skipped ones say why)."""
        days = {(o["start_time"] - dt.timedelta(hours=5)).date().isoformat() for o in offers}
        b2b = {d: b2b_teams(self.logs, d) for d in days}
        props = []
        for o in offers:
            day = (o["start_time"] - dt.timedelta(hours=5)).date().isoformat()
            props.append(self.service.offers_to_props(self.ds, [o], b2b[day])[0])
        idx = [i for i, p in enumerate(props) if p]
        for i in idx:
            props[i]["id"] = str(i)
        groups = self.service.evaluate_board(self.engine, [props[i] for i in idx], self.settings)
        inj = self.engine.injuries()
        out = [{"decision": None, "skipped": "not an NBA prop the model knows"} for _ in offers]
        for g in groups:
            for o in g["offers"]:
                i = int(o["prop"]["id"])
                if not g["ok"]:
                    out[i] = {"decision": None, "skipped": g.get("error", "not scored")}
                    continue
                e, p = o["ev"], o["prop"]
                pid = g["player"]["id"]
                out[i] = {
                    "projection": g["projection"]["mean"], "p_over": o["p_over"], "p_under": o["p_under"],
                    "p_push": o["p_push"], "decision": e["decision"], "model_call": e["decision"],
                    "tier": e["tier"], "ev_over": e["sides"]["OVER"]["ev"], "ev_under": e["sides"]["UNDER"]["ev"],
                    "market_prob_over": e["sides"]["OVER"]["market_prob"],
                    "stake_pct": e["sides"][e["best_side"]]["kelly_pct"] if e["decision"] != "PASS" else 0.0,
                    "low_confidence": bool(e["low_confidence"]), "track_only": False, "outside_training": False,
                    "one_sided": False, "kind": "count", "position": "", "model_version": self.version,
                    "team": g["player"]["team"] or "", "opp": p["opponent"],
                    "injury_status": (inj.get(pid) or {}).get("status"),
                    "reasoning": [pt["text"] for pt in g["reasoning"].get("points", [])],
                }
        return out
