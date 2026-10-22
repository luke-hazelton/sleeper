#!/usr/bin/env python3
"""Projection accuracy and lineup efficiency.

Every number the toolkit produces rests on the projection feed. This checks
the feed against what players actually scored in completed weeks, so you know
how big an edge has to be before it means anything. It also ranks each team
by points left on the bench (Sleeper's max-possible points vs actual).

A player projected but missing from the stats feed (inactive, hurt in
warmups) counts as 0, since that is what he scored in your lineup. Past
weeks' projections are whatever the feed holds now, normally its final
pre-game numbers.

Usage:
  accuracy.py                    all completed weeks, players projected >= 5
  accuracy.py --start 3 --min 8  weeks 3 on, likely starters only
"""
import argparse
import math
import statistics
import sys

import sleeper
from analysis import League
from ros import END_WEEK

POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")


def compare(proj_weeks, stat_weeks, key, pos_of, min_proj):
    """[(pos, projected, actual)] for players projected at least `min_proj`.
    proj_weeks/stat_weeks map week -> {pid: row}; `pos_of` maps pid -> position."""
    out = []
    for wk, proj in proj_weeks.items():
        stats = stat_weeks.get(wk)
        if stats is None:
            continue
        for pid, row in proj.items():
            p = sleeper.proj_points(row, key)
            pos = pos_of(pid)
            if p < min_proj or pos not in POSITIONS:
                continue
            s = stats.get(pid) or {}
            out.append((pos, p, float(s.get(key) or 0.0)))
    return out


def summarize(rows):
    """{pos or "ALL": dict(n, proj, actual, bias, mae, sd, corr)}."""
    groups = {}
    for pos, p, a in rows:
        groups.setdefault(pos, []).append((p, a))
        groups.setdefault("ALL", []).append((p, a))
    out = {}
    for pos, pairs in groups.items():
        proj, act = [p for p, _ in pairs], [a for _, a in pairs]
        err = [a - p for p, a in pairs]
        try:
            corr = statistics.correlation(proj, act)
        except statistics.StatisticsError:  # fewer than 2 rows, or constant
            corr = float("nan")
        out[pos] = {"n": len(pairs), "proj": statistics.fmean(proj),
                    "actual": statistics.fmean(act), "bias": statistics.fmean(err),
                    "mae": statistics.fmean(abs(e) for e in err),
                    "sd": statistics.stdev(err) if len(err) > 1 else 0.0, "corr": corr}
    return out


def noise_floor(sd, weeks_left):
    """Error SD of the difference between two players' ROS totals, treating
    each week's miss as independent."""
    return sd * math.sqrt(2 * max(weeks_left, 0))


def efficiency(rosters):
    """[(name_key, actual, max possible)] from roster settings; skips teams
    Sleeper hasn't computed max points for yet."""
    out = []
    for r in rosters:
        s = r.get("settings") or {}
        mx = s.get("ppts", 0) + s.get("ppts_decimal", 0) / 100
        if mx:
            out.append((r["roster_id"], s.get("fpts", 0) + s.get("fpts_decimal", 0) / 100, mx))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--end", type=int, help="last week (default: last completed)")
    ap.add_argument("--min", type=float, default=5.0,
                    help="only players projected at least this many points")
    args = ap.parse_args()

    cfg = sleeper.require_config()
    lg = League(cfg["league_id"])
    open_wk = sleeper.first_open_week(lg.season)
    end = args.end or open_wk - 1
    if end < args.start:
        sys.exit("No completed weeks to check yet.")
    weeks = range(args.start, end + 1)
    proj = {wk: sleeper.projections(lg.season, wk) for wk in weeks}
    stats = {wk: sleeper.weekly_stats(lg.season, wk) for wk in weeks}
    rows = compare(proj, stats, lg.score_key, lg.pos, args.min)
    summ = summarize(rows)

    print(f"\nPROJECTION ACCURACY | weeks {args.start}-{end} | projected >= {args.min:g} "
          f"| {lg.score_key} | {cfg['league_name']}\n")
    if not rows:
        print("  No projected players in that range.")
        return
    print(f"  {'POS':<5}{'N':>6}{'PROJ':>7}{'ACTUAL':>8}{'BIAS':>7}{'MAE':>7}{'ERR SD':>8}{'CORR':>7}")
    for pos in [p for p in POSITIONS if p in summ] + ["ALL"]:
        s = summ[pos]
        print(f"  {pos:<5}{s['n']:6d}{s['proj']:7.1f}{s['actual']:8.1f}{s['bias']:+7.1f}"
              f"{s['mae']:7.1f}{s['sd']:8.1f}{s['corr']:7.2f}")
    print("\n  BIAS = actual minus projected on average (+ means the feed runs low).")
    print("  MAE = typical miss for one player-week. CORR = how well the feed orders players.")

    left = max(END_WEEK - open_wk + 1, 0)
    if left:
        floor = noise_floor(summ["ALL"]["sd"], left)
        print(f"\n  NOISE FLOOR: with {left} weeks left, two players' rest-of-season totals")
        print(f"  carry about +/-{floor:.0f} pts of projection error between them. A ros.py")
        print(f"  edge much smaller than that is a coin flip, not a reason to trade.")

    eff = efficiency(lg.rosters)
    if eff:
        team = {r["roster_id"]: lg.owner_name(r) for r in lg.rosters}
        mine = (lg.roster_for_user(cfg["user_id"]) or {}).get("roster_id")
        print(f"\n  LINEUP EFFICIENCY (season to date)\n")
        print(f"  {'TEAM':<24}{'PF':>8}{'MAX PF':>9}{'BENCHED':>9}{'EFF':>7}")
        for rid, pf, mx in sorted(eff, key=lambda e: -e[1] / e[2]):
            print(f"  {team[rid][:23]:<24}{pf:8.1f}{mx:9.1f}{mx - pf:9.1f}{pf / mx:7.1%}"
                  + ("  <- you" if rid == mine else ""))
        print("\n  BENCHED = points your best possible lineup would have added (hindsight).")


if __name__ == "__main__":
    main()
