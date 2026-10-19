# NBA Prop Model

A local web app that projects NBA player stats for the 2026-27 season, prices player-prop lines, and tells you
whether each one is a bet (with expected value, a suggested stake and the reasoning behind it).

## Run it

Double-click **`run.bat`** (or `./run.sh` on macOS/Linux). The first run creates a `.venv` virtual environment,
installs `numpy` and `lightgbm` into it, downloads every regular-season box score since 2015-16 plus current
rosters from stats.nba.com, and opens http://127.0.0.1:8000.

Then go to the **Model** tab and click **Train model** (or run `python train.py`). The first time, this downloads
shot-zone and play-type history for every season (~5 minutes) and trains the models (~5 minutes). Until a model
is trained, the app uses the simpler formula model.

```
run.bat --port 9000        # different port
run.bat --no-browser       # don't open a browser tab
```

Requires Python 3.10+.

### Standalone executable (Windows)

**`NBA Prop Model.exe`** runs the app without Python installed. Double-click it; a console window shows the
server log (close it to stop the app) and the browser opens automatically. Launching it again while it's running
just reopens the browser tab. It keeps `data/` (your props and the download cache) and `config.json` in the same
folder as the `.exe`, so move them together.

After changing the code, rebuild it with **`build_exe.bat`**, which installs PyInstaller into `.venv`.

## Using it

**Props tab**: the board. Click **+ Add prop** and enter the player, stat, line, both odds, the opponent and
home/away; a live preview shows the call before you save. Click any row for the full reasoning, the EV of both
sides, the suggested stake and the last 10 games against the line. On first run the board is seeded with
*example* lines (last-season averages at −110). Clear them once you add real lines.

You can also edit `data/props.csv` directly in Excel:

| column | example |
|---|---|
| player | Jalen Brunson |
| stat | PTS, REB, AST, 3PM, STL, BLK, TOV, PRA, PR, PA, RA, SB (steals+blocks) |
| line | 25.5 |
| over_odds / under_odds | -115 / -105 |
| opponent | BOS (blank = unknown) |
| home | home / away / blank |
| b2b | yes if second night of a back-to-back |

**Settings** (on the Props tab): minimum EV to bet (default 3%), market blend (35%), bankroll, Kelly fraction (¼)
and **My books**. When the same player/stat/game has lines from several books, they're grouped into one row; every
book is evaluated, and the recommendation uses the best price among the books you've ticked. The expanded row has
a line-shopping table comparing all of them.

**Projections tab**: per-game 2026-27 projections for every rostered player; click one for per-stat reasoning
and season history.

**Model tab**: data status, train/retrain, the held-out scorecard for every stat, what the model relies on,
calibration, and the older formula-model backtest.

### Live lines (free)

**Import lines** on the Props tab pulls every player prop for today's and tomorrow's games from Action Network's
public feed (DraftKings, FanDuel, Caesars, Fanatics, BetRivers, Parx; NJ availability), with opponents and
home/away filled in. No key needed. This feed is unofficial, so it can change or stop without notice; if it does,
the raw responses saved in `data/lines/raw/` show what changed.

### Collecting lines to test for profit

Every line is saved to `data/lines/lines.csv` (one row each time a line or price is new or moves) and graded against
the box score after the game (`data/lines/graded.csv`, using the last price before tip-off). This builds the
real-line history needed to test whether the model actually beats the books.

Collection runs:
- every hour via the Windows scheduled task **"NBA Prop Model - collect lines"** (runs `collect_lines.py` with the
  `.venv` Python; log in `data/lines/collector.log`). The PC has to be on and logged in.
  To stop it: Task Scheduler → delete that task, or `schtasks /Delete /TN "NBA Prop Model - collect lines" /F`.
- every 3 hours while the app is open, and on **Snapshot now** in the Model tab.

Lines are stored in `data/lines/` (about 130 MB per season). Raw feed copies are kept for 3 days; the lines
themselves are kept forever.

### The Odds API (optional second source)

Get a key from [The Odds API](https://the-odds-api.com), copy `config.example.json` to `config.json`, and
paste the key in (or set `ODDS_API_KEY`). Imports and snapshots then also pull props from every book in `regions`.
Restrict books with `"bookmakers": ["draftkings", "fanduel"]`.

Cost: 1 credit per market returned, per region, per game, so about 5 credits per game for the default five markets.
The free plan (500 credits/month) covers ~12 full slates; the $30 plan (20K) covers several refreshes a day.
`max_events` caps how many games one import pulls.

## Cloud pipeline (setup from scratch)

Line collection, grading and health checks run on GitHub Actions and store everything in Postgres
(Supabase), so no PC needs to be on.

1. **Supabase:** create a free project (Data API off, automatic RLS on). Copy **Connect -> Session pooler**
   (port 5432; the direct connection is IPv6-only and GitHub runners can't reach it).
2. **Local:** copy `.env.example` to `.env` and set `DATABASE_URL=` to that string with the password
   filled in (no brackets). `.env` is gitignored.
3. **Database:** `python scripts/migrate.py` creates the tables (`migrations/*.sql`, applied once each).
4. **GitHub:** repo Settings -> Secrets and variables -> Actions -> New repository secret
   `DATABASE_URL` = the same string (value only, without `DATABASE_URL=`).
5. **History (optional):** `python scripts/import_lines.py` imports older CSV line history.
6. **Scheduler:** GitHub's own cron never fired for this repo, so Supabase triggers the workflows.
   Create a fine-grained GitHub token for this repo only with **Actions: Read and write**, put it in
   `.env` as `GITHUB_DISPATCH_TOKEN=`, then run `python scripts/setup_scheduler.py`. It enables
   `pg_cron` + `pg_net`, stores the token in Supabase Vault and schedules the three jobs below
   (`--status` shows recent runs and GitHub's responses; `--test` dispatches Collect once).
   The token expires: renew it and re-run the script before then.
7. **Workflows** (Actions tab, dispatch-only; times are when Supabase triggers them, UTC):
   - **Collect lines**, hourly at :17: `scripts/collect.py` (Action Network -> `offers`, change-only)
   - **Grade lines**, 10:00 daily: `scripts/grade.py` (ESPN box scores -> `grades`, incremental)
   - **Health**, 15:00 daily: fails (emails you) if collection is stale or grading is behind; Mondays
     posts a report to the "Health report" issue
   - **Tests**, every push: pytest, with a throwaway Postgres for the database tests

Useful queries in Supabase's SQL editor:

```sql
select * from offers_current order by last_seen desc limit 50;          -- latest prices
select result, count(*) from grades group by 1;                          -- grading so far
select * from grades_licensed;                                            -- only licensed sources (for anything commercial)
```

## How the model works

**Machine-learning model** (used once trained): one LightGBM model per stat (PTS, REB, AST, 3PM, STL, BLK, TOV
and the combos), trained on ~246,000 player-games since 2016-17. Every row is built only from what was known
before tip-off:

- recency-weighted per-minute rates and per-game averages (half-lives of 10/25/40/100 games), season-to-date
  average, game-to-game volatility, minutes trend;
- rest days, back-to-backs, games missed recently (injury returns), point in the season, experience, games with
  current team;
- **who's playing**: which of the player's regular teammates are out and how much scoring/rebounding/playmaking
  that frees up, and which opponents are out and how much defensive value (defensive win shares) that removes.
  History uses who actually played; live uses the ESPN injury report, weighting each listed player by how likely
  they are to sit (Out 100%, Doubtful 85%, Questionable 50%, Day-To-Day 35%);
- what the opponent had allowed in that stat *as of that date*, blended with its previous season;
- **shot-zone matchup**: the player's shot mix (rim, paint, midrange, corner 3, above-the-break 3) against what
  the opponent allowed from each zone, using previous-season profiles;
- **play-type matchup**: the player's possession mix (isolation, pick-and-roll, spot-up, transition, post-up, ...)
  against the opponent's points per possession allowed on each, using previous-season Synergy data.

Walk-forward protocol: tuned on 2023-24, calibrated on 2024-25, scored **once** on held-out 2025-26, then
retrained on everything for live use. The outcome distribution is negative binomial around the projection, and
probabilities are recalibrated per stat. Reasoning shows each situational factor's effect (from SHAP values).

**Formula model** (fallback): recency-weighted per-minute rate × projected minutes × experience curve × matchup
× venue, with the same distribution, calibration and EV logic.

**EV and decision**: blend model and no-vig market probabilities; EV = p(win) × payout − p(lose). Bet only if EV
clears the threshold (raised 75% for low-confidence spots: new team, small sample). Stakes are ¼ Kelly, capped at 3%.

### Results on the held-out 2025-26 season (players projected for 20+ minutes)

| | PTS | REB | AST | 3PM | PRA | BLK |
|---|---|---|---|---|---|---|
| Avg miss, ML | **5.04** | **1.95** | **1.53** | **1.03** | **6.31** | 0.58 |
| Avg miss, formula | 5.25 | 1.98 | 1.57 | 1.03 | 6.64 | 0.58 |
| Avg miss, season average | 5.25 | 1.99 | 1.57 | 1.04 | 6.67 | 0.57 |

The ML model beats the formula on 11 of 12 stats (ties on blocks), by about 1–5%; the lineup ("who's playing")
features account for about half of that gain. A year-by-year test (Model tab) shows the same edge in every season
since 2018-19.

**First check against real lines (opening night 2026-27):** the ML model sits systematically *below* the books
(median −0.9 points on PTS props), most of all for young players the market expects to improve. Until enough
collected lines are graded to measure and correct this, treat its recommendations as unproven. Calibration is within ~1 point
in the 20–70% range where nearly all bets fall.

**What we learned about matchup data:** retraining without the shot-zone and play-type features gives the same
holdout accuracy (within 0.04%). Game-level matchup effects are small next to game-to-game randomness, and
last-season team profiles are coarse. The features stay in, and the reasoning shows their measured effect, but they
are not where the edge comes from. Nearly all of the model's signal is player form, minutes and schedule.

Free historical sportsbook lines aren't available, so these tests use each player's season-to-date average as the
line. They measure accuracy, **not** profit against real books, which are much sharper. Testing against real
historical lines is the planned next step.

## Limitations

- Injury statuses change through the day; re-check props close to tip-off. Minutes restrictions aren't known.
- Rookies aren't projected until they've played 8 NBA games.
- No zone vs. man coverage data (not publicly available).
- Matchup profiles come from the previous season, so early-season roster and scheme changes aren't reflected.

For research and entertainment. Bet responsibly. 1-800-GAMBLER.

## Layout

```
app.py              web server + JSON API
train.py            download history + train the ML models
spm/data.py         stats.nba.com download, caching, dataset
spm/advanced.py     shot-zone and play-type profiles
spm/features.py     leak-free feature engine (training + live rows)
spm/ml.py           LightGBM training, walk-forward evaluation, predictor
spm/model.py        formula model and outcome distributions
spm/ev.py           odds maths, EV, Kelly, decisions
spm/reasoning.py    plain-English explanations
spm/backtest.py     walk-forward backtest + calibration fit
spm/props.py        props.csv storage
spm/lineups.py      who's playing: rotations, absences, injury report
spm/odds/           odds sources behind one interface (Offer + OddsSource + registry)
  base.py           the interface; `source` and `licensed` decide what's usable commercially
  action_network.py dev-only scraped feed (licensed = False)
  the_odds_api.py   licensed feed for launch (needs ODDS_API_KEY)
spm/collector.py    snapshots enabled sources (ODDS_SOURCES), change-only storage, grading
spm/yearly.py       year-by-year walk-forward test and feature ablation
collect_lines.py    legacy local snapshot + grading (the Windows task; now disabled)
scripts/collect.py  standalone cloud collector (GitHub Actions hourly); Postgres or CSV
scripts/migrate.py  apply migrations/*.sql to DATABASE_URL and sync the odds-source registry
scripts/import_lines.py  one-time import of pre-Postgres line history
spm/store/pg.py     Postgres storage: change-only writes in SQL, idempotent snapshots
spm/store/grading.py  incremental grading: closing line (last price before tip) vs box score, once
spm/boxscores.py    box scores for grading (stats.nba.com league game log, date-filtered)
spm/store/health.py pipeline health: stale collection / grading backlog checks, weekly report
scripts/health.py   daily "Health" workflow: fails (emails you) on missing success; Mondays posts a report
                    to the "Health report" issue and re-enables scheduled workflows (60-day keepalive)
scripts/grade.py    grade pending finished games (daily "Grade lines" workflow); --regrade-since, --check-source
migrations/         versioned schema (001_init.sql: sports, sources, events, offers, offers_current,
                    offers_licensed view; 002_grading.sql: results, grades, grades_licensed view)
.env.example        template for .env (DATABASE_URL, ODDS_API_KEY); .env is gitignored
spm/service.py      glue used by the API
tests/              pytest suite (EV math, parsing, grading, odds-source contract); run `pytest`
static/             front end (HTML/CSS/JS, no build step)
data/               props.csv, the download cache and trained models (data/models)
```
