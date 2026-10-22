# Sleeper API notes

Base: `https://api.sleeper.app/v1`. No auth, no API key. ~1000 req/min limit.
Read-only: nothing here can modify your team.

## Documented endpoints used

| Endpoint | Notes |
|---|---|
| `/state/nfl` | current `week`, `display_week`, `season` |
| `/user/{username}` | username → `user_id` |
| `/user/{user_id}/leagues/nfl/{season}` | that user's leagues |
| `/league/{id}` | `scoring_settings`, `roster_positions`, `settings` |
| `/league/{id}/rosters` | `players[]`, `starters[]`, `settings.wins/fpts` |
| `/league/{id}/users` | display names, `metadata.team_name` |
| `/league/{id}/matchups/{week}` | `matchup_id` pairs teams; `players_points` is live. Works for **future** weeks too: pairings for the whole regular season are published up front (points empty) |
| `/league/{id}/transactions/{week}` | `type` (`waiver`/`free_agent`/`trade`/`commissioner`), `status` (`complete`/`failed`), `adds`/`drops` as `{player_id: roster_id}`, `draft_picks`, `settings.waiver_bid` (FAAB only) |
| `/players/nfl` | ~14MB full player DB. Cache it; do not fetch per-request |
| `/players/nfl/trending/add?lookback_hours=24&limit=50` | league-wide add counts |
| `/stats/nfl/regular/{season}/{week}` | actual stats once games are played, keyed by player_id, with `pts_ppr`/`pts_half_ppr`/`pts_std`. Players who didn't play are absent |

## Undocumented but stable: the projections endpoint

```
https://api.sleeper.app/projections/nfl/{season}/{week}
    ?season_type=regular&position[]=QB&position[]=RB&order_by=pts_ppr
```

Returns one row per player with `stats` (`pts_ppr`, `pts_half_ppr`, `pts_std`,
plus component stats), `opponent`, `team`, `game_id`, and a nested `player`
object carrying a **fresher `injury_status`** than the bulk player DB. Source is
Rotowire (`company` field).

Note it is on the bare host, **not** under `/v1`.

Players with no real projection still appear, carrying only `adp_dd_ppr`. Filter
on `stats.pts_ppr != null` to get the meaningful rows.

## League and roster fields worth knowing

- `settings.trade_deadline`: deadline week; `99` means none.
- `settings.playoff_week_start`, `playoff_teams`, `last_scored_leg` (last week counted in standings).
- `settings.waiver_type`: `0` rolling, `1` reverse standings, `2` FAAB (`waiver_budget`).
- `settings.pick_trading`, `max_keepers`, `reserve_slots`, `taxi_slots`.
- Roster `settings`: `waiver_position`, `waiver_budget_used`, `fpts` (points for),
  `ppts` (max possible points), each with a `_decimal` companion in hundredths.
- Roster `reserve` / `taxi`: players on IR / taxi, also listed in `players`, not
  taking an active spot.

## Gotchas

- `players_points` in a matchup updates live during games; `points` is the total.
- A player whose game has kicked off is locked; compare team abbreviations against
  `/stats/.../{week}` to work out which games have already been played.
- DEF player_ids are team abbreviations (`"LAR"`), not numeric ids.
- `injury_status` values seen: `Questionable`, `Doubtful`, `Out`, `IR`, `PUP`, `Sus`, `NA`.
- Projections are **not** updated the instant a player is hurt mid-game.
- No rest-of-season projections and no strength-of-schedule are exposed anywhere.
  The toolkit builds both from weekly projections plus the published schedule.
