#!/usr/bin/env python3
"""Fantasy football decision helper for Sleeper leagues.

Usage:
  ff.py setup <sleeper_username>   Link your account, pick a league
  ff.py roster [--week N]          Your roster w/ projections & injuries
  ff.py lineup [--week N]          Optimal start/sit vs your current lineup
  ff.py matchup [--week N]         Your head-to-head matchup projection
  ff.py waivers [--pos RB] [-n 15] Best available free agents + trending
  ff.py trade --give A,B --get C   Evaluate a trade by lineup impact
  ff.py league                     Power rankings across the league
"""
import argparse
import sys

import sleeper
from analysis import League, injury_multiplier, BENCH_SLOTS

W = 26  # name column width


def _inj_tag(status):
    if not status:
        return ""
    short = {"Questionable": "Q", "Doubtful": "D", "Out": "OUT", "IR": "IR",
             "PUP": "PUP", "Sus": "SUS", "NA": "NA"}.get(status, status[:3].upper())
    return f" [{short}]"


def _cfg_or_die():
    cfg = sleeper.load_config()
    if not cfg.get("league_id"):
        sys.exit("No league configured. Run:  ff.py setup <your_sleeper_username>")
    return cfg


def _line(lg, pid, slot=None):
    nm = lg.name(pid) + _inj_tag(lg.injury(pid))
    opp = lg.opponent(pid) or ("BYE" if lg.proj.get(pid) else "n/a")
    pts = lg.points(pid)
    raw = lg.points(pid, adjust=False)
    flag = " *" if raw > 0 and pts < raw else ""
    pre = f"{slot:<11}" if slot else ""
    return f"  {pre}{nm:<{W}} {lg.pos(pid):<3} {lg.team_of(pid):<3} vs {opp:<4} {pts:6.1f}{flag}"


# ---------- commands ----------

def cmd_setup(args):
    u = sleeper.user(args.username)
    if not u:
        sys.exit(f"No Sleeper user '{args.username}'")
    uid = u["user_id"]
    leagues = sleeper.user_leagues(uid)
    if not leagues:
        sys.exit(f"User {args.username} has no NFL leagues this season.")
    print(f"User: {u['display_name']}  (id {uid})\n")
    for i, lg in enumerate(leagues, 1):
        print(f"  [{i}] {lg['name']}  —  {lg['total_rosters']} teams, {lg['status']}")
    if len(leagues) == 1:
        pick = 0
        print("\nOnly one league; selecting it.")
    else:
        pick = int(input("\nSelect league #: ")) - 1
    chosen = leagues[pick]
    sleeper.save_config({
        "username": args.username, "user_id": uid,
        "league_id": chosen["league_id"], "league_name": chosen["name"],
    })
    print(f"\nSaved. League: {chosen['name']} ({chosen['league_id']})")


def _my_roster(lg, cfg):
    r = lg.roster_for_user(cfg["user_id"])
    if not r:
        sys.exit("You don't have a roster in this league.")
    return r


def cmd_roster(args):
    cfg = _cfg_or_die()
    lg = League(cfg["league_id"], args.week)
    r = _my_roster(lg, cfg)
    print(f"\n{cfg['league_name']} — Week {lg.week} — {lg.owner_name(r)}")
    print(f"Scoring: {lg.score_key}   Record: {r['settings'].get('wins',0)}-{r['settings'].get('losses',0)}\n")
    ids = r.get("players") or []
    by_pos = {}
    for pid in ids:
        by_pos.setdefault(lg.pos(pid), []).append(pid)
    for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
        group = sorted(by_pos.get(pos, []), key=lambda p: -lg.points(p))
        if not group:
            continue
        print(f"{pos}")
        for pid in group:
            print(_line(lg, pid))
        print()
    print("  * = projection discounted for injury risk")


def cmd_lineup(args):
    cfg = _cfg_or_die()
    lg = League(cfg["league_id"], args.week)
    r = _my_roster(lg, cfg)
    ids = r.get("players") or []
    current = [p for p in (r.get("starters") or []) if p and p != "0"]
    slots, total, bench = lg.optimal_lineup(ids)

    print(f"\nOPTIMAL LINEUP — Week {lg.week} — {cfg['league_name']}\n")
    for slot, pid, pts in slots:
        print(_line(lg, pid, slot) if pid else f"  {slot:<11}(empty)")
    print(f"\n  {'PROJECTED TOTAL':<38}{total:6.1f}")

    cur_pts = sum(lg.points(p) for p in current)
    print(f"  {'Current lineup':<38}{cur_pts:6.1f}")

    opt_ids = {pid for _, pid, _ in slots if pid}
    cur_ids = set(current)
    add, drop = opt_ids - cur_ids, cur_ids - opt_ids
    if not add and not drop:
        print("\n  Your lineup is already optimal.")
    else:
        print(f"\n  CHANGES  (+{total - cur_pts:.1f} pts)")
        for pid in sorted(add, key=lambda p: -lg.points(p)):
            print(f"    START  {lg.name(pid)} ({lg.pos(pid)}, {lg.points(pid):.1f})")
        for pid in sorted(drop, key=lambda p: -lg.points(p)):
            print(f"    BENCH  {lg.name(pid)}{_inj_tag(lg.injury(pid))} "
                  f"({lg.pos(pid)}, {lg.points(pid):.1f})")

    risky = [p for p in opt_ids if lg.injury(p)]
    if risky:
        print("\n  INJURY WATCH")
        for pid in risky:
            pl = lg.players.get(pid, {})
            note = pl.get("injury_body_part") or ""
            print(f"    {lg.name(pid)}: {lg.injury(pid)} {note}".rstrip())
    if bench:
        print("\n  BENCH")
        for pid in bench[:8]:
            print(_line(lg, pid))


def cmd_matchup(args):
    cfg = _cfg_or_die()
    lg = League(cfg["league_id"], args.week)
    r = _my_roster(lg, cfg)
    ms = sleeper.matchups(cfg["league_id"], lg.week)
    mine = next((m for m in ms if m["roster_id"] == r["roster_id"]), None)
    if not mine or mine.get("matchup_id") is None:
        sys.exit("No matchup found for this week.")
    opp = next((m for m in ms if m.get("matchup_id") == mine["matchup_id"]
                and m["roster_id"] != r["roster_id"]), None)
    rmap = {x["roster_id"]: x for x in lg.rosters}

    def side(m):
        rr = rmap[m["roster_id"]]
        best = lg.starter_value(rr.get("players") or [])
        cur = sum(lg.points(p) for p in (m.get("starters") or []) if p and p != "0")
        return lg.owner_name(rr), cur, best

    n1, c1, b1 = side(mine)
    print(f"\nWEEK {lg.week} MATCHUP — {cfg['league_name']}\n")
    print(f"  {n1:<24} {c1:6.1f}  (optimal {b1:.1f})")
    if opp:
        n2, c2, b2 = side(opp)
        print(f"  {n2:<24} {c2:6.1f}  (optimal {b2:.1f})")
        d = c1 - c2
        verb = "favored by" if d >= 0 else "underdog by"
        print(f"\n  You are {verb} {abs(d):.1f} projected points.")
        if d < 0 and b1 > c2:
            print(f"  Optimizing your lineup flips this (+{b1 - c1:.1f} available).")
    else:
        print("  (bye week / no opponent)")


def cmd_waivers(args):
    cfg = _cfg_or_die()
    lg = League(cfg["league_id"], args.week)
    r = _my_roster(lg, cfg)
    mine = r.get("players") or []
    base = lg.starter_value(mine)

    fas = lg.free_agents()
    if args.pos:
        want = {p.strip().upper() for p in args.pos.split(",")}
        fas = [p for p in fas if lg.pos(p) in want]

    trend = {t["player_id"]: t["count"] for t in sleeper.trending("add", 24, 200)}

    scored = []
    for pid in fas:
        pts = lg.points(pid)
        if pts <= 0 and pid not in trend:
            continue
        gain = lg.starter_value(mine + [pid]) - base
        scored.append((gain, pts, pid))
    scored.sort(key=lambda x: (-x[0], -x[1]))

    print(f"\nTOP FREE AGENTS — Week {lg.week} — {cfg['league_name']}")
    print(f"(gain = added points to your optimal starting lineup)\n")
    print(f"  {'PLAYER':<{W}} {'POS':<3} {'TM':<3} {'OPP':<7} {'PROJ':>5} {'GAIN':>6}  ADDS/24h")
    for gain, pts, pid in scored[:args.num]:
        nm = lg.name(pid) + _inj_tag(lg.injury(pid))
        opp = lg.opponent(pid) or ("BYE" if lg.proj.get(pid) else "n/a")
        adds = trend.get(pid)
        a = f"{adds:,}" if adds else "-"
        g = f"+{gain:.1f}" if gain > 0 else "  -"
        print(f"  {nm:<{W}} {lg.pos(pid):<3} {lg.team_of(pid):<3} vs {opp:<4} "
              f"{pts:5.1f} {g:>6}  {a}")

    hot = [(c, p) for p, c in trend.items() if p in set(fas)]
    hot.sort(reverse=True)
    if hot:
        print("\n  TRENDING & AVAILABLE (league-wide adds, last 24h)")
        for c, pid in hot[:8]:
            print(f"    {lg.name(pid):<{W}} {lg.pos(pid):<3} {lg.team_of(pid):<3} "
                  f"{c:>8,} adds   proj {lg.points(pid):.1f}")


def _resolve(lg, names):
    out = []
    for n in names:
        hits = sleeper.find_player(n)
        hits = [h for h in hits if h[0] in lg.rostered_ids() or True]
        if not hits:
            sys.exit(f"No player matching '{n}'")
        out.append(hits[0][0])
    return out


def cmd_trade(args):
    cfg = _cfg_or_die()
    lg = League(cfg["league_id"], args.week)
    r = _my_roster(lg, cfg)
    mine = list(r.get("players") or [])

    give = _resolve(lg, args.give.split(","))
    get = _resolve(lg, args.get.split(","))

    for pid in give:
        if pid not in mine:
            sys.exit(f"{lg.name(pid)} is not on your roster.")

    # locate the partner roster holding the incoming players
    partner = None
    for rr in lg.rosters:
        if rr["roster_id"] == r["roster_id"]:
            continue
        if any(p in (rr.get("players") or []) for p in get):
            partner = rr
            break

    my_after = [p for p in mine if p not in give] + get
    my_before = lg.starter_value(mine)
    my_after_v = lg.starter_value(my_after)

    print(f"\nTRADE EVALUATION — Week {lg.week} — {cfg['league_name']}\n")
    print("  YOU GIVE")
    for pid in give:
        print(_line(lg, pid))
    print("  YOU GET")
    for pid in get:
        print(_line(lg, pid))

    d = my_after_v - my_before
    print(f"\n  Your starting lineup: {my_before:.1f} -> {my_after_v:.1f}  ({d:+.1f})")

    if partner:
        theirs = list(partner.get("players") or [])
        t_before = lg.starter_value(theirs)
        t_after = lg.starter_value([p for p in theirs if p not in get] + give)
        td = t_after - t_before
        print(f"  {lg.owner_name(partner)}: {t_before:.1f} -> {t_after:.1f}  ({td:+.1f})")
        print()
        if d > 0 and td > 0:
            print("  WIN-WIN — both lineups improve. Strong case to offer this.")
        elif d > 0 and td <= 0:
            print("  GOOD FOR YOU — they lose lineup value, so expect pushback.")
        elif d <= 0 and td > 0:
            print("  BAD FOR YOU — decline unless you're buying long-term upside.")
        else:
            print("  LOSE-LOSE on current-week value.")
    else:
        print("  (incoming players are free agents / unrostered)")
    print("\n  Note: single-week lineup impact only. Weigh schedule, bye weeks,")
    print("  injury recovery and rest-of-season upside before deciding.")


def cmd_league(args):
    cfg = _cfg_or_die()
    lg = League(cfg["league_id"], args.week)
    rows = []
    for r in lg.rosters:
        ids = r.get("players") or []
        s = r.get("settings", {})
        rows.append((lg.starter_value(ids), lg.owner_name(r),
                     s.get("wins", 0), s.get("losses", 0),
                     s.get("fpts", 0) + s.get("fpts_decimal", 0) / 100,
                     r.get("owner_id") == cfg["user_id"]))
    rows.sort(reverse=True)
    print(f"\nPOWER RANKINGS — Week {lg.week} — {cfg['league_name']}")
    print(f"(by projected optimal lineup this week)\n")
    print(f"  {'#':<3}{'TEAM':<26}{'PROJ':>6}{'REC':>8}{'PTS':>9}")
    for i, (proj, nm, w, l, pts, me) in enumerate(rows, 1):
        star = " <- you" if me else ""
        print(f"  {i:<3}{nm:<26}{proj:6.1f}{f'{w}-{l}':>8}{pts:9.1f}{star}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup"); s.add_argument("username"); s.set_defaults(f=cmd_setup)
    for nm, fn in (("roster", cmd_roster), ("lineup", cmd_lineup),
                   ("matchup", cmd_matchup), ("league", cmd_league)):
        s = sub.add_parser(nm); s.add_argument("--week", type=int); s.set_defaults(f=fn)
    s = sub.add_parser("waivers"); s.add_argument("--week", type=int)
    s.add_argument("--pos"); s.add_argument("-n", "--num", type=int, default=15)
    s.set_defaults(f=cmd_waivers)
    s = sub.add_parser("trade"); s.add_argument("--week", type=int)
    s.add_argument("--give", required=True); s.add_argument("--get", required=True)
    s.set_defaults(f=cmd_trade)

    args = ap.parse_args()
    args.f(args)


if __name__ == "__main__":
    main()
