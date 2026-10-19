"""Incremental grading: settle each closing line against the box score, once.

Each run only looks at games that started a few hours ago and still have
ungraded lines, so its cost grows with *new* games, not with history:

  1. pending_events(): finished games that have offers without a grade;
  2. load results (box scores) for those games' dates (upsert: safe to repeat);
  3. closing lines (last price before tip-off) not yet graded, in SQL;
  4. settle each: over / under / push, or void when the player didn't play.
     Games whose box scores aren't available yet are skipped and retried later.
"""

import datetime as dt

from ..model import STATS

FINISHED_AFTER = dt.timedelta(hours=3)   # tip-off + 3h: the game is over
ET_OFFSET = dt.timedelta(hours=5)        # UTC -> US Eastern *date* (safe for NBA tip times in EST and EDT)
FIELDS = ("pts", "reb", "ast", "fg3m", "stl", "blk", "tov")
GRADED_SPORTS = ("nba",)                 # sports with a box-score source and grader (health checks only these)


def et_date(start_time):
    """US Eastern calendar date of a tip-off (timestamptz)."""
    return (start_time.astimezone(dt.timezone.utc) - ET_OFFSET).date()


def market_value(market, stats):
    """Actual value of a prop market from a box-score row (dict with pts/reb/...)."""
    return sum(stats[f] for f in STATS[market][1])


def match_name(key, candidates):
    """Same last name and one first name a prefix of the other, if exactly one candidate fits.

    Deliberately strict: a loose fuzzy match would confuse e.g. Jalen and Jaylin Williams (same team).
    """
    first, _, last = key.partition(" ")
    hits = [c for c in candidates
            if c.rpartition(" ")[2] == last.rpartition(" ")[2] and c != key
            and (c.startswith(first) or first.startswith(c.partition(" ")[0]))]
    return hits[0] if len(hits) == 1 else None


def settle(line, actual):
    return "over" if actual > line else "under" if actual < line else "push"


def pending_events(conn, now=None, sport="nba"):
    """Finished games of one sport with at least one ungraded offer: [(event_id, start_time, home, away)]."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            select e.id, e.start_time, e.home_team, e.away_team
            from events e
            where e.sport = %s and e.start_time is not null and e.start_time < %s
              and exists (
                select 1 from offers o
                left join grades g on g.event_id = o.event_id and g.player_key = o.player_key
                                  and g.market = o.market and g.book = o.book
                where o.event_id = e.id and g.event_id is null and o.observed_at < e.start_time)
            order by e.start_time""", (sport, now - FINISHED_AFTER))
        return cur.fetchall()


def upsert_results(conn, rows):
    """rows: dicts with the results columns. Upsert keeps stat corrections current."""
    if not rows:
        return 0
    cols = ["sport", "game_date", "source_game_id", "player_key", "player_name", "team", "opponent",
            "minutes", *FIELDS]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            f"insert into results ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))}) "
            "on conflict (sport, source_game_id, player_key) do update set "
            + ", ".join(f"{c} = excluded.{c}" for c in cols[4:]) + ", loaded_at = now()",
            [tuple(r[c] for c in cols) for r in rows])
    return len(rows)


def _closing_lines(cur, event_ids):
    cur.execute("""
        select distinct on (o.event_id, o.player_key, o.market, o.book)
               o.event_id, o.player_key, o.market, o.book, o.sport, o.source, o.player_name,
               o.line, o.over_odds, o.under_odds, o.observed_at
        from offers o
        join events e on e.id = o.event_id
        left join grades g on g.event_id = o.event_id and g.player_key = o.player_key
                          and g.market = o.market and g.book = o.book
        where o.event_id = any(%s) and o.observed_at < e.start_time and g.event_id is null
        order by o.event_id, o.player_key, o.market, o.book, o.observed_at desc""", (event_ids,))
    return cur.fetchall()


def grade_events(conn, events):
    """Grade every ungraded closing line of these events whose box score is loaded.

    Returns (graded, voided, skipped_games).
    """
    graded = voided = 0
    skipped = []
    with conn.transaction(), conn.cursor() as cur:
        by_event = {}
        for event_id, start, home, away in events:
            cur.execute("""select player_key, pts, reb, ast, fg3m, stl, blk, tov from results
                           where sport = 'nba' and game_date = %s and team in (%s, %s)
                             and opponent in (%s, %s)""", (et_date(start), home, away, home, away))
            box = {r[0]: dict(zip(FIELDS, r[1:])) for r in cur.fetchall()}
            if box:
                by_event[event_id] = box
            else:
                skipped.append(event_id)   # box score not available yet: retry next run
        if not by_event:
            return 0, 0, skipped
        rows = []
        for (event_id, player_key, market, book, sport, source, name, line, over, under, observed) \
                in _closing_lines(cur, list(by_event)):
            box = by_event[event_id]
            stats = box.get(player_key)
            if stats is None:  # "nic claxton" vs "nicolas claxton" is a spelling difference, not a DNP
                alt = match_name(player_key, box)
                stats = box[alt] if alt else None
            if stats is None:
                actual, result = None, "void"     # didn't play: books void the bet
                voided += 1
            else:
                actual = market_value(market, stats)
                result = settle(float(line), actual)
                graded += 1
            rows.append((event_id, player_key, market, book, sport, source, name, line, over, under,
                         observed, actual, result))
        cur.executemany("""
            insert into grades (event_id, player_key, market, book, sport, source, player_name, line,
                                over_odds, under_odds, closing_observed_at, actual, result)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict do nothing""", rows)
    return graded, voided, skipped


def delete_grades_since(conn, since_date):
    """For stat corrections: drop grades of games on/after a date so the next run regrades them."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("""delete from grades g using events e
                       where e.id = g.event_id and e.start_time >= %s""", (since_date,))
        return cur.rowcount
