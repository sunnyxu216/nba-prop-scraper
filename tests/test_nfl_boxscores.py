"""NFL box scores from ESPN (spm/nfl/boxscores.py) against a recorded real game.

Fixture: JAX @ HOU, 2026-10-18 (London), trimmed to what the parser reads: the
summary's box score, both teams' game rosters (who played) and the ESPN id -> full
name map from the team rosters.
"""

import datetime as dt
import json
from pathlib import Path

import pytest

from spm.nfl import boxscores as bx

FIXTURES = Path(__file__).parent / "fixtures"
DAY = dt.date(2026, 10, 18)


@pytest.fixture(scope="module")
def game():
    return json.loads((FIXTURES / "espn_nfl_game.json").read_text())


@pytest.fixture(scope="module")
def rows(game):
    return {r["player_name"]: r for r in bx.parse_game(game["summary"], game["game_rosters"], game["names"], DAY)}


def stats(r):
    return {k: v for k, v in r.items() if k in bx.STAT_FIELDS and v}


def test_quarterback_line(rows):
    r = rows["Trevor Lawrence"]
    assert (r["team"], r["opponent"], r["game_date"], r["source_game_id"]) == ("JAX", "HOU", DAY, "espn:401872996")
    assert stats(r) == {"pass_att": 41, "pass_cmp": 24, "pass_yds": 306, "pass_td": 4, "pass_int": 1,
                        "rush_att": 3, "rush_yds": 11}


def test_receiver_and_mixed_usage(rows):
    assert stats(rows["Jaylin Noel"]) == {"rec": 4, "rec_yds": 107, "rec_td": 1, "targets": 4}
    assert stats(rows["Nico Collins"]) == {"rush_att": 1, "rush_yds": 5, "rec": 3, "rec_yds": 74, "targets": 5}


def test_receiving_tds_match_passing_tds(rows):
    for team in ("JAX", "HOU"):
        team_rows = [r for r in rows.values() if r["team"] == team]
        assert sum(r["rec_td"] for r in team_rows) == sum(r["pass_td"] for r in team_rows)


def test_played_without_stats_is_a_zero_row(game, rows):
    """Played (game roster) but in no box-score category: graded as 0, not void."""
    in_box = {str(a["athlete"]["id"]) for side in game["summary"]["boxscore"]["players"]
              for c in side["statistics"] for a in c["athletes"]}
    quiet = [game["names"][str(e["playerId"])] for r in game["game_rosters"].values() for e in r["entries"]
             if not e["didNotPlay"] and str(e["playerId"]) not in in_box and str(e["playerId"]) in game["names"]]
    assert quiet and all(rows[n] and not stats(rows[n]) for n in quiet)


def test_did_not_play_has_no_row(rows):
    assert "David Montgomery" not in rows          # HOU, didNotPlay: bets on him are void


def test_player_keys_normalized(rows):
    assert rows["C.J. Stroud"]["player_key"] == "cj stroud"
