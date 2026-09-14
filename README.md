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
python3 scripts/ff.py lineup              # optimal start/sit vs your current lineup
python3 scripts/ff.py matchup             # head-to-head projection
python3 scripts/ff.py waivers -n 15       # best free agents, ranked by lineup gain
python3 scripts/ff.py waivers --pos RB    # ...filtered to a position
python3 scripts/ff.py league              # power rankings
python3 scripts/needs.py                  # positional surplus/deficit + trade partners
python3 scripts/ff.py trade --give "Geno Smith,Travis Kelce" --get "Travis Etienne"

# rest-of-season (the one that matters for trades)
python3 scripts/ros.py --give "Geno Smith" --get "Travis Etienne"
python3 scripts/ros.py --pos RB -n 20        # ROS ranks + who owns them
python3 scripts/ros.py --players "Josh Allen,Kyler Murray"

./scripts/weekly.sh 2                     # everything at once, for week 2
```

All commands accept `--week N`. Without it they use the current NFL week — which
during a live Sunday is the week already in progress, so pass `--week N+1` when
you're planning ahead.

## How the recommendations are computed

- **Projections** come from Sleeper's Rotowire feed (`/projections/nfl/{season}/{week}`),
  scored with your league's actual scoring settings (full PPR here).
- **Injury discounting**: Out/IR/PUP → 0x, Doubtful → 0.25x, Questionable → 0.85x.
  Discounted numbers are flagged with `*`.
- **Lineup optimizer** fills dedicated slots before flex slots (most-constrained
  first), so a flex-eligible stud isn't wasted on a slot only he can fill.
- **Waiver "GAIN"** = how much a player would add to your *optimal starting lineup*,
  not his raw projection. A great player you'd never start scores 0 gain.
- **Trade evaluation** compares optimal-lineup totals before/after for *both* sides,
  so you can see whether an offer is plausibly acceptable to the other manager.

## Known limits — read before trusting a number

- Projections are **pre-game snapshots**. A player injured mid-game keeps his stale
  projection until the feed refreshes (Kyler Murray projected 18.7 for Week 2 while
  in concussion protocol). Always sanity-check injured players against the news.
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
scripts/analysis.py  League model, scoring, lineup optimizer
scripts/ff.py        main CLI
scripts/needs.py     cross-league positional surplus -> trade partner matching
scripts/weekly.sh    runs the full weekly report
data/cache/          API responses (auto-expiring)
data/players_raw.json / players_slim.json   player DB (refreshed daily)
```

Caching: player DB 24h, projections 6h, rosters/matchups 5min, trending 30min.
Delete `data/cache/` to force a refresh.
