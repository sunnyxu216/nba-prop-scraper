"""Spike: can a GitHub-hosted runner reach Action Network's public API?

Makes the same requests the collector makes (scoreboard, then one props
market for one game) and reports status codes and how much data came back.
Standalone on purpose: no project imports, so it answers only this question.

Exit code 0 = reachable with real props data, 1 = blocked or empty.
"""

import datetime as dt
import gzip
import json
import sys
import urllib.error
import urllib.request

BASE = "https://api.actionnetwork.com/web"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Encoding": "gzip",
}


def get(path):
    req = urllib.request.Request(BASE + path, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300]
    except Exception as e:  # DNS, timeout, connection reset
        return None, str(e).encode()


def main():
    ok = False
    for offset in range(3):
        day = (dt.date.today() + dt.timedelta(days=offset)).strftime("%Y%m%d")
        status, body = get(f"/v1/scoreboard/nba?date={day}")
        print(f"scoreboard {day}: HTTP {status}, {len(body)} bytes")
        if status != 200:
            print("  body:", body[:300].decode(errors="replace"))
            continue
        games = [g for g in json.loads(body).get("games", []) if g.get("type") == "reg"]
        print(f"  regular-season games: {len(games)}")
        if not games:
            continue
        gid = games[0]["id"]
        status, body = get(f"/v2/games/{gid}/props?customPickType=core_bet_type_27_points&stateCode=NJ")
        print(f"props game {gid}: HTTP {status}, {len(body)} bytes")
        if status == 200:
            props = json.loads(body).get("player_props", {}).get("core_bet_type_27_points", [])
            print(f"  points props: {len(props)} players")
            ok = len(props) > 0
        else:
            print("  body:", body[:300].decode(errors="replace"))
        break
    print("RESULT:", "REACHABLE" if ok else "BLOCKED OR NO DATA")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
