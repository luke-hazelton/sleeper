#!/usr/bin/env python3
"""Rest-of-season valuation: aggregates weekly projections across the remaining
schedule, split by regular season and fantasy playoffs.

Single-week trade math is misleading because it can't see bye weeks, schedule,
or what a player does in the weeks that decide your season. This can.
"""
import argparse
import sys

import sleeper
from analysis import League

REG_END = 14  # last regular-season week before playoffs (playoff_week_start - 1)


def load_weeks(season, start, end):
    """{week: {pid: pts}} for the given range."""
    out = {}
    for wk in range(start, end + 1):
        try:
            out[wk] = sleeper.projections(season, wk)
        except Exception as e:
            print(f"  (week {wk} unavailable: {e})", file=sys.stderr)
    return out


class ROS:
    def __init__(self, lg, start, end, playoff_start):
        self.lg = lg
        self.start, self.end = start, end
        self.playoff_start = playoff_start
        self.weeks = load_weeks(lg.season, start, end)

    def pts(self, pid, wk):
        proj = self.weeks.get(wk, {})
        return (sleeper.proj_points(proj.get(pid), self.lg.score_key)
                * self.lg.availability(pid, wk, proj))

    def total(self, pid, lo=None, hi=None):
        lo = lo or self.start
        hi = hi or self.end
        return sum(self.pts(pid, w) for w in range(lo, hi + 1) if w in self.weeks)

    def games(self, pid, lo=None, hi=None):
        """Weeks with a real projection — i.e. not a bye and not buried."""
        lo = lo or self.start
        hi = hi or self.end
        return sum(1 for w in range(lo, hi + 1)
                   if w in self.weeks and self.pts(pid, w) > 0)

    def ppg(self, pid, lo=None, hi=None):
        g = self.games(pid, lo, hi)
        return self.total(pid, lo, hi) / g if g else 0.0

    def line(self, pid):
        lg = self.lg
        reg = self.total(pid, self.start, min(REG_END, self.end))
        po = self.total(pid, self.playoff_start, self.end)
        return (f"  {lg.name(pid):<22}{lg.pos(pid):<4}{lg.team_of(pid):<4}"
                f"{self.total(pid):7.1f}{self.ppg(pid):7.1f}"
                f"{self.games(pid):5d}{reg:8.1f}{po:8.1f}")

    def header(self):
        return (f"  {'PLAYER':<22}{'POS':<4}{'TM':<4}{'TOTAL':>7}{'PPG':>7}"
                f"{'GMS':>5}{'REG':>8}{'PLYF':>8}")


def lineup_impact(lg, weeks, before, after, playoff_start):
    """Optimal-lineup totals for two rosters, summed over `weeks` ({week: proj}).

    Returns (before, after, before_playoffs, after_playoffs). Restores the
    league's current week afterwards, so later lookups (e.g. injury tags) see
    this week's data rather than the last week looped over.
    """
    saved = lg.week, lg.proj
    tb = ta = tbp = tap = 0.0
    try:
        for wk, proj in weeks.items():
            lg.set_week(wk, proj)
            b, a = lg.starter_value(before), lg.starter_value(after)
            tb += b
            ta += a
            if wk >= playoff_start:
                tbp += b
                tap += a
    finally:
        lg.set_week(*saved)
    return tb, ta, tbp, tap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--give"); ap.add_argument("--get")
    ap.add_argument("--players", help="comma-separated names to just value")
    ap.add_argument("--pos", help="rest-of-season ranks at a position")
    ap.add_argument("-n", "--num", type=int, default=20)
    ap.add_argument("--end", type=int, default=17)
    ap.add_argument("--start", type=int,
                    help="first week to count (default: first week with no games played)")
    args = ap.parse_args()

    cfg = sleeper.load_config()
    if not cfg.get("league_id"):
        sys.exit("No league configured. Run:  ff.py setup <your_sleeper_username>")
    lg = League(cfg["league_id"])
    me = lg.roster_for_user(cfg["user_id"])
    if not me:
        sys.exit("You don't have a roster in this league.")
    start = args.start or sleeper.first_open_week(lg.season)
    po_start = lg.info["settings"].get("playoff_week_start", 15)

    print(f"\nREST OF SEASON — weeks {start}-{args.end} "
          f"(playoffs {po_start}-{args.end}) — {cfg['league_name']}")
    ros = ROS(lg, start, args.end, po_start)

    def resolve(names, pool=None):
        out = []
        for n in names.split(","):
            try:
                out.append(sleeper.resolve_player(n, pool))
            except ValueError as e:
                sys.exit(str(e))
        return out

    if args.pos:
        want = {p.strip().upper() for p in args.pos.split(",")}
        pool = [p for p in lg.players if lg.pos(p) in want]
        pool.sort(key=lambda p: -ros.total(p))
        rostered = lg.rostered_ids()
        me = set((lg.roster_for_user(cfg["user_id"]) or {}).get("players") or [])
        print(f"\n{ros.header()}  OWNER")
        for pid in pool[:args.num]:
            tag = "YOU" if pid in me else ("taken" if pid in rostered else "FA")
            print(ros.line(pid) + f"  {tag}")
        return

    if args.players:
        print(f"\n{ros.header()}")
        for pid in resolve(args.players):
            print(ros.line(pid))
        return

    if not (args.give and args.get):
        sys.exit("pass --give/--get, --players, or --pos")

    mine_ids = set(me.get("players") or [])
    give, get = resolve(args.give, mine_ids), resolve(args.get, lg.rostered_ids())
    not_mine = [lg.name(p) for p in give if p not in mine_ids]
    if not_mine:
        sys.exit(f"Not on your roster: {', '.join(not_mine)}")
    print(f"\n{ros.header()}")
    print("  -- YOU GIVE --")
    for pid in give:
        print(ros.line(pid))
    print("  -- YOU GET --")
    for pid in get:
        print(ros.line(pid))

    gv, gt = sum(ros.total(p) for p in give), sum(ros.total(p) for p in get)
    gv_p = sum(ros.total(p, po_start, args.end) for p in give)
    gt_p = sum(ros.total(p, po_start, args.end) for p in get)
    print(f"\n  Raw ROS points   give {gv:7.1f}   get {gt:7.1f}   ({gt - gv:+.1f})")
    print(f"  Playoff weeks    give {gv_p:7.1f}   get {gt_p:7.1f}   ({gt_p - gv_p:+.1f})")

    # What actually matters: weekly starting-lineup impact across the season.
    me = lg.roster_for_user(cfg["user_id"])
    mine = list(me.get("players") or [])
    after = [p for p in mine if p not in give] + get
    tb, ta, tbp, tap = lineup_impact(lg, ros.weeks, mine, after, po_start)
    print(f"\n  Optimal lineup, weeks {start}-{args.end}:")
    print(f"    before {tb:8.1f}   after {ta:8.1f}   ({ta - tb:+.1f})")
    print(f"  Playoff weeks {po_start}-{args.end}:")
    print(f"    before {tbp:8.1f}   after {tap:8.1f}   ({tap - tbp:+.1f})")
    print("\n  (lineup impact counts only what you'd actually start each week;")
    print("   raw points above include production you'd leave on the bench)")

    hurt = [p for p in dict.fromkeys(mine + get) if lg.injury(p) or p in lg.returns]
    if hurt:
        print("\n  Injury assumptions (record a return week with injuries.py --set):")
        for pid in hurt:
            back = next((w for w in ros.weeks if ros.pts(pid, w) > 0), None)
            how = "recorded return" if pid in lg.returns else "tag + projection feed"
            when = f"counted from week {back}" if back else "not counted this season"
            print(f"    {lg.name(pid):<22}{(lg.injury(pid) or '-'):<8}{when}  ({how})")


if __name__ == "__main__":
    main()
