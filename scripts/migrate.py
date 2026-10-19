"""Apply pending database migrations and sync the odds-source registry.

    python scripts/migrate.py

Needs DATABASE_URL (env var, or .env in the project root).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spm import envfile, odds  # noqa: E402
from spm.store import pg  # noqa: E402


def main():
    envfile.load()
    with pg.connect() as conn:
        applied = pg.migrate(conn)
        pg.sync_sources(conn, odds.registered())
    print("Applied:", ", ".join(applied) if applied else "nothing (up to date)")
    print("Sources:", ", ".join(f"{n} ({'licensed' if c.licensed else 'unlicensed'})"
                                for n, c in odds.registered().items()))


if __name__ == "__main__":
    main()
