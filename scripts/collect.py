"""Standalone line collector: odds sources in, change-only offer history out.

Loads nothing but the odds adapters (no NBA stats, no models, no numpy), so it
runs anywhere with Python 3.10+, e.g. GitHub Actions. Player names stay as the
source gives them; matching them to NBA player ids happens later, at grading.

Settings (environment variables):
  ODDS_SOURCES    comma-separated adapters (default: every available one)
  SPORT           default "nba"
  DAYS_AHEAD      days of games to fetch, default 2
  STATE_CODE      Action Network state for book availability, default "NJ"
  REQUEST_DELAY   seconds between Action Network requests, default 0.25
  DATABASE_URL    Postgres (Supabase). When set, offers go to the database;
                  otherwise to daily CSV files in OUT_DIR
  OUT_DIR         CSV fallback location, default data/offers
  ODDS_API_KEY    only needed for the_odds_api

Storage either way is change-only: an offer is written only when it's new or its
line/price moved since it was last seen. In Postgres that comparison happens in
SQL inside one transaction (spm/store/pg.py); the CSV fallback keeps one file
per UTC day (OUT_DIR/YYYY-MM-DD.csv).

Exit codes: 0 = ok (including "no games"), 1 = every source failed.
"""

import csv
import datetime as dt
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spm import envfile, odds  # noqa: E402
from spm.odds import action_network  # noqa: E402

FIELDS = ["observed_at", "source", "sport", "event_id", "start_time", "home_team", "away_team",
          "player_name", "source_player_id", "team", "market", "book", "line", "over_odds", "under_odds"]
LOOKBACK_DAYS = 3  # how far back to look for an offer's previous price


def settings():
    env = os.environ.get
    return {
        "sport": env("SPORT", "nba"),
        "days": int(env("DAYS_AHEAD", "2")),
        "state": env("STATE_CODE", "NJ"),
        "delay": float(env("REQUEST_DELAY", "0.25")),
        "out_dir": env("OUT_DIR", os.path.join("data", "offers")),
    }


def offer_key(row):
    player = row["source_player_id"] or row["player_name"]
    return (row["source"], row["event_id"], player, row["market"], row["book"])


def price(row):
    return (str(float(row["line"])), str(int(row["over_odds"])), str(int(row["under_odds"])))


def last_prices(out_dir, today):
    """Latest known (line, over, under) per offer from the last few daily files."""
    known = {}
    for i in range(LOOKBACK_DAYS, -1, -1):
        path = os.path.join(out_dir, f"{(today - dt.timedelta(days=i)).isoformat()}.csv")
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    known[offer_key(row)] = price(row)
    return known


def record(offers, out_dir, today):
    """Append offers that are new or moved. Returns how many rows were written."""
    os.makedirs(out_dir, exist_ok=True)
    known = last_prices(out_dir, today)
    path = os.path.join(out_dir, f"{today.isoformat()}.csv")
    new_file = not os.path.exists(path)
    written = 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for o in offers:
            row = o.as_dict()
            k, p = offer_key(row), price(row)
            if known.get(k) != p:
                w.writerow(row)
                known[k] = p
                written += 1
    return written


def main():
    envfile.load()
    cfg = settings()
    sources = odds.enabled_sources()
    for s in sources:
        if isinstance(s, action_network.ActionNetwork):
            s.state, s.delay = cfg["state"], cfg["delay"]
    if not sources:
        print("No odds sources available (check ODDS_SOURCES / ODDS_API_KEY).")
        return 1

    offers, failed = [], 0
    for s in sources:
        try:
            got = s.fetch(cfg["sport"], days=cfg["days"])
        except Exception as e:
            failed += 1
            print(f"::error::{s.name} failed: {e}")
            continue
        offers += got
        print(f"{s.name}: {len(got)} offers")
        for err in s.errors[:10]:
            print(f"::warning::{s.name}: {err}")

    if os.environ.get("DATABASE_URL"):
        from spm.store import pg
        with pg.connect() as conn:
            written = pg.record(conn, offers)
        print(f"{written} new or moved offers written to Postgres")
    else:
        today = dt.datetime.now(dt.timezone.utc).date()
        written = record(offers, cfg["out_dir"], today)
        print(f"{written} new or moved offers written to {cfg['out_dir']}/{today.isoformat()}.csv")
    return 1 if failed == len(sources) else 0


if __name__ == "__main__":
    sys.exit(main())
