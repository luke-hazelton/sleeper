#!/usr/bin/env python3
"""Playoff odds: simulates the rest of the regular season, game by game.

Sleeper publishes every regular-season pairing up front, so each remaining
game is known. Each team's score in it is its projected optimal lineup that
week (same per-week injury rules as ros.py) plus random noise, sized from how
much teams' actual weekly scores have swung so far this season. A week already
in progress starts from the live score instead. Ties in the standings go to
points for, Sleeper's default tiebreaker. Divisions aren't modeled.

Usage:
  playoffs.py                 odds for every team + your remaining schedule
  playoffs.py --sims 50000    more simulations (slower, steadier numbers)
"""
import argparse
import math
import random
import statistics
import sys

import sleeper
from analysis import League, trade_window
from ff import _estimate
from ros import load_weeks, week_values

DEFAULT_SD = 25.0  # weekly score swing to assume before two weeks are played
BYES = {6: 2}      # playoff teams -> first-round byes (Sleeper's 6-team bracket)


def win_prob(diff, sd):
    """P(win) when you project `diff` points more and each side's score
    varies with standard deviation `sd`."""
    return 0.5 * (1 + math.erf(diff / (2 * sd)))


def team_week_sd(scores):
    """Pooled standard deviation of teams' weekly scores around each team's
    own average. `scores` maps roster_id -> [weekly points]. Falls back to
    DEFAULT_SD until there are at least two weeks of data."""
    sq = n = 0
    for pts in scores.values():
        if len(pts) < 2:
            continue
        mu = statistics.fmean(pts)
        sq += sum((p - mu) ** 2 for p in pts)
        n += len(pts) - 1
    return math.sqrt(sq / n) if n else DEFAULT_SD


def simulate(standings, games, means, sd, playoff_teams, sims=10000, seed=None, sds=None):
    """Monte Carlo the remaining games.

    standings: {roster_id: (wins, points_for)}, ties counted as half a win
    games:     [(week, roster_a, roster_b)]
    means:     {(week, roster_id): projected points}
    sds:       optional {(week, roster_id): sd} overriding `sd`
    Returns {roster_id: {"playoffs", "bye", "seed", "wins"}} as averages.
    """
    rng = random.Random(seed)
    sds = sds or {}
    rids = list(standings)
    games = [(a, b, means.get((wk, a), 0.0), means.get((wk, b), 0.0),
              sds.get((wk, a), sd), sds.get((wk, b), sd)) for wk, a, b in games]
    byes = BYES.get(playoff_teams, 0)
    tally = {r: {"playoffs": 0, "bye": 0, "seed": 0, "wins": 0.0} for r in rids}
    for _ in range(sims):
        wins = {r: standings[r][0] for r in rids}
        pf = {r: standings[r][1] for r in rids}
        for a, b, ma, mb, sa, sb in games:
            xa, xb = rng.gauss(ma, sa), rng.gauss(mb, sb)
            pf[a] += xa
            pf[b] += xb
            if xa == xb:
                wins[a] += 0.5
                wins[b] += 0.5
            else:
                wins[a if xa > xb else b] += 1
        for seed_no, r in enumerate(sorted(rids, key=lambda r: (-wins[r], -pf[r])), 1):
            t = tally[r]
            t["seed"] += seed_no
            t["wins"] += wins[r]
            t["playoffs"] += seed_no <= playoff_teams
            t["bye"] += seed_no <= byes
    return {r: {k: v / sims for k, v in t.items()} for r, t in tally.items()}


def outlook(p):
    """Buy/sell framing for a playoff probability."""
    if p >= 0.8:
        return "CONTENDER", "buy for the playoff weeks; ros.py shows playoff-week lineup impact"
    if p >= 0.3:
        return "BUBBLE", "trades that help now matter most; every regular-season win counts"
    return "LONG SHOT", "consider selling this-season production for keeper value or picks"


def season_inputs(lg):
    """Standings, remaining games and per-team weekly projections.

    Returns a dict with standings, games, means, sds, sd, start, end, sd_weeks.
    """
    last = lg.setting("last_scored_leg", 0)
    start, end = last + 1, lg.reg_end
    standings = {}
    for r in lg.rosters:
        s = r.get("settings") or {}
        standings[r["roster_id"]] = (s.get("wins", 0) + 0.5 * s.get("ties", 0),
                                     s.get("fpts", 0) + s.get("fpts_decimal", 0) / 100)

    scores = {}
    for wk in range(1, last + 1):
        for m in sleeper.matchups(lg.id, wk):
            if m.get("points"):
                scores.setdefault(m["roster_id"], []).append(m["points"])

    games, means, sds = [], {}, {}
    if start <= end:
        games = [(wk, a, b) for wk in range(start, end + 1)
                 for a, b in sleeper.schedule_pairs(lg.id, wk)]
        rids = [r["roster_id"] for r in lg.rosters]
        pools = [r.get("players") or [] for r in lg.rosters]
        for wk, vals in week_values(lg, load_weeks(lg.season, start, end), pools).items():
            means.update({(wk, rid): v for rid, v in zip(rids, vals)})

    sd = team_week_sd(scores)
    cw = sleeper.current_week()[0]
    if start <= cw <= end and lg.week == cw and any(
            s != "pre_game" for s in sleeper.game_status(lg.season, cw).values()):
        # games underway: live score so far + what's left, with less noise
        for m in sleeper.matchups(lg.id, cw):
            live = m.get("players_points") or {}
            got = left = 0.0
            for p in m.get("starters") or []:
                if p and p != "0":
                    a, rest = _estimate(lg, p, live)
                    got, left = got + a, left + rest
            means[(cw, m["roster_id"])] = got + left
            sds[(cw, m["roster_id"])] = sd * math.sqrt(left / max(got + left, 1.0))
    return {"standings": standings, "games": games, "means": means, "sds": sds,
            "sd": sd, "start": start, "end": end, "sd_weeks": last}


def playoff_odds(lg, sims=10000, seed=None):
    """(results by roster_id, inputs) for the league as it stands."""
    inp = season_inputs(lg)
    res = simulate(inp["standings"], inp["games"], inp["means"], inp["sd"],
                   lg.setting("playoff_teams", 6), sims, seed, inp["sds"])
    return res, inp


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sims", type=int, default=10000)
    ap.add_argument("--seed", type=int, help="fix the random seed for repeatable output")
    args = ap.parse_args()

    cfg = sleeper.require_config()
    lg = League(cfg["league_id"])
    me = lg.roster_for_user(cfg["user_id"])
    if not me:
        sys.exit("You don't have a roster in this league.")
    res, inp = playoff_odds(lg, args.sims, args.seed)
    n_po = lg.setting("playoff_teams", 6)
    byes = BYES.get(n_po, 0)
    start, end = inp["start"], inp["end"]
    team = {r["roster_id"]: lg.owner_name(r) for r in lg.rosters}
    my_id = me["roster_id"]

    span = f"weeks {start}-{end}" if start <= end else "regular season complete"
    print(f"\nPLAYOFF ODDS | {args.sims:,} sims of {span} | {cfg['league_name']}")
    print(f"  {n_po} of {len(lg.rosters)} make the playoffs"
          + (f" (top {byes} get byes)" if byes else "") + ". Ties broken by points for.")
    basis = f"weeks 1-{inp['sd_weeks']}" if inp["sd_weeks"] >= 2 else "default, too few weeks played"
    print(f"  Weekly score swing: +/-{inp['sd']:.1f} pts ({basis}).")
    print("  " + trade_window(lg.trade_deadline, sleeper.current_week()[0])[1])
    if lg.setting("divisions"):
        print("  This league has divisions; division winners aren't modeled.")

    left = {r: [] for r in team}
    opp_proj = {r: [] for r in team}
    for wk, a, b in inp["games"]:
        for x, y in ((a, b), (b, a)):
            left[x].append(inp["means"].get((wk, x), 0.0))
            opp_proj[x].append(inp["means"].get((wk, y), 0.0))
    avg = lambda xs: statistics.fmean(xs) if xs else 0.0  # noqa: E731

    print(f"\n  {'TEAM':<24}{'REC':>7}{'PF':>8}{'PROJ/WK':>9}{'OPP/WK':>8}"
          f"{'xWINS':>7}{'PLAYOFF':>9}" + (f"{'BYE':>7}" if byes else "") + f"{'SEED':>6}")
    for rid in sorted(team, key=lambda r: (-res[r]["playoffs"], res[r]["seed"])):
        s = next(r for r in lg.rosters if r["roster_id"] == rid)["settings"]
        rec = f"{s.get('wins', 0)}-{s.get('losses', 0)}" + (f"-{s['ties']}" if s.get("ties") else "")
        o = res[rid]
        print(f"  {team[rid][:23]:<24}{rec:>7}{inp['standings'][rid][1]:8.1f}"
              f"{avg(left[rid]):9.1f}{avg(opp_proj[rid]):8.1f}{o['wins']:7.1f}"
              f"{o['playoffs']:9.0%}" + (f"{o['bye']:7.0%}" if byes else "")
              + f"{o['seed']:6.1f}" + ("  <- you" if rid == my_id else ""))
    print("\n  PROJ/WK = your projected optimal lineup, OPP/WK = your opponents' (schedule strength)")

    mine = [(wk, b if a == my_id else a) for wk, a, b in inp["games"] if my_id in (a, b)]
    if mine:
        print(f"\n  YOUR REMAINING SCHEDULE")
        for wk, opp in mine:
            m_you, m_opp = inp["means"].get((wk, my_id), 0.0), inp["means"].get((wk, opp), 0.0)
            sd = math.hypot(inp["sds"].get((wk, my_id), inp["sd"]),
                            inp["sds"].get((wk, opp), inp["sd"])) / math.sqrt(2)
            live = " (live)" if (wk, my_id) in inp["sds"] else ""
            print(f"    wk {wk:<3} vs {team[opp][:22]:<23}{m_you:6.1f} - {m_opp:<6.1f}"
                  f"  win {win_prob(m_you - m_opp, sd):4.0%}{live}")

    label, advice = outlook(res[my_id]["playoffs"])
    print(f"\n  OUTLOOK: {label} ({res[my_id]['playoffs']:.0%}). {advice[0].upper() + advice[1:]}.")


if __name__ == "__main__":
    main()
