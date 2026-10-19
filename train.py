"""Download the full history and train the ML models.

    python train.py

Takes a few minutes the first time (downloads shot-zone and play-type data for
every season since 2015-16), then writes models to data/models/.
"""

import time

from spm import advanced, data, features, ml


def main():
    t0 = time.time()
    ds = data.load_dataset()
    prof = advanced.load_profiles(data.FIRST_SEASON, ds.current_start)
    store = features.FeatureStore(ds, prof, build_training=True)
    print(f"Features built in {time.time() - t0:.0f}s. Training ...")
    ml.train(store)
    print(f"Done in {time.time() - t0:.0f}s. Models saved to {ml.MODEL_DIR}")


if __name__ == "__main__":
    main()
