"""Cloud NBA scoring inputs (spm/nba_cloud.py): this season's logs from ESPN results, the bundle."""

import io
import json
import tarfile

import pytest

from spm import nba_cloud

# [date, game_id, player_key, name, team, opp, minutes, pts, reb, ast, fg3m, stl, blk, tov]
RESULTS = [
    ["2026-10-21", "espn:1", "lamelo ball", "LaMelo Ball", "MIN", "MIA", 34.0, 22, 6, 9, 3, 1, 0, 4],
    ["2026-10-21", "espn:1", "bam adebayo", "Bam Adebayo", "MIA", "MIN", 33.0, 18, 11, 4, 0, 1, 1, 2],
    ["2026-10-20", "espn:0", "nic claxton", "Nic Claxton", "BKN", "CHA", 28.0, 10, 8, 2, 0, 0, 2, 1],
    ["2026-10-23", "espn:2", "rookie guy", "Rookie Guy", "MIA", "BOS", 12.0, 4, 2, 1, 0, 0, 0, 1],
]
INDEX = {"lamelo ball": 1630163, "bam adebayo": 1628389, "nicolas claxton": 1629651}
HOME = {"2026-10-21|MIA", "2026-10-20|BKN"}


def test_logs_from_results():
    logs = nba_cloud.current_season_logs(RESULTS, HOME, INDEX)
    assert [r[4] for r in logs] == ["2026-10-20", "2026-10-21", "2026-10-21", "2026-10-23"]   # by date
    by_name = {r[1]: r for r in logs}
    assert by_name["LaMelo Ball"][:8] == [1630163, "LaMelo Ball", "MIN", "espn:1", "2026-10-21", "MIA", 0, 34.0]
    assert by_name["Bam Adebayo"][6] == 1                       # home team
    assert by_name["LaMelo Ball"][8:] == [22, 6, 9, 3, 1, 0, 4]
    assert by_name["Nic Claxton"][0] == 1629651                 # spelling difference resolved
    assert by_name["Rookie Guy"][0] < 0                         # no history: stable synthetic id
    assert by_name["Rookie Guy"][0] == nba_cloud.synthetic_id("rookie guy")


def test_logs_before_date_for_backfills():
    logs = nba_cloud.current_season_logs(RESULTS, HOME, INDEX, before="2026-10-21")
    assert [r[1] for r in logs] == ["Nic Claxton"]


def test_roster_follows_trades_and_adds_new_players():
    roster = [{"id": 1630163, "name": "LaMelo Ball", "team": "CHA", "from_year": 2020},
              {"id": 1, "name": "Bench Guy", "team": "BOS", "from_year": 2019}]
    logs = nba_cloud.current_season_logs(RESULTS, HOME, INDEX)
    out = {p["name"]: p for p in nba_cloud.refresh_roster(roster, logs)}
    assert out["LaMelo Ball"]["team"] == "MIN" and out["LaMelo Ball"]["from_year"] == 2020
    assert out["Bench Guy"]["team"] == "BOS"                    # no games yet: snapshot team kept
    assert out["Rookie Guy"]["team"] == "MIA"


def test_b2b_teams():
    logs = nba_cloud.current_season_logs(RESULTS, HOME, INDEX)
    assert nba_cloud.b2b_teams(logs, "2026-10-22") == {"MIN", "MIA"}
    assert nba_cloud.b2b_teams(logs, "2026-10-23") == set()


def make_data(tmp_path, current=2026):
    (tmp_path / "cache").mkdir(parents=True)
    for s in range(2015, current):
        for kind in ("logs", "advanced", "defense"):
            (tmp_path / "cache" / f"{kind}_{s}.json").write_text(json.dumps({"s": s}))
    (tmp_path / "cache" / "debut_years.json").write_text("{}")
    (tmp_path / "cache" / f"roster_{current}.json").write_text("{}")
    (tmp_path / "cache" / "injuries.json").write_text("{}")           # not part of the bundle
    (tmp_path / "models").mkdir()
    for f in ("PTS.txt", "report.json"):
        (tmp_path / "models" / f).write_text(f)
    return tmp_path


def test_bundle_round_trip(tmp_path):
    src = make_data(tmp_path / "home")
    blob = nba_cloud.make_bundle(str(src), current=2026)
    assert blob == nba_cloud.make_bundle(str(src), current=2026)   # deterministic: same files, same version
    dest = tmp_path / "cloud"
    nba_cloud.install_bundle(blob, "v1", str(dest))
    assert (dest / "models" / "PTS.txt").read_text() == "PTS.txt"
    assert json.loads((dest / "cache" / "logs_2025.json").read_text()) == {"s": 2025}
    assert not (dest / "cache" / "injuries.json").exists()
    assert nba_cloud.installed_version(str(dest)) == "v1"


def test_bundle_rejects_paths_outside_data(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("cache/../../evil.py")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError):
        nba_cloud.install_bundle(buf.getvalue(), "v", str(tmp_path))


def test_missing_history_is_reported(tmp_path):
    src = make_data(tmp_path)
    (src / "cache" / "logs_2019.json").unlink()
    with pytest.raises(FileNotFoundError, match="logs_2019"):
        nba_cloud.bundle_paths(str(src), current=2026)
