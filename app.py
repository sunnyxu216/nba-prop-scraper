"""NBA Prop Model: local web app.

    python app.py            -> http://127.0.0.1:8000
    python app.py --port 9000
"""

import argparse
import datetime as dt
import json
import mimetypes
import os
import socket
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from spm import advanced, backtest, collector, data, ev, features, ml, model, odds, props, service, yearly

STATIC = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "static")

STATE = {
    "ds": None, "engine": None, "status": "loading", "log": [], "error": None,
    "season": None, "bt_status": "idle", "bt_progress": 0.0, "bt_error": None,
    "train_status": "idle", "train_progress": 0.0, "train_error": None,
    "yearly_status": "idle", "yearly_progress": 0.0, "yearly_error": None, "scheduler": None,
    "nfl_status": "idle", "nfl_error": None, "nfl_local": None, "nfl_local_at": None,
}
LOCK = threading.Lock()


def log(msg):
    print(msg, flush=True)
    STATE["log"] = (STATE["log"] + [msg])[-12:]


def load(force=False):
    STATE.update(status="loading", error=None)
    try:
        ds = data.load_dataset(force=force, log=log)
        bt = None if force else backtest.cached(ds)
        if bt is None or "platt" not in bt:
            log("Running backtest to calibrate probabilities ...")
            bt = backtest.run(ds)
        ds.calibration = bt["platt"]
        engine = service.Engine(ds, log=log)
        season = service.season_projections(engine)
        props.seed_examples(ds)
        with LOCK:
            STATE.update(ds=ds, engine=engine, season=season, status="ready")
        # With a database configured, the cloud collector (GitHub Actions) does the scraping;
        # only fall back to snapshotting from this PC when there's no database.
        if STATE["scheduler"] is None and not os.environ.get("DATABASE_URL"):
            STATE["scheduler"] = collector.Scheduler(lambda: STATE["ds"], every_hours=3, log=log)
        log("Ready.")
    except Exception as e:
        traceback.print_exc()
        STATE.update(status="error", error=str(e))


def run_backtest():
    ds = STATE["ds"]
    STATE.update(bt_status="running", bt_progress=0.0, bt_error=None)
    try:
        ds.calibration = backtest.run(ds, progress=lambda f: STATE.update(bt_progress=f))["platt"]
        STATE["bt_status"] = "done"
    except Exception as e:
        traceback.print_exc()
        STATE.update(bt_status="error", bt_error=str(e))


def run_training():
    ds = STATE["ds"]
    STATE.update(train_status="running", train_progress=0.0, train_error=None)
    try:
        log("Preparing shot-zone and play-type history (first run downloads it) ...")
        prof = advanced.load_profiles(data.FIRST_SEASON, ds.current_start, log)
        store = features.FeatureStore(ds, prof, build_training=True, log=log)
        ml.train(store, log=log, progress=lambda f: STATE.update(train_progress=f))
        del store
        engine = service.Engine(ds, log=log)
        season = service.season_projections(engine)
        with LOCK:
            STATE.update(engine=engine, season=season)
        STATE["train_status"] = "done"
    except Exception as e:
        traceback.print_exc()
        STATE.update(train_status="error", train_error=str(e))


def run_yearly():
    ds = STATE["ds"]
    STATE.update(yearly_status="running", yearly_progress=0.0, yearly_error=None)
    try:
        prof = advanced.load_profiles(data.FIRST_SEASON, ds.current_start, log)
        store = features.FeatureStore(ds, prof, build_training=True, log=log)
        yearly.run(store, log=log, progress=lambda f: STATE.update(yearly_progress=f))
        STATE["yearly_status"] = "done"
    except Exception as e:
        traceback.print_exc()
        STATE.update(yearly_status="error", yearly_error=str(e))


def run_nfl_scoring():
    """Score the open NFL lines on this PC (same model and code as the cloud job), for the board.

    Only reads from Supabase; the paper-trading log is written by the cloud job alone.
    """
    from spm.nfl import board as nfl_board, data as nfl_data, live as nfl_live
    from spm.store import paper, pg
    STATE.update(nfl_status="running", nfl_error=None)
    try:
        with pg.connect() as conn:
            offers = paper.open_offers(conn, "nfl", max_age_hours=6)
        log(f"NFL: scoring {len(offers)} open lines (first run downloads NFL history) ...")
        nfl_data.download_all(log=log)
        results = nfl_live.Scorer(log=log).score(offers)
        STATE.update(nfl_local=nfl_board.from_scored(offers, results),
                     nfl_local_at=dt.datetime.now(dt.timezone.utc), nfl_status="done")
        log("NFL: scoring done.")
    except Exception as e:
        traceback.print_exc()
        STATE.update(nfl_status="error", nfl_error=str(e))


def nfl_board_rows():
    """(rows, where they came from, when scored): the cloud's latest scoring, or a newer local run."""
    rows, origin, when = [], None, None
    if os.environ.get("DATABASE_URL"):
        from spm.store import paper, pg
        try:
            with pg.connect() as conn:
                rows = paper.latest(conn, "nfl")
            origin = "cloud"
            when = max((r["scored_at"] for r in rows), default=None)
        except Exception as e:     # e.g. migration 005 not applied yet
            log(f"NFL: no cloud predictions ({e.__class__.__name__})")
    local, local_at = STATE["nfl_local"], STATE["nfl_local_at"]
    if local is not None and (when is None or local_at > when):
        rows, origin, when = local, "local", local_at
    return rows, origin, when


def nfl_model_info():
    from spm.nfl import model as nfl_model
    info = {"trained": nfl_model.Predictor.available(), "paper": None}
    if info["trained"]:
        with open(os.path.join(nfl_model.MODEL_DIR, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        info.update({k: meta[k] for k in ("trained_at", "data_through", "protocol", "provenance")})
        info["markets"] = {m: {"kind": v["kind"], "n_train": v["n_train"], "holdout": v["holdout"],
                               "top_features": v["top_features"][:6]} for m, v in meta["markets"].items()}
    if os.environ.get("DATABASE_URL"):
        from spm.store import archive, pg
        try:
            with pg.connect() as conn:
                info["paper"] = archive.paper_summary(conn, "nfl")
        except Exception as e:
            info["paper_error"] = f"No paper-trading data yet ({e.__class__.__name__})"
    return info


def lines_to_props(rows):
    out = []
    for r in rows:
        try:
            out.append(props.clean({
                "player": r["player"], "stat": r["stat"], "line": r["line"], "over_odds": r["over_odds"],
                "under_odds": r["under_odds"], "opponent": r["opponent"], "game_date": r["game_date"],
                "home": None if r["home"] == "" else r["home"] == "1", "book": r["book"], "source": r["source"],
            }))
        except ValueError:
            continue
    return out


def cloud_lines_to_props(ds, rows):
    """Supabase offers_current rows -> props (resolving each player's team and the matchup)."""
    return [p for p in service.offers_to_props(ds, rows) if p]


STALE_LINES_AFTER = dt.timedelta(minutes=75)   # the collector runs hourly


def stale_line_notes(rows, now=None):
    """Warnings for games whose newest line is older than one collector run (lines may have moved)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    newest = {}
    for r in rows:
        game = f"{r['away_team']} @ {r['home_team']}"
        newest[game] = max(newest.get(game, r["last_seen"]), r["last_seen"])
    return [f"Stale: {game} lines were last updated {(now - seen).total_seconds() / 60:.0f} min ago "
            f"and may have moved since" for game, seen in newest.items() if now - seen > STALE_LINES_AFTER]


def settings_from(q):
    s = {}
    if "bias_adjust" in q:
        s["bias_adjust"] = q["bias_adjust"][0].lower() in ("1", "true", "yes", "on")
    if q.get("books"):
        s["books"] = [b for b in q["books"][0].split(",") if b]
    for key, cast in (("ev_threshold", float), ("market_blend", float), ("bankroll", float), ("kelly_mult", float)):
        if key in q:
            try:
                s[key] = cast(q[key][0])
            except ValueError:
                pass
    return s


class Handler(BaseHTTPRequestHandler):
    server_version = "NBAPropModel/1.0"

    def log_message(self, *args):
        pass

    # ----------------------------------------------------------------- plumbing

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _ds(self):
        ds = STATE["ds"]
        if ds is None:
            self._send(503, {"error": "Data is still loading"})
        return ds

    def _route(self, method):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        parts = [p for p in url.path.split("/") if p]
        try:
            if not parts or parts[0] != "api":
                return self._static(url.path)
            return self._api(method, parts[1:], q)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            return  # the browser gave up (closed tab, refresh) before the answer was ready
        except ValueError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": str(e)})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    def _static(self, path):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            return self._send(404, {"error": "not found"})
        with open(full, "rb") as f:
            self._send(200, f.read(), mimetypes.guess_type(full)[0] or "application/octet-stream")

    # ----------------------------------------------------------------- api

    def _api(self, method, parts, q):
        route = (method, parts[0] if parts else "")

        if route == ("GET", "status"):
            ds = STATE["ds"]
            return self._send(200, {
                "status": STATE["status"], "error": STATE["error"], "log": STATE["log"],
                "meta": ds.meta if ds else None,
                "odds_sources": [s.name for s in odds.enabled_sources()],
                "odds_api": any(s.name == "the_odds_api" for s in odds.enabled_sources()),
                "backtest": {"status": STATE["bt_status"], "progress": STATE["bt_progress"],
                             "error": STATE["bt_error"]},
                "model": STATE["engine"].mode if STATE["engine"] else None,
                "training": {"status": STATE["train_status"], "progress": STATE["train_progress"],
                             "error": STATE["train_error"]},
            })

        # NFL: independent of the NBA data, so these work while it loads
        if route == ("GET", "nfl-board"):
            from spm.nfl import board as nfl_board
            settings = settings_from(q)
            rows, origin, when = nfl_board_rows()
            groups = nfl_board.build(rows, settings, settings.get("books"))
            return self._send(200, {"props": groups, "origin": origin, "scored_at": str(when) if when else None,
                                    "books": sorted({r["book"] for r in rows}), "status": STATE["nfl_status"],
                                    "error": STATE["nfl_error"], "database": bool(os.environ.get("DATABASE_URL"))})

        if route == ("POST", "nfl-score"):
            if not os.environ.get("DATABASE_URL"):
                return self._send(400, {"error": "Scoring NFL lines needs DATABASE_URL (the collected lines live in Supabase)"})
            if STATE["nfl_status"] != "running":
                threading.Thread(target=run_nfl_scoring, daemon=True).start()
            return self._send(202, {"ok": True})

        if route == ("GET", "nfl-model"):
            return self._send(200, nfl_model_info())

        if route == ("POST", "refresh"):
            if STATE["status"] != "loading":
                threading.Thread(target=load, kwargs={"force": True}, daemon=True).start()
            return self._send(202, {"ok": True})

        ds = self._ds()
        if ds is None:
            return

        if route == ("GET", "meta"):
            names = sorted(p.name for p in ds.players.values() if p.on_roster)
            return self._send(200, {
                "stats": [{"key": k, "label": v[0]} for k, v in model.STATS.items()],
                "teams": [{"key": k, "name": v} for k, v in data.TEAMS.items()],
                "players": names, "defaults": ev.DEFAULTS,
            })

        if route == ("GET", "props"):
            settings = settings_from(q)
            rows = service.evaluate_board(STATE["engine"], props.load(), settings)
            books = sorted({p["book"] for p in props.load() if p["book"]})
            return self._send(200, {"props": rows, "books": books, "mode": STATE["engine"].mode})

        if route == ("POST", "props"):
            body = self._json_body()
            prop = props.add(body)
            return self._send(201, service.evaluate_prop(STATE["engine"], prop, body.get("settings")))

        if route == ("POST", "evaluate"):
            body = self._json_body()
            return self._send(200, service.evaluate_prop(STATE["engine"], props.clean(body), body.get("settings")))

        if route == ("DELETE", "props") and len(parts) == 2:
            props.delete(parts[1])
            return self._send(200, {"ok": True})

        if route == ("POST", "props-clear"):
            src = (q.get("source") or ["example"])[0]
            if src == "all":
                props.save([])
            else:
                props.remove_source(src)
            return self._send(200, {"ok": True})

        if route == ("POST", "odds-import") and os.environ.get("DATABASE_URL"):
            from spm.store import pg
            with pg.connect() as conn:
                rows = pg.current_lines(conn)
            fresh = cloud_lines_to_props(ds, rows)
            if fresh:
                props.remove_source(*odds.registered(), "odds_api", "example")
                props.save(props.load() + fresh)
            newest = max((r["last_seen"] for r in rows), default=None)
            note = (f"Supabase: {len(fresh)} lines for upcoming games (collected "
                    f"{newest:%b %d %H:%M} UTC)") if newest else "Supabase: no open lines for upcoming games"
            return self._send(200, {"imported": len(fresh), "notes": [note] + stale_line_notes(rows)})

        if route == ("POST", "odds-import"):
            summary = collector.snapshot(ds, log=log)
            fresh = lines_to_props(collector.latest_rows())
            if fresh:
                props.remove_source(*odds.registered(), "odds_api", "example")
                props.save(props.load() + fresh)
            return self._send(200, {"imported": len(fresh), "notes": summary["notes"]})

        if route == ("GET", "lines"):
            return self._send(200, collector.grade(ds))

        if route == ("GET", "yearly"):
            return self._send(200, {"result": yearly.load(), "status": STATE["yearly_status"],
                                    "progress": STATE["yearly_progress"], "error": STATE["yearly_error"]})

        if route == ("POST", "yearly"):
            if STATE["yearly_status"] != "running":
                threading.Thread(target=run_yearly, daemon=True).start()
            return self._send(202, {"ok": True})

        if route == ("GET", "projections"):
            return self._send(200, {"season": ds.meta["current_season"], "rows": STATE["season"]})

        if route == ("GET", "player") and len(parts) == 2:
            pid = int(parts[1])
            row = next((r for r in STATE["season"] or [] if r["id"] == pid), None)
            detail = service.player_detail(STATE["engine"], pid, row)
            return self._send(200 if detail else 404, detail or {"error": "player not found"})

        if route == ("GET", "backtest"):
            return self._send(200, {"result": backtest.cached(ds), "status": STATE["bt_status"],
                                    "progress": STATE["bt_progress"]})

        if route == ("GET", "model-report"):
            eng = STATE["engine"]
            report = eng.predictor.report if eng and eng.predictor else None
            return self._send(200, {"mode": eng.mode if eng else None, "report": report,
                                    "status": STATE["train_status"], "progress": STATE["train_progress"],
                                    "error": STATE["train_error"]})

        if route == ("POST", "train"):
            if STATE["train_status"] != "running":
                threading.Thread(target=run_training, daemon=True).start()
            return self._send(202, {"ok": True})

        if route == ("POST", "backtest"):
            if STATE["bt_status"] != "running":
                threading.Thread(target=run_backtest, daemon=True).start()
            return self._send(202, {"ok": True})

        self._send(404, {"error": f"unknown endpoint {method} /api/{'/'.join(parts)}"})


def main():
    from spm import envfile
    envfile.load()  # DATABASE_URL etc. from .env
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    url = f"http://{args.host}:{args.port}"
    # Windows lets two servers share a port (SO_REUSEADDR), so check for a running copy first.
    with socket.socket() as probe:
        probe.settimeout(0.5)
        in_use = probe.connect_ex((args.host, args.port)) == 0
    try:
        if in_use:
            raise OSError("port in use")
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError:
        print(f"Port {args.port} is already in use; opening the running app at {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return
    threading.Thread(target=load, daemon=True).start()
    print(f"NBA Prop Model running at {url}  (Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
