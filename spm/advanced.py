"""Shot-zone and play-type profiles from stats.nba.com.

* Shot zones: where each player shoots from (and how well), and what each defense
  allows from each zone.
* Play types (Synergy tracking, 2015-16 onward): how each player's possessions
  are used (isolation, pick-and-roll, spot-up, ...) and how many points per
  possession each defense allows on each type.

Profiles are season-level. The model only ever uses the *previous* season's
profiles, so training never peeks at the season being predicted.
"""

import time
from collections import defaultdict

from .data import TEAMS, _http_json, _read_cache, _write_cache, season_label

ZONES = ("RA", "PAINT", "MID", "C3", "ATB3")
ZONE_LABELS = {"RA": "at the rim", "PAINT": "in the paint", "MID": "from midrange",
               "C3": "from the corners", "ATB3": "from above-the-break 3"}
ZONE_VALUE = {"RA": 2, "PAINT": 2, "MID": 2, "C3": 3, "ATB3": 3}

PLAY_TYPES = ("Isolation", "Transition", "PRBallHandler", "PRRollman", "Postup", "Spotup",
              "Handoff", "Cut", "OffScreen", "OffRebound", "Misc")
PLAY_LABELS = {"Isolation": "isolation", "Transition": "transition", "PRBallHandler": "pick-and-roll ball handler",
               "PRRollman": "pick-and-roll roll man", "Postup": "post-ups", "Spotup": "spot-ups",
               "Handoff": "handoffs", "Cut": "cuts", "OffScreen": "off-screen", "OffRebound": "putbacks",
               "Misc": "miscellaneous"}

FIRST_PLAYTYPE_SEASON = 2015
NAME_TO_ABBR = {v: k for k, v in TEAMS.items()}
NAME_TO_ABBR["Los Angeles Clippers"] = "LAC"

_SHOT_FILTERS = {
    "LeagueID": "00", "SeasonType": "Regular Season", "PerMode": "Totals", "DistanceRange": "By Zone",
    "DateFrom": "", "DateTo": "", "GameScope": "", "GameSegment": "", "LastNGames": 0, "Location": "",
    "Month": 0, "OpponentTeamID": 0, "Outcome": "", "PORound": 0, "PaceAdjust": "N", "Period": 0,
    "PlayerExperience": "", "PlayerPosition": "", "PlusMinus": "N", "Rank": "N", "SeasonSegment": "",
    "StarterBench": "", "TeamID": 0, "VsConference": "", "VsDivision": "", "Conference": "",
    "Division": "", "ShotClockRange": "",
}
# Column groups in the shot-location response, in order.
_SHOT_GROUPS = ("RA", "PAINT", "MID", "LC3", "RC3", "ATB3", "BACK", "C3")


def _zone_triplets(row, skip):
    vals = {}
    for i, g in enumerate(_SHOT_GROUPS):
        fgm, fga = row[skip + 3 * i], row[skip + 3 * i + 1]
        vals[g] = (float(fgm or 0), float(fga or 0))
    return {z: vals[z] for z in ZONES}


def fetch_shot_zones(start):
    season = season_label(start)
    rs = _http_json("https://stats.nba.com/stats/leaguedashplayershotlocations",
                    {**_SHOT_FILTERS, "Season": season, "MeasureType": "Base", "PlayerOrTeam": "Player",
                     "College": "", "Country": "", "DraftPick": "", "DraftYear": "", "Height": "", "Weight": ""}
                    )["resultSets"]["rowSet"]
    players = {str(r[0]): _zone_triplets(r, 6) for r in rs}
    rs = _http_json("https://stats.nba.com/stats/leaguedashteamshotlocations",
                    {**_SHOT_FILTERS, "Season": season, "MeasureType": "Opponent"})["resultSets"]["rowSet"]
    teams = {}
    for r in rs:
        abbr = NAME_TO_ABBR.get(r[1])
        if abbr:
            teams[abbr] = _zone_triplets(r, 2)
    return {"players": players, "teams": teams}


def fetch_play_types(start, log=print):
    season = season_label(start)
    out = {"players": defaultdict(dict), "teams": defaultdict(dict)}
    for pt in PLAY_TYPES:
        for who, grouping in (("P", "offensive"), ("T", "defensive")):
            d = _http_json("https://stats.nba.com/stats/synergyplaytypes", {
                "LeagueID": "00", "PerMode": "Totals", "PlayType": pt, "PlayerOrTeam": who,
                "SeasonType": "Regular Season", "SeasonYear": season, "TypeGrouping": grouping})
            rs = d["resultSets"][0]
            ix = {h: i for i, h in enumerate(rs["headers"])}
            for r in rs["rowSet"]:
                key = str(r[ix["PLAYER_ID"]]) if who == "P" else r[ix["TEAM_ABBREVIATION"]]
                bucket = out["players" if who == "P" else "teams"][key]
                poss, pts = bucket.get(pt, (0.0, 0.0))
                # traded players appear once per team: sum them
                bucket[pt] = (poss + float(r[ix["POSS"]] or 0), pts + float(r[ix["PTS"]] or 0))
            time.sleep(0.4)
    return {"players": dict(out["players"]), "teams": dict(out["teams"])}


def load_season(start, current_start, log=print):
    """Shot zones + play types for one season (cached; completed seasons never refetched)."""
    name = f"advanced_{start}.json"
    cached = _read_cache(name)
    if cached and (start < current_start or time.time() - cached["fetched"] < 12 * 3600):
        return cached
    log(f"Downloading {season_label(start)} shot zones and play types ...")
    try:
        payload = {"fetched": time.time(), "zones": fetch_shot_zones(start),
                   "playtypes": fetch_play_types(start, log) if start >= FIRST_PLAYTYPE_SEASON else None}
    except Exception as e:
        if cached:
            log(f"  failed ({e}); using cached copy")
            return cached
        log(f"  unavailable ({e})")
        return None
    _write_cache(name, payload)
    return payload


# --------------------------------------------------------------------------- profiles

class Profiles:
    """Per-season player and defense profiles, ready for feature building."""

    def __init__(self, seasons, defense=None):
        self.defense = defense or {}
        self.player_zone, self.team_zone, self.league_zone = {}, {}, {}
        self.player_pt, self.team_pt, self.league_pt = {}, {}, {}
        for start, payload in seasons.items():
            if not payload:
                continue
            self._zones(start, payload["zones"])
            if payload.get("playtypes"):
                self._playtypes(start, payload["playtypes"])

    def _zones(self, start, z):
        def profile(trip):
            fga = sum(trip[k][1] for k in ZONES)
            if fga <= 0:
                return None
            return {k: {"share": trip[k][1] / fga,
                        "pct": trip[k][0] / trip[k][1] if trip[k][1] else None,
                        "fga": trip[k][1]} for k in ZONES} | {"_fga": fga}

        self.player_zone[start] = {int(pid): p for pid, t in z["players"].items() if (p := profile(t))}
        self.team_zone[start] = {abbr: p for abbr, t in z["teams"].items() if (p := profile(t))}
        tot = {k: [sum(t[k][0] for t in z["teams"].values()), sum(t[k][1] for t in z["teams"].values())]
               for k in ZONES}
        self.league_zone[start] = profile({k: tuple(v) for k, v in tot.items()})

    def _playtypes(self, start, p):
        def profile(types):
            poss = sum(v[0] for v in types.values())
            if poss <= 0:
                return None
            return {t: {"share": types.get(t, (0, 0))[0] / poss,
                        "ppp": types[t][1] / types[t][0] if types.get(t, (0, 0))[0] else None}
                    for t in PLAY_TYPES} | {"_poss": poss}

        self.player_pt[start] = {int(pid): pr for pid, t in p["players"].items() if (pr := profile(t))}
        self.team_pt[start] = {abbr: pr for abbr, t in p["teams"].items() if (pr := profile(t))}
        tot = defaultdict(lambda: [0.0, 0.0])
        for t in p["teams"].values():
            for k, (poss, pts) in t.items():
                tot[k][0] += poss
                tot[k][1] += pts
        self.league_pt[start] = profile({k: tuple(v) for k, v in tot.items()})

    # ----------------------------------------------------------------- matchup features

    def zone_features(self, pid, opp, season):
        """Features for a game in `season`, using season-1 profiles."""
        s = season - 1
        p = self.player_zone.get(s, {}).get(pid)
        lg = self.league_zone.get(s)
        out = {"three_share": None, "rim_share": None, "zone_fg_edge": None, "zone_freq_edge": None,
               "opp_3p_delta": None, "opp_3pa_ratio": None, "opp_rim_delta": None}
        if p:
            out["three_share"] = p["C3"]["share"] + p["ATB3"]["share"]
            out["rim_share"] = p["RA"]["share"]
        o = self.team_zone.get(s, {}).get(opp) if opp else None
        if o and lg:
            o3 = (o["C3"]["pct"] * o["C3"]["fga"] + o["ATB3"]["pct"] * o["ATB3"]["fga"]) / (o["C3"]["fga"] + o["ATB3"]["fga"])
            l3 = (lg["C3"]["pct"] * lg["C3"]["fga"] + lg["ATB3"]["pct"] * lg["ATB3"]["fga"]) / (lg["C3"]["fga"] + lg["ATB3"]["fga"])
            out["opp_3p_delta"] = o3 - l3
            out["opp_3pa_ratio"] = (o["C3"]["share"] + o["ATB3"]["share"]) / (lg["C3"]["share"] + lg["ATB3"]["share"])
            out["opp_rim_delta"] = o["RA"]["pct"] - lg["RA"]["pct"]
            if p:
                out["zone_fg_edge"] = sum(p[z]["share"] * ((o[z]["pct"] or 0) - (lg[z]["pct"] or 0)) * ZONE_VALUE[z]
                                          for z in ZONES)
                out["zone_freq_edge"] = sum(p[z]["share"] * (o[z]["share"] / lg[z]["share"] - 1)
                                            for z in ZONES if lg[z]["share"])
        elif not opp and p:
            out.update(zone_fg_edge=0.0, zone_freq_edge=0.0, opp_3p_delta=0.0, opp_3pa_ratio=1.0, opp_rim_delta=0.0)
        return out

    def playtype_features(self, pid, opp, season):
        s = season - 1
        p = self.player_pt.get(s, {}).get(pid)
        lg = self.league_pt.get(s)
        out = {"pt_iso": None, "pt_prbh": None, "pt_spot": None, "pt_trans": None, "pt_post": None,
               "pt_roll": None, "pt_eff": None, "pt_ppp_edge": None, "pt_freq_edge": None}
        if p and lg:
            out.update(pt_iso=p["Isolation"]["share"], pt_prbh=p["PRBallHandler"]["share"],
                       pt_spot=p["Spotup"]["share"], pt_trans=p["Transition"]["share"],
                       pt_post=p["Postup"]["share"], pt_roll=p["PRRollman"]["share"])
            out["pt_eff"] = sum(p[t]["share"] * ((p[t]["ppp"] or 0) - (lg[t]["ppp"] or 0))
                                for t in PLAY_TYPES if p[t]["ppp"] is not None)
        o = self.team_pt.get(s, {}).get(opp) if opp else None
        if p and o and lg:
            out["pt_ppp_edge"] = sum(p[t]["share"] * ((o[t]["ppp"] or lg[t]["ppp"] or 0) - (lg[t]["ppp"] or 0))
                                     for t in PLAY_TYPES)
            out["pt_freq_edge"] = sum(p[t]["share"] * (o[t]["share"] / lg[t]["share"] - 1)
                                      for t in PLAY_TYPES if lg[t]["share"])
        elif p and not opp:
            out.update(pt_ppp_edge=0.0, pt_freq_edge=0.0)
        return out

    # ----------------------------------------------------------------- explanations

    def describe_zone_matchup(self, pid, opp, season):
        s = season - 1
        p = self.player_zone.get(s, {}).get(pid)
        o = self.team_zone.get(s, {}).get(opp)
        lg = self.league_zone.get(s)
        if not (p and o and lg):
            return None
        top = max(ZONES, key=lambda z: p[z]["share"])
        delta = (o[top]["pct"] or 0) - (lg[top]["pct"] or 0)
        rank = sorted(self.team_zone[s], key=lambda t: self.team_zone[s][t][top]["pct"] or 0).index(opp) + 1
        return {"zone": top, "label": ZONE_LABELS[top], "share": p[top]["share"],
                "player_pct": p[top]["pct"], "opp_pct": o[top]["pct"], "league_pct": lg[top]["pct"],
                "delta": delta, "rank": rank, "teams": len(self.team_zone[s]), "season": season_label(s)}

    def describe_playtype_matchup(self, pid, opp, season):
        s = season - 1
        p = self.player_pt.get(s, {}).get(pid)
        o = self.team_pt.get(s, {}).get(opp)
        lg = self.league_pt.get(s)
        if not (p and o and lg):
            return None
        top = max(PLAY_TYPES, key=lambda t: p[t]["share"])
        teams = self.team_pt[s]
        rank = sorted(teams, key=lambda t: teams[t][top]["ppp"] if teams[t][top]["ppp"] is not None else 9).index(opp) + 1
        return {"type": top, "label": PLAY_LABELS[top], "share": p[top]["share"],
                "player_ppp": p[top]["ppp"], "opp_ppp": o[top]["ppp"], "league_ppp": lg[top]["ppp"],
                "rank": rank, "teams": len(teams), "season": season_label(s)}


def load_profiles(first, current_start, log=print):
    from .lineups import load_defense
    seasons = {s: load_season(s, current_start, log) for s in range(first, current_start)}
    return Profiles(seasons, load_defense(first, current_start, log))
