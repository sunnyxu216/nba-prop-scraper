"""Score open NFL lines with the trained model and log them for paper trading.

    python scripts/score_nfl.py

Run hourly by the "Score NFL" workflow (Supabase pg_cron, 20 minutes after collection).
Exits early, before loading the model, when no NFL lines are open, so off days cost seconds.
Needs DATABASE_URL, the trained models in models/nfl/ and the nflverse history (downloaded and
cached under data/cache/nfl/; the workflow keeps that cache between runs).
Exit codes: 0 = ok (including nothing to score), 1 = error.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spm import envfile  # noqa: E402
from spm.store import paper, pg  # noqa: E402


def main():
    envfile.load()
    t = time.time()
    with pg.connect() as conn:
        pg.migrate(conn)
        offers = paper.open_offers(conn, "nfl")
        if not offers:
            print("No open NFL lines: nothing to score.")
            return 0
        from spm.nfl import data, live    # heavy imports (numpy, lightgbm) only when there's work
        data.download_all(log=lambda m: None)
        results = live.Scorer().score(offers)
        logged, latest = paper.record(conn, offers, results, "nfl")
    scored = [r for r in results if not r.get("skipped")]
    bets = [r for r in scored if r["decision"] != "PASS"]
    print(f"Scored {len(scored)} of {len(offers)} open NFL lines ({len(offers) - len(scored)} skipped: "
          f"no history or no schedule match); {len(bets)} recommended bets; "
          f"{logged} checkpoint rows and {latest} board rows written in {time.time() - t:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
