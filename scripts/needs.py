#!/usr/bin/env python3
"""Cross-league positional surplus/deficit finder -> trade partner matching.

Ranks every team's players within each position by average weekly points over
the coming weeks, then compares starters against the league to find who is
deep and who is thin.

Values follow the same rules as ros.py: injuries are applied per week (recorded
return weeks, short-term tags, the projection feed), and a bye only zeroes the
week it happens, so one bye can't make a team look thin.
"""
import argparse
import sys

import sleeper
from analysis import BENCH_SLOTS, League, trade_window
from playoffs import outlook, playoff_odds
from ros import ROS

POSITIONS = ("QB", "RB", "WR", "TE")


def core_needs(roster_positions):
    """Weekly starters required per position, from the league's lineup slots.

    SUPER_FLEX counts as a QB, since that's how it gets filled. Plain FLEX slots
    are left out because any of RB/WR/TE can fill them.
    """
    need = {}
    for slot in roster_positions:
        pos = "QB" if slot == "SUPER_FLEX" else slot
        if pos in POSITIONS:
            need[pos] = need.get(pos, 0) + 1
    return {p: need[p] for p in POSITIONS if p in need}


def uncounted_slots(roster_positions):
    """Starting slots core_needs leaves out, e.g. "FLEX x2, K, DEF"."""
    out = {}
    for slot in roster_positions:
        if slot in BENCH_SLOTS or slot == "SUPER_FLEX" or slot in POSITIONS:
            continue
        out[slot] = out.get(slot, 0) + 1
    return ", ".join(f"{s} x{n}" if n > 1 else s for s, n in out.items())


def team_profile(lg, roster, value, core):
    """Return {pos: (starter_strength, surplus_strength, players_sorted)}."""
    by = {}
    for pid in roster.get("players") or []:
        by.setdefault(lg.pos(pid), []).append(pid)
    out = {}
    for pos, need in core.items():
        group = sorted(by.get(pos, []), key=lambda p: -value(p))
        starters, extra = group[:need], group[need:]
        s_str = sum(value(p) for p in starters) / need
        # surplus = quality of the best bench piece at that position
        x_str = max((value(p) for p in extra), default=0.0)
        out[pos] = (s_str, x_str, group)
    return out


def league_medians(profiles, core):
    """Median starter strength per position across all teams."""
    n = len(profiles)
    return {p: sorted(pr[p][0] for pr in profiles.values())[n // 2] for p in core}


def rank_partners(profiles, my_id, need, have, med):
    """Other teams ranked by fit: surplus at what I need, below median at what
    I have. Returns [(score, roster_id)], best first."""
    out = []
    for rid, pr in profiles.items():
        if rid != my_id:
            out.append((pr[need][1] - (pr[have][0] - med[have]), rid))
    out.sort(key=lambda c: -c[0])
    return out


def build_parser():
    # allow_abbrev=False: otherwise "--week 2" silently becomes "--weeks 2"
    ap = argparse.ArgumentParser(allow_abbrev=False)
    span = ap.add_mutually_exclusive_group()
    span.add_argument("--week", type=int, help="value a single week")
    span.add_argument("--weeks", type=int,
                      help="number of upcoming weeks to average (default: rest of season)")
    ap.add_argument("--end", type=int, default=17)
    return ap


def main():
    args = build_parser().parse_args()
    cfg = sleeper.require_config()
    lg = League(cfg["league_id"])
    me = lg.roster_for_user(cfg["user_id"])
    if not me:
        sys.exit("You don't have a roster in this league.")

    if args.week:
        start = end = args.week
    else:
        start = sleeper.first_open_week(lg.season)
        end = min(args.end, start + args.weeks - 1) if args.weeks else args.end
    ros = ROS(lg, start, end, lg.info["settings"].get("playoff_week_start", 15))
    n_weeks = max(len(ros.weeks), 1)
    cache = {}

    def value(pid):
        """Average weekly points over the window; injured weeks and byes are 0."""
        if pid not in cache:
            cache[pid] = ros.total(pid) / n_weeks
        return cache[pid]

    core = core_needs(lg.roster_positions)
    profiles = {r["roster_id"]: team_profile(lg, r, value, core) for r in lg.rosters}
    mine = profiles[me["roster_id"]]

    span = f"week {start}" if start == end else f"weeks {start}-{end}"
    print(f"\nPOSITIONAL STRENGTH | {span} | {cfg['league_name']}")
    print("(avg weekly pts of required starters | best surplus bench piece)")
    print("  required starters: " + ", ".join(f"{p} {n}" for p, n in core.items()))
    skipped = uncounted_slots(lg.roster_positions)
    if skipped:
        print(f"  not counted: {skipped} (FLEX goes to your best leftover RB/WR/TE;"
              " ros.py --byes covers the whole lineup)")
    print()
    print(f"  {'TEAM':<24}" + "".join(f"{p:>16}" for p in core))
    for r in lg.rosters:
        pr = profiles[r["roster_id"]]
        row = "".join(f"{pr[p][0]:8.1f}/{pr[p][1]:<7.1f}" for p in core)
        tag = " <- you" if r["roster_id"] == me["roster_id"] else ""
        print(f"  {lg.owner_name(r):<24}{row}{tag}")

    # my weakest / strongest relative to league median
    med = league_medians(profiles, core)
    print("\n  YOUR GAPS vs league median")
    gaps = sorted(core, key=lambda p: mine[p][0] - med[p])
    for p in gaps:
        d = mine[p][0] - med[p]
        mark = "WEAK " if d < -1.5 else ("STRONG" if d > 1.5 else "even  ")
        print(f"    {p:<4} {mark} you {mine[p][0]:5.1f} vs median {med[p]:5.1f}  ({d:+.1f})"
              f"   surplus bench: {mine[p][1]:.1f}")

    need = gaps[0]
    have = gaps[-1]
    week = sleeper.current_week()[0]
    open_, deadline = trade_window(lg.trade_deadline, week)
    print(f"\n  {deadline}")
    odds, _ = playoff_odds(lg, sims=2000, seed=0)
    p = odds[me["roster_id"]]["playoffs"]
    label, advice = outlook(p)
    print(f"  Playoff odds {p:.0%} ({label}): {advice}. Details: playoffs.py")
    if not open_:
        return
    print(f"\n  TRADE THESIS: you are thinnest at {need}, deepest at {have}.")
    if label == "LONG SHOT":
        print(f"  As a long shot, buying at {need} likely won't change your season;")
        print(f"  the teams below are thin at {have} and may pay for yours instead.")
    print(f"  Partners who are deep at {need} and thin at {have}:\n")
    by_id = {r["roster_id"]: r for r in lg.rosters}
    for score, rid in rank_partners(profiles, me["roster_id"], need, have, med)[:5]:
        r, pr = by_id[rid], profiles[rid]
        print(f"    {lg.owner_name(r):<24} surplus {need}: {pr[need][1]:5.1f}"
              f"   their {have}: {pr[have][0]:5.1f} (median {med[have]:.1f})")
        surplus = pr[need][2][core[need]:][:3]
        if surplus:
            print("        their spare " + need + ": " +
                  ", ".join(f"{lg.name(p)} ({value(p):.1f})" for p in surplus))


if __name__ == "__main__":
    main()
