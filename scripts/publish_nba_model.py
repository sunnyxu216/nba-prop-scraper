"""Upload the trained NBA model and its history to the database for the cloud scorer.

    python scripts/publish_nba_model.py          # pack data/models + data/cache history and upload
    python scripts/publish_nba_model.py --dry    # just report what would be uploaded

Run at home after training (train.py), or after the app has refreshed the season roster.
stats.nba.com blocks cloud servers, so this is how the hourly "Score NBA" job gets the
past seasons' logs, shot zones, play types, defense ratings and the model files. The bundle
is stored in the private `artifacts` table, not in the public repo. Unchanged files are
not re-uploaded. Needs DATABASE_URL (env var or .env).
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spm import envfile, nba_cloud  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    envfile.load()
    names = nba_cloud.bundle_paths()
    blob = nba_cloud.make_bundle()
    print(f"{len(names)} files, {len(blob) / 1e6:.1f} MB compressed")
    if args.dry:
        return 0
    from spm.store import artifacts, pg
    with pg.connect() as conn:
        pg.migrate(conn)
        before = artifacts.version_of(conn, nba_cloud.BUNDLE)
        version = artifacts.put(conn, nba_cloud.BUNDLE, blob)
    print(f"Bundle {version}: " + ("unchanged, nothing uploaded" if version == before else "uploaded"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
