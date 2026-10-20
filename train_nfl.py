"""Download NFL history (nflverse) and train the NFL prop models.

    python train_nfl.py              # download/refresh data, train, save models/nfl/
    python train_nfl.py --no-save    # train and report without overwriting models/nfl/

Runs in the foreground on demand (a few minutes); nothing is scheduled.
"""

import argparse
import sys
import time

from spm.nfl import data, features, model


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    t = time.time()
    data.download_all()
    built, _ = features.build()
    model.train(built, save=not args.no_save)
    print(f"Done in {time.time() - t:.0f}s" + ("" if args.no_save else f"; models saved to {model.MODEL_DIR}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
