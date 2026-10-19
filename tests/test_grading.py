"""Grading (spm/collector.grade): closing line before tip-off, settled against the box score.

Uses a fake dataset and a temporary lines.csv. Snapshot times sit a full day
before or after tip-off so the tests pass in any time zone (the grader converts
UTC tip-off times to local time).
"""

import csv
from types import SimpleNamespace

import pytest

from spm import collector

TIP = "2026-10-21T00:00:00.000Z"   # 8pm ET on Oct 20
BEFORE, EARLIER, AFTER = "2026-10-19T12:00:00", "2026-10-19T09:00:00", "2026-10-22T12:00:00"


def game(date, **stats):
    base = dict(pts=0, reb=0, ast=0, fg3m=0, stl=0, blk=0, tov=0)
    return SimpleNamespace(date=date, **{**base, **stats})


@pytest.fixture
def ds():
    players = {
        1: SimpleNamespace(games=[game("2026-10-20", pts=25, reb=8, ast=5)]),
        2: SimpleNamespace(games=[]),  # didn't play
    }
    return SimpleNamespace(players=players)


@pytest.fixture
def lines_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "LINES_CSV", str(tmp_path / "lines.csv"))
    monkeypatch.setattr(collector, "GRADED_CSV", str(tmp_path / "graded.csv"))
    monkeypatch.setattr(collector, "_read_cache", lambda name: None)

    def write(rows):
        with open(collector.LINES_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=collector.FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({f: r.get(f, "") for f in collector.FIELDS})
    return write


def row(snapshot, line, stat="PTS", player_id=1, book="DraftKings"):
    return {"snapshot": snapshot, "source": "action_network", "game_date": "2026-10-20", "tipoff": TIP,
            "player": "P", "player_id": player_id, "team": "AAA", "opponent": "BBB", "home": 1,
            "stat": stat, "book": book, "line": line, "over_odds": -110, "under_odds": -110}


def graded():
    with open(collector.GRADED_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_uses_last_price_before_tipoff(ds, lines_csv):
    lines_csv([row(EARLIER, 23.5), row(BEFORE, 24.5), row(AFTER, 30.5)])
    summary = collector.grade(ds)
    out = graded()
    assert summary["graded"] == 1
    assert out[0]["line"] == "24.5"          # last pre-tip move, not the post-tip one
    assert out[0]["result"] == "over"        # 25 > 24.5


def test_under_and_push(ds, lines_csv):
    lines_csv([row(BEFORE, 9.5, stat="REB"), row(BEFORE, 5, stat="AST")])
    collector.grade(ds)
    by_stat = {r["stat"]: r for r in graded()}
    assert by_stat["REB"]["result"] == "under"   # 8 < 9.5
    assert by_stat["AST"]["result"] == "push"    # 5 == 5


def test_combo_stats_sum_fields(ds, lines_csv):
    lines_csv([row(BEFORE, 37.5, stat="PRA")])
    collector.grade(ds)
    assert graded()[0]["actual"] == "38"          # 25 + 8 + 5


def test_each_book_graded_separately(ds, lines_csv):
    lines_csv([row(BEFORE, 24.5, book="DraftKings"), row(BEFORE, 25.5, book="FanDuel")])
    collector.grade(ds)
    results = {r["book"]: r["result"] for r in graded()}
    assert results == {"DraftKings": "over", "FanDuel": "under"}


def test_player_without_a_game_is_not_graded(ds, lines_csv):
    lines_csv([row(BEFORE, 10.5, player_id=2)])
    summary = collector.grade(ds)
    assert summary["closing_lines"] == 1
    assert summary["graded"] == 0


def test_no_lines_file(ds, tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "LINES_CSV", str(tmp_path / "missing.csv"))
    assert collector.grade(ds)["graded"] == 0
