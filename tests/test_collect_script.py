"""Standalone collector storage (scripts/collect.py): change-only daily files."""

import csv
import datetime as dt
import importlib.util
from pathlib import Path

from spm.odds import Offer

spec = importlib.util.spec_from_file_location("collect", Path(__file__).parents[1] / "scripts" / "collect.py")
collect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect)

TODAY = dt.date(2026, 10, 20)


def offer(line=24.5, over=-110, under=-110, book="DraftKings", observed="2026-10-20T12:00:00Z"):
    return Offer(source="action_network", sport="nba", event_id="1", start_time="2026-10-20T23:00:00Z",
                 home_team="DET", away_team="BOS", player_name="Jayson Tatum", source_player_id="99",
                 team="BOS", market="PTS", book=book, line=line, over_odds=over, under_odds=under,
                 observed_at=observed)


def rows(out_dir, day=TODAY):
    with open(out_dir / f"{day.isoformat()}.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_first_sighting_written(tmp_path):
    assert collect.record([offer(), offer(book="FanDuel")], str(tmp_path), TODAY) == 2
    assert len(rows(tmp_path)) == 2


def test_unchanged_offer_not_rewritten(tmp_path):
    collect.record([offer()], str(tmp_path), TODAY)
    assert collect.record([offer(observed="2026-10-20T13:00:00Z")], str(tmp_path), TODAY) == 0
    assert len(rows(tmp_path)) == 1


def test_price_or_line_move_written(tmp_path):
    collect.record([offer()], str(tmp_path), TODAY)
    assert collect.record([offer(over=-120, under=100)], str(tmp_path), TODAY) == 1
    assert collect.record([offer(line=25.5, over=-120, under=100)], str(tmp_path), TODAY) == 1
    assert [r["line"] for r in rows(tmp_path)] == ["24.5", "24.5", "25.5"]


def test_previous_days_count_as_known(tmp_path):
    yesterday = TODAY - dt.timedelta(days=1)
    collect.record([offer()], str(tmp_path), yesterday)
    assert collect.record([offer()], str(tmp_path), TODAY) == 0


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("STATE_CODE", "PA")
    monkeypatch.setenv("DAYS_AHEAD", "1")
    monkeypatch.setenv("OUT_DIR", "x/y")
    cfg = collect.settings()
    assert (cfg["state"], cfg["days"], cfg["out_dir"]) == ("PA", {"nba": 1}, "x/y")


def test_sports_and_per_sport_days(monkeypatch):
    monkeypatch.setenv("SPORTS", "nba, nfl")
    monkeypatch.setenv("DAYS_AHEAD", "2")
    monkeypatch.setenv("DAYS_AHEAD_NFL", "6")
    assert collect.settings()["days"] == {"nba": 2, "nfl": 6}
    monkeypatch.delenv("DAYS_AHEAD")
    monkeypatch.delenv("DAYS_AHEAD_NFL")
    assert collect.settings()["days"] == {"nba": 2, "nfl": 7}      # built-in defaults


def test_one_sided_offer_in_csv(tmp_path):
    td = offer(line=0.5, over=250, under=None)
    td = type(td)(**{**td.as_dict(), "sport": "nfl", "market": "ANYTIME_TD"})
    assert collect.record([td], str(tmp_path), TODAY) == 1
    assert collect.record([td], str(tmp_path), TODAY) == 0          # re-read from CSV ("" under) still matches
    assert rows(tmp_path)[0]["under_odds"] == ""
