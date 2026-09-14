#!/usr/bin/env python3
"""Cross-league positional surplus/deficit finder -> trade partner matching.

Ranks every team's players within each position, then compares the quality of
their starters against replacement level to find who is deep and who is thin.
"""
import argparse
import sys
import sleeper
from analysis import League

# roughly how many of each position a team must start weekly in this format
CORE = {"QB": 2, "RB": 2, "WR": 2, "TE": 1}


def team_profile(lg, roster):
    """Return {pos: (starter_strength, surplus_strength, players_sorted)}."""
    by = {}
    for pid in roster.get("players") or []:
        by.setdefault(lg.pos(pid), []).append(pid)
    out = {}
    for pos, need in CORE.items():
        group = sorted(by.get(pos, []), key=lambda p: -lg.points(p, adjust=False))
        starters = group[:need]
        extra = group[need:]
        s_str = sum(lg.points(p, adjust=False) for p in starters) / max(need, 1)
        # surplus = quality of the best bench piece at that position
        x_str = max((lg.points(p, adjust=False) for p in extra), default=0.0)
        out[pos] = (s_str, x_str, group)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int)
    args = ap.parse_args()
    cfg = sleeper.load_config()
    if not cfg.get("league_id"):
        sys.exit("run ff.py setup first")
    lg = League(cfg["league_id"], args.week)

    me = lg.roster_for_user(cfg["user_id"])
    profiles = {r["roster_id"]: team_profile(lg, r) for r in lg.rosters}
    mine = profiles[me["roster_id"]]

    print(f"\nPOSITIONAL STRENGTH — Week {lg.week} — {cfg['league_name']}")
    print("(avg proj of required starters | best surplus bench piece)\n")
    print(f"  {'TEAM':<24}" + "".join(f"{p:>16}" for p in CORE))
    for r in lg.rosters:
        pr = profiles[r["roster_id"]]
        row = "".join(f"{pr[p][0]:8.1f}/{pr[p][1]:<7.1f}" for p in CORE)
        tag = " <- you" if r["roster_id"] == me["roster_id"] else ""
        print(f"  {lg.owner_name(r):<24}{row}{tag}")

    # my weakest / strongest relative to league median
    med = {p: sorted(profiles[r["roster_id"]][p][0] for r in lg.rosters)[len(lg.rosters)//2]
           for p in CORE}
    print("\n  YOUR GAPS vs league median")
    gaps = sorted(CORE, key=lambda p: mine[p][0] - med[p])
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
        surplus = [p for p in pr[need][2][CORE[need]:]][:3]
        if surplus:
            print("        their spare " + need + ": " +
                  ", ".join(f"{lg.name(p)} ({lg.points(p, adjust=False):.1f})" for p in surplus))


if __name__ == "__main__":
    main()
