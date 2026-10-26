"""Pipeline health: detect *missing* success, not just errors.

A collector that silently stops running raises no error anywhere, so the
checks look at what the database shows actually happened:

* collection is stale: games are coming up but no line has been seen recently
  (offers_current.last_seen is refreshed by every collector run, even when no
  price moved, so it doubles as a heartbeat);
* a game was dropped: it starts soon and had lines, but the collector stopped refreshing them
  while still running for other games (the heartbeat alone can't see this);
* grading is behind: games that finished long ago still have ungraded lines.

Checked per sport, so one sport's healthy collector can't hide another's broken one.
"""

import datetime as dt

STALE_AFTER = dt.timedelta(hours=3)       # collector runs hourly; allow for GitHub delays
UPCOMING_WINDOW = dt.timedelta(hours=36)  # only expect fresh lines when games are this close
GRADING_GRACE = dt.timedelta(hours=36)    # daily grader + box-score delays
GAME_WINDOW = dt.timedelta(hours=12)      # games starting this soon must have fresh lines ...
GAME_STALE_AFTER = dt.timedelta(hours=2)  # ... seen within this long (two missed hourly runs)


def status(conn, now=None, sport=None, check_grading=True):
    """Health inputs for one sport (or all sports when sport is None).

    check_grading=False for sports without a grader yet: their lines are expected to stay ungraded.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    # "%s is null or sport = %s": one query for both "this sport" and "every sport"
    with conn.cursor() as cur:
        cur.execute("select max(last_seen) from offers_current where %s::text is null or sport = %s",
                    (sport, sport))
        last_seen = cur.fetchone()[0]
        cur.execute("select count(*) from events where start_time between %s and %s "
                    "and (%s::text is null or sport = %s)", (now, now + UPCOMING_WINDOW, sport, sport))
        upcoming = cur.fetchone()[0]
        cur.execute("""
            select e.away_team || ' @ ' || e.home_team from events e
            join offers_current oc on oc.event_id = e.id
            where e.start_time between %s and %s and (%s::text is null or e.sport = %s)
            group by e.id having max(oc.last_seen) < %s order by min(e.start_time)""",
                    (now, now + GAME_WINDOW, sport, sport, now - GAME_STALE_AFTER))
        stale_games = [r[0] for r in cur.fetchall()]
        ungraded = 0
        if check_grading:
            cur.execute("""
                select count(distinct e.id) from events e
                join offers o on o.event_id = e.id and o.observed_at < e.start_time
                left join grades g on g.event_id = o.event_id and g.player_key = o.player_key
                                  and g.market = o.market and g.book = o.book
                where e.start_time < %s and g.event_id is null
                  and (%s::text is null or e.sport = %s)""", (now - GRADING_GRACE, sport, sport))
            ungraded = cur.fetchone()[0]
    return {"now": now, "sport": sport, "last_seen": last_seen, "upcoming_games": upcoming,
            "stale_games": stale_games, "ungraded_old_games": ungraded}


SIZE_LIMIT_MB = 400        # of Supabase's 500 MB free tier: alert well before writes are blocked


def size_problems(conn):
    with conn.cursor() as cur:
        cur.execute("select pg_database_size(current_database())")
        mb = cur.fetchone()[0] / 1e6
    return [f"Database is {mb:.0f} MB of the 500 MB free tier (alert above {SIZE_LIMIT_MB} MB): "
            f"check the Archive workflow is deleting old rows."] if mb > SIZE_LIMIT_MB else []


def problems(s):
    """Human-readable failures for a status() dict (empty list = healthy)."""
    out = []
    if s["upcoming_games"] and (s["last_seen"] is None or s["now"] - s["last_seen"] > STALE_AFTER):
        age = "never" if s["last_seen"] is None else f"{(s['now'] - s['last_seen']).total_seconds() / 3600:.1f}h ago"
        out.append(f"Collection is stale: {s['upcoming_games']} games in the next "
                   f"{UPCOMING_WINDOW.total_seconds() / 3600:.0f}h, but lines were last seen {age}.")
    elif s.get("stale_games"):
        out.append(f"Games dropped by the collector: {', '.join(s['stale_games'])} start within "
                   f"{GAME_WINDOW.total_seconds() / 3600:.0f}h but their lines haven't been refreshed in "
                   f"{GAME_STALE_AFTER.total_seconds() / 3600:.0f}h.")
    if s["ungraded_old_games"]:
        out.append(f"Grading is behind: {s['ungraded_old_games']} games finished over "
                   f"{GRADING_GRACE.total_seconds() / 3600:.0f}h ago still have ungraded lines.")
    return out


def weekly_report(conn, now=None):
    """Markdown summary of the last 7 days."""
    now = now or dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=7)
    with conn.cursor() as cur:
        def one(sql, *args):
            cur.execute(sql, args)
            return cur.fetchone()[0]
        offers = one("select count(*) from offers where observed_at >= %s", since)
        games = one("select count(*) from events where start_time >= %s and start_time < %s", since, now)
        open_lines = one("select count(*) from offers_current where last_seen >= %s", now - dt.timedelta(hours=6))
        cur.execute("""select result, count(*) from grades where graded_at >= %s group by 1 order by 1""", (since,))
        grades = dict(cur.fetchall())
        cur.execute("""select book, count(*) from offers where observed_at >= %s group by 1 order by 2 desc""",
                    (since,))
        books = cur.fetchall()
        cur.execute("""select sport, count(*) from offers where observed_at >= %s group by 1 order by 1""", (since,))
        by_sport = cur.fetchall()
        licensed = one("select count(*) from offers_licensed where observed_at >= %s", since)
        size_mb = one("select pg_database_size(current_database())") / 1e6
    lines = [
        f"## Weekly health report ({since:%Y-%m-%d} to {now:%Y-%m-%d})", "",
        "| | |", "|---|---|",
        f"| Price changes recorded | {offers:,} "
        f"({', '.join(f'{s.upper()} {n:,}' for s, n in by_sport) or 'none'}) |",
        f"| Games played | {games} |",
        f"| Open lines right now | {open_lines:,} |",
        f"| Lines graded | {sum(grades.values()):,} "
        f"({', '.join(f'{k} {v:,}' for k, v in grades.items()) or 'none'}) |",
        f"| Books | {', '.join(f'{b} ({n:,})' for b, n in books) or 'none'} |",
        f"| From licensed sources | {licensed:,} of {offers:,} |",
        f"| Database size | {size_mb:.0f} MB of 500 MB free tier |",
    ]
    lines += _paper_section(conn)
    return "\n".join(lines)


def _paper_section(conn):
    """Paper-trading record per sport (each pick once, best book; full history from the archive),
    or nothing before migration 005."""
    from . import archive

    def fmt(r):
        return (f"{r['picks']} picks, {r['wins']}-{r['losses']}-{r['pushes']}, {r['units']:+.2f}u"
                + (f", ROI {r['roi']:+.1%}" if r["roi"] is not None else "")
                + (f", avg CLV {r['avg_clv']:+.1%}" if r["avg_clv"] is not None else ""))
    out = []
    for sport in ("nba", "nfl"):
        try:
            with conn.transaction():
                s = archive.paper_summary(conn, sport)
        except Exception:          # predictions table not created yet
            return []
        cp = s["checkpoints"]
        out += ["", f"### {sport.upper()} paper trading (all time; each pick counted once, at its best book)", "",
                f"- At close (last hour before start): {fmt(cp['close'])}",
                f"- 2 hours before: {fmt(cp['t2'])}",
                f"- When first posted: {fmt(cp['open'])}"]
        if s["track_only"]["picks"]:
            out.append(f"- Tracked only (never recommended): {fmt(s['track_only'])}")
    return out
