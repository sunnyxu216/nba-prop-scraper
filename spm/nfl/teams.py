"""Canonical NFL team codes (nflverse's, since the training data uses them) and every
other spelling our sources use, so games from Action Network, The Odds API, ESPN and
nflverse line up when grading.
"""

TEAMS = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LA": "Los Angeles Rams", "LAC": "Los Angeles Chargers",
    "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks", "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}

# Other sources' codes -> ours. Action Network: JAC; ESPN: JAX, LAR, WSH.
ALIASES = {"JAC": "JAX", "LAR": "LA", "WSH": "WAS"}
NAME_TO_CODE = {v: k for k, v in TEAMS.items()}


def code(abbr):
    """Any source's team abbreviation -> canonical code ('' if unknown)."""
    abbr = (abbr or "").strip().upper()
    abbr = ALIASES.get(abbr, abbr)
    return abbr if abbr in TEAMS else ""
