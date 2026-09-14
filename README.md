# Sleeper Fantasy Football Helper

Decision tooling for a Sleeper fantasy football league (built against a 12-team, full PPR, SUPERFLEX league).

Pulls live data from Sleeper's public API — rosters, projections, injuries, trending
adds — and turns it into start/sit, waiver and trade recommendations.

No login required. Sleeper's read API is public; only your **username** is needed.

## Setup

Link your league once. This writes `config.json`, which is gitignored:

```bash
python3 scripts/ff.py setup <sleeper_username>
```

## Commands

```bash
python3 scripts/ff.py roster              # your roster + projections + injuries
python3 scripts/ff.py lineup              # optimal start/sit (locked players stay put)
python3 scripts/ff.py matchup             # live score + estimated final
python3 scripts/ff.py waivers -n 15       # best free agents, ranked by lineup gain
python3 scripts/ff.py waivers --pos RB    # ...filtered to a position
python3 scripts/ff.py league              # power rankings
python3 scripts/needs.py                  # positional strength (rest of season) + trade partners
python3 scripts/needs.py --weeks 1        # ...same, for just the next week
python3 scripts/ff.py trade --give "Geno Smith,Travis Kelce" --get "Travis Etienne"

# rest-of-season (the one that matters for trades)
python3 scripts/ros.py --give "Geno Smith" --get "Travis Etienne"
python3 scripts/ros.py --pos RB -n 20        # ROS ranks + who owns them
python3 scripts/ros.py --players "Josh Allen,Kyler Murray"

# injury return weeks (feeds into every ROS number)
python3 scripts/injuries.py                  # how your injured players are being counted
python3 scripts/injuries.py --players "Omarion Hampton,Kyler Murray"
python3 scripts/injuries.py --set "Kyler Murray" --week 3 --note "concussion" --source URL
python3 scripts/injuries.py --set "Malik Davis" --season-ending

./scripts/weekly.sh 2                     # everything at once, for week 2

python3 -m unittest discover tests        # offline unit tests (no network)
```

No packages are required. `pip install -r requirements.txt` only adds `certifi`, which
avoids the curl fallback on macOS Python builds that lack root certificates.

All commands accept `--week N`. Without it they use the current NFL week — which
during a live Sunday is the week already in progress, so pass `--week N+1` when
you're planning ahead.

`ros.py` and `injuries.py` instead start from the **first week with no games played
yet** (using game dates from the projection feed), so they stay correct both on a live
Sunday and after Sleeper rolls over to the new week. Override with `ros.py --start N`.

Player names must identify one player. Exact full names win, then players on the
relevant roster; if a name still matches several players, the command lists them
and stops instead of guessing. `ros.py` also refuses to "give" a player you don't own.

## How the recommendations are computed

- **Projections** come from Sleeper's Rotowire feed (`/projections/nfl/{season}/{week}`),
  scored with your league's actual scoring settings (full PPR here).
- **Injuries** are counted per week, in this order:
  1. A **recorded return week** (`data/injury_returns.json`, edited with `injuries.py`)
     wins: 0 points before it, full projection from it on.
  2. Otherwise the **injury tag** (Out/IR/PUP → 0x, Doubtful → 0.25x, Questionable → 0.85x)
     applies only to the current and next week. The tag describes the latest report,
     not the rest of the season.
  3. Later weeks use the **projection feed as-is**. The feed already zeroes players on
     multi-week IR (Jordyn Tyson projects 0 through week 5), but it does *not* know
     about fresh short-term injuries — that's what recorded return weeks are for.

  Discounted numbers are flagged with `*`.
- **Lineup optimizer** fills dedicated slots before flex slots (most-constrained
  first), so a flex-eligible stud isn't wasted on a slot only he can fill.
- **Live games** come from Sleeper's schedule feed (`/schedule/nfl/regular/{season}`),
  which marks each game `pre_game` / `in_game` / `complete`. Players whose game has
  started show `[LOCKED]`.
  - `lineup` keeps locked starters in their slots, won't suggest locked bench players,
    and only optimizes what you can still change.
  - `matchup` shows **scored so far + estimated remaining**: finished games count
    actual points, unstarted games count their projection, and in-progress games
    count actual points plus half of the projection not yet reached. The feed has no
    game clock, so that last part is rough.
- **Waiver "GAIN"** = how much a player would add to your *optimal starting lineup*,
  not his raw projection. A great player you'd never start scores 0 gain.
- **Trade evaluation** compares optimal-lineup totals before/after for *both* sides,
  so you can see whether an offer is plausibly acceptable to the other manager.

## Known limits — read before trusting a number

- Projections are **pre-game snapshots**. A player injured mid-game keeps his stale
  projection until the feed refreshes (Kyler Murray projected 18.7 for Week 2 while
  in concussion protocol). After a notable injury, check the news and record a return
  week with `injuries.py --set`.
- Before this per-week injury handling existed, an "Out" tag zeroed a player for the
  *entire* season, which made trading him look free. Treat any older trade numbers
  involving an injured player with suspicion.
- `ff.py trade` is **single-week** and will mislead you on trades. Use `ros.py` instead:
  it aggregates weeks through 17 and splits regular season from playoffs (15-17).
- Raw ROS points also mislead, in the opposite direction — they count bench production
  you'd never start. `ros.py --give/--get` reports **optimal-lineup impact**, which is
  the number to trust.
- Neither tool prices **injury insurance**, which is often the real reason to trade
  surplus depth. To check it, re-run lineup value with a starter removed
  (see the scenario table approach in the Week 1 analysis).
- Projections assume **current depth-chart roles**. A player who loses his job keeps
  his projection until the feed catches up, so always confirm job security with news
  before trusting a rest-of-season number.
- `n/a` in the opponent column means no projection row exists for that player, which
  is different from a real `BYE`.

## Layout

```
scripts/sleeper.py   API client + on-disk caching
scripts/analysis.py  League model, scoring, injury availability, lineup optimizer
scripts/ff.py        main CLI
scripts/ros.py       rest-of-season valuation + trade lineup impact
scripts/injuries.py  injury return-week tracker
scripts/needs.py     cross-league positional strength -> trade partner matching
scripts/weekly.sh    runs the full weekly report
tests/test_core.py   offline unit tests (python3 -m unittest discover tests)
requirements.txt     optional deps only (certifi); everything else is stdlib
data/injury_returns.json   recorded return weeks (committed, hand-curated)
data/cache/          API responses (auto-expiring)
data/players_raw.json / players_slim.json   player DB (refreshed daily)
```

Caching: player DB 24h, projections 6h, rosters/matchups 5min, trending 30min.
Delete `data/cache/` to force a refresh. The player DB lives outside it
(`data/players_raw.json`, `data/players_slim.json`); delete those to refresh it too.
