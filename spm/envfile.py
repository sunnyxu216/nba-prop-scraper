"""Load KEY=value lines from a .env file into os.environ (without overriding real env vars).

Local secrets (DATABASE_URL, ODDS_API_KEY) live in .env, which is gitignored; in
GitHub Actions the same names come from repository secrets instead.
"""

import os
from pathlib import Path

from .data import ROOT as _ROOT  # project folder, or the .exe's folder when packaged

ROOT = Path(_ROOT)


def load(path=ROOT / ".env"):
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
