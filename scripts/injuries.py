#!/usr/bin/env python3
"""Injury return tracker: shows how each injured player is being counted in
rest-of-season math, and records expected return weeks from the news.

Usage:
  injuries.py                          injured players on your roster
  injuries.py --league                 ...on every roster in the league
  injuries.py --players "A,B"          specific players (e.g. trade targets)
  injuries.py --set "Kyler Murray" --week 3 --note "concussion" --source URL
  injuries.py --set "Malik Davis" --season-ending --note "hip surgery"
  injuries.py --clear "Kyler Murray"

How a player's weekly projection is counted:
  1. A return week recorded here wins: 0 before it, full projection from it on.
  2. Otherwise the injury tag (Out/Doubtful/Questionable) only discounts weeks
     up to the next one with no games played (plus any week in progress). It
     describes the latest report, not the season.
  3. Later weeks use the projection feed as-is, which already zeroes players
     on multi-week IR (e.g. a rookie on IR-designated-to-return).
"""
import argparse
import datetime
import json
import os
import sys

import sleeper
from analysis import League, RETURNS_FILE, SEASON_OVER
from ros import load_weeks

END_WEEK = 17


def _read_all():
    if not os.path.exists(RETURNS_FILE):
        return {}
    with open(RETURNS_FILE) as f:
        return json.load(f)


def _write_all(data):
    with open(RETURNS_FILE, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def _resolve(name):
    try:
        pid = sleeper.resolve_player(name)
    except ValueError as e:
        sys.exit(str(e))
    return pid, sleeper.players()[pid]


def _wk(w):
    if w is None:
        return "never"
    return "season over" if w >= SEASON_OVER else f"wk {w}"


def feed_back(lg, weeks, pid, start):
    """First week the projection feed has him scoring."""
    for wk in range(start, END_WEEK + 1):
        if sleeper.proj_points(weeks.get(wk, {}).get(pid), lg.score_key) > 0:
            return wk
    return None


def counted_from(lg, weeks, pid, start):
    """First week the tools actually count his points."""
    for wk in range(start, END_WEEK + 1):
        proj = weeks.get(wk, {})
        if sleeper.proj_points(proj.get(pid), lg.score_key) * lg.availability(pid, wk, proj) > 0:
            return wk
    return None


def edit(args, lg):
    data = _read_all()
    if args.clear:
        pid, p = _resolve(args.clear)
        if data.pop(pid, None) is None:
            sys.exit(f"{p['full_name']} has no recorded return week")
        _write_all(data)
        print(f"Cleared return week for {p['full_name']}")
        return
    pid, p = _resolve(args.set)
    if args.season_ending:
        week = SEASON_OVER
    elif args.week is None:
        sys.exit("--set needs --week N or --season-ending")
    else:
        week = args.week
    data[pid] = {
        "name": p["full_name"], "season": lg.season, "return_week": week,
        "note": args.note, "source": args.source,
        "updated": datetime.date.today().isoformat(),
    }
    _write_all(data)
    print(f"Saved: {p['full_name']} expected back {_wk(week)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--league", action="store_true", help="every roster in the league")
    ap.add_argument("--players", help="comma-separated names")
    ap.add_argument("--set", metavar="NAME", help="record an expected return week")
    ap.add_argument("--clear", metavar="NAME", help="remove a recorded return week")
    ap.add_argument("--week", type=int, help="expected return week (with --set)")
    ap.add_argument("--season-ending", action="store_true", help="not back this season (with --set)")
    ap.add_argument("--note", default="")
    ap.add_argument("--source", default="")
    args = ap.parse_args()

    cfg = sleeper.load_config()
    if not cfg.get("league_id"):
        sys.exit("No league configured. Run:  ff.py setup <your_sleeper_username>")
    lg = League(cfg["league_id"])
    if args.set or args.clear:
        edit(args, lg)
        return

    owner = {}
    for r in lg.rosters:
        for p in r.get("players") or []:
            owner[p] = lg.owner_name(r)
    if args.players:
        pool = [_resolve(n)[0] for n in args.players.split(",")]
    else:
        if args.league:
            pool = list(owner)
        else:
            pool = list((lg.roster_for_user(cfg["user_id"]) or {}).get("players") or [])
        pool = [p for p in pool if lg.injury(p) or p in lg.returns]

    start = sleeper.first_open_week(lg.season)
    weeks = load_weeks(lg.season, start, END_WEEK)

    print(f"\nINJURIES | planning from week {start} | {cfg['league_name']}\n")
    if not pool:
        print("  No injured players.")
        return
    own_col = f"{'OWNER':<18}" if args.league else ""
    print(f"  {'PLAYER':<22}{'POS':<4}{'TM':<4}{'STATUS':<8}{'INJURY':<18}"
          f"{own_col}{'FEED BACK':<13}{'COUNTED FROM':<14}RECORDED RETURN")
    for pid in sorted(pool, key=lambda p: (owner.get(p, ""), lg.name(p))):
        p = lg.players.get(pid) or {}
        inj = f"{p.get('injury_body_part') or ''} {p.get('injury_notes') or ''}".strip()
        ret = lg.returns.get(pid)
        rec = f"{_wk(ret['return_week'])}  {ret.get('note', '')}".rstrip() if ret else "-"
        own = f"{owner.get(pid, 'FA')[:17]:<18}" if args.league else ""
        print(f"  {lg.name(pid)[:21]:<22}{lg.pos(pid):<4}{lg.team_of(pid):<4}"
              f"{(lg.injury(pid) or '-'):<8}{inj[:17]:<18}{own}"
              f"{_wk(feed_back(lg, weeks, pid, start)):<13}"
              f"{_wk(counted_from(lg, weeks, pid, start)):<14}{rec}")
    print("\n  FEED BACK = first week the projection feed has him scoring (a bye can push it a week)")
    print("  COUNTED FROM = first week lineup/ROS math counts him, after your recorded return")
    print("  Record one:  injuries.py --set \"Name\" --week N --note \"...\" --source URL")


if __name__ == "__main__":
    main()
