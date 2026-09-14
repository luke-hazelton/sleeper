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
from analysis import League
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int,
                    help="number of upcoming weeks to average (default: rest of season)")
    ap.add_argument("--end", type=int, default=17)
    args = ap.parse_args()
    cfg = sleeper.load_config()
    if not cfg.get("league_id"):
        sys.exit("No league configured. Run:  ff.py setup <your_sleeper_username>")
    lg = League(cfg["league_id"])
    me = lg.roster_for_user(cfg["user_id"])
    if not me:
        sys.exit("You don't have a roster in this league.")

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
    print(f"\nPOSITIONAL STRENGTH — {span} — {cfg['league_name']}")
    print("(avg weekly pts of required starters | best surplus bench piece)")
    print("  required starters: " + ", ".join(f"{p} {n}" for p, n in core.items()) + "\n")
    print(f"  {'TEAM':<24}" + "".join(f"{p:>16}" for p in core))
    for r in lg.rosters:
        pr = profiles[r["roster_id"]]
        row = "".join(f"{pr[p][0]:8.1f}/{pr[p][1]:<7.1f}" for p in core)
        tag = " <- you" if r["roster_id"] == me["roster_id"] else ""
        print(f"  {lg.owner_name(r):<24}{row}{tag}")

    # my weakest / strongest relative to league median
    med = {p: sorted(profiles[r["roster_id"]][p][0] for r in lg.rosters)[len(lg.rosters)//2]
           for p in core}
    print("\n  YOUR GAPS vs league median")
    gaps = sorted(core, key=lambda p: mine[p][0] - med[p])
    for p in gaps:
        d = mine[p][0] - med[p]
        mark = "WEAK " if d < -1.5 else ("STRONG" if d > 1.5 else "even  ")
        print(f"    {p:<4} {mark} you {mine[p][0]:5.1f} vs median {med[p]:5.1f}  ({d:+.1f})"
              f"   surplus bench: {mine[p][1]:.1f}")

    need = gaps[0]
    have = gaps[-1]
    print(f"\n  TRADE THESIS: you are thinnest at {need}, deepest at {have}.")
    print(f"  Partners who are deep at {need} and thin at {have}:\n")
    cands = []
    for r in lg.rosters:
        if r["roster_id"] == me["roster_id"]:
            continue
        pr = profiles[r["roster_id"]]
        # they have surplus at what I need, and are below median at what I have
        score = pr[need][1] - (pr[have][0] - med[have])
        cands.append((score, r, pr))
    cands.sort(key=lambda c: -c[0])
    for score, r, pr in cands[:5]:
        print(f"    {lg.owner_name(r):<24} surplus {need}: {pr[need][1]:5.1f}"
              f"   their {have}: {pr[have][0]:5.1f} (median {med[have]:.1f})")
        surplus = pr[need][2][core[need]:][:3]
        if surplus:
            print("        their spare " + need + ": " +
                  ", ".join(f"{lg.name(p)} ({value(p):.1f})" for p in surplus))


if __name__ == "__main__":
    main()
