#!/usr/bin/env python3
"""Fantasy football decision helper for Sleeper leagues.

Usage:
  ff.py setup <sleeper_username>   Link your account, pick a league
  ff.py leagues                    Leagues saved at setup (* = active)
  ff.py use <#|name|id>            Switch the active league
  ff.py roster [--week N]          Your roster w/ projections & injuries
  ff.py lineup [--week N]          Optimal start/sit vs your current lineup
  ff.py matchup [--week N]         Your head-to-head matchup projection
  ff.py waivers [--pos RB] [-n 15] Best available free agents + trending
  ff.py trade --give A,B --get C   Evaluate a trade by lineup impact
  ff.py league                     Power rankings across the league
  ff.py activity [--weeks 2]       Recent adds, drops, trades and contested claims
"""
import argparse
import sys

import datetime

import sleeper
from analysis import League, BENCH_SLOTS, trade_window
from ros import END_WEEK, load_weeks, trade_guard, week_values

W = 26  # name column width


def _inj_tag(status):
    if not status:
        return ""
    short = {"Questionable": "Q", "Doubtful": "D", "Out": "OUT", "IR": "IR",
             "PUP": "PUP", "Sus": "SUS", "NA": "NA"}.get(status, status[:3].upper())
    return f" [{short}]"


def _cfg_or_die():
    return sleeper.require_config()


def _line(lg, pid, slot=None):
    nm = lg.name(pid) + _inj_tag(lg.injury(pid)) + (" [LOCKED]" if lg.locked(pid) else "")
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
        print(f"  [{i}] {lg['name']}  |  {lg['total_rosters']} teams, {lg['status']}")
    if len(leagues) == 1:
        pick = 0
        print("\nOnly one league; selecting it.")
    else:
        pick = int(input("\nSelect league #: ")) - 1
    chosen = leagues[pick]
    sleeper.save_config({
        "username": args.username, "user_id": uid,
        "league_id": chosen["league_id"], "league_name": chosen["name"],
        "leagues": [{"league_id": lg["league_id"], "league_name": lg["name"]}
                    for lg in leagues],
    })
    print(f"\nSaved. League: {chosen['name']} ({chosen['league_id']})")
    if len(leagues) > 1:
        print("Switch later with:  ff.py use <#|name>   (or FF_LEAGUE=<#|name> for one command)")


def _saved_leagues():
    cfg = sleeper.require_config()
    return cfg, cfg.get("leagues") or [{"league_id": cfg["league_id"],
                                        "league_name": cfg["league_name"]}]


def cmd_leagues(args):
    cfg, leagues = _saved_leagues()
    for i, lg in enumerate(leagues, 1):
        star = "*" if lg["league_id"] == cfg["league_id"] else " "
        print(f" {star}[{i}] {lg['league_name']}  ({lg['league_id']})")
    if len(leagues) == 1:
        print("\nOnly one league saved. Re-run setup to pick up leagues joined since.")


def cmd_use(args):
    cfg, leagues = _saved_leagues()
    try:
        chosen = sleeper.pick_league(leagues, args.league)
    except ValueError as e:
        sys.exit(str(e))
    stored = sleeper._read_config()
    stored.update(league_id=chosen["league_id"], league_name=chosen["league_name"],
                  leagues=leagues)
    sleeper.save_config(stored)
    print(f"Active league: {chosen['league_name']} ({chosen['league_id']})")


def _my_roster(lg, cfg):
    r = lg.roster_for_user(cfg["user_id"])
    if not r:
        sys.exit("You don't have a roster in this league.")
    return r


def cmd_roster(args):
    cfg = _cfg_or_die()
    lg = League(cfg["league_id"], args.week)
    r = _my_roster(lg, cfg)
    print(f"\n{cfg['league_name']} | Week {lg.week} | {lg.owner_name(r)}")
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


def _current_starters(lg, r):
    """This week's lineup as actually set. The rosters endpoint's `starters`
    field is the season-long default and goes stale as soon as you edit a
    week, so prefer matchups/<week>, which is the authoritative per-week
    lineup. Falls back to the roster field if the week has no matchup."""
    try:
        ms = sleeper.matchups(lg.id, lg.week)
    except Exception:
        return list(r.get("starters") or [])
    mine = next((m for m in ms if m["roster_id"] == r["roster_id"]), None)
    if mine and (mine.get("starters") or None):
        return list(mine["starters"])
    return list(r.get("starters") or [])


def _unlocked_lineup(lg, r):
    """Best lineup you can still set: starters whose games kicked off stay in
    their slots, and bench players whose games started can't come in.
    Returns (slots, total, bench, current_starters, locked_slots)."""
    n_slots = len([s for s in lg.roster_positions if s not in BENCH_SLOTS])
    cur_slots = _current_starters(lg, r)[:n_slots]
    current = [p for p in cur_slots if p and p != "0"]
    fixed = {i: p for i, p in enumerate(cur_slots) if p and p != "0" and lg.locked(p)}
    locked_in = set(fixed.values())
    pool = [p for p in (r.get("players") or []) if not lg.locked(p) or p in locked_in]
    slots, total, bench = lg.optimal_lineup(pool, fixed)
    return slots, total, bench, current, fixed


def cmd_lineup(args):
    cfg = _cfg_or_die()
    lg = League(cfg["league_id"], args.week)
    r = _my_roster(lg, cfg)
    slots, total, bench, current, fixed = _unlocked_lineup(lg, r)

    print(f"\nOPTIMAL LINEUP | Week {lg.week} | {cfg['league_name']}\n")
    if fixed:
        print(f"  ({len(fixed)} starter(s) locked because their games started; kept in place)\n")
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


def _estimate(lg, pid, live):
    """(points so far, points still expected) for one starter. Finished games
    count actual points, unstarted games their projection, and in-progress
    games half of the projection not yet reached; the feed has no game clock."""
    actual = live.get(pid) or 0.0
    status = lg.game(pid)
    if status == "complete":
        return actual, 0.0
    if status in (None, "pre_game"):
        return actual, lg.points(pid)
    return actual, max(0.0, lg.points(pid) - actual) * 0.5


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
        live = m.get("players_points") or {}
        scored = left = 0.0
        to_play = []
        for p in m.get("starters") or []:
            if not p or p == "0":
                continue
            a, rest = _estimate(lg, p, live)
            scored += a
            left += rest
            if lg.game(p) not in (None, "complete"):
                to_play.append(p)
        return lg.owner_name(rr), scored, left, to_play

    sides = [side(mine)] + ([side(opp)] if opp else [])
    print(f"\nWEEK {lg.week} MATCHUP | {cfg['league_name']}\n")
    print(f"  {'TEAM':<24}{'SCORED':>8}{'LEFT (est)':>12}{'FINAL (est)':>13}")
    for nm, scored, left, _ in sides:
        print(f"  {nm:<24}{scored:8.1f}{left:12.1f}{scored + left:13.1f}")
    if not opp:
        print("  (bye week / no opponent)")
        return

    d = sum(sides[0][1:3]) - sum(sides[1][1:3])
    verb = "favored by" if d >= 0 else "underdog by"
    print(f"\n  You are {verb} {abs(d):.1f} estimated points.")
    for label, (_, _, _, to_play) in zip(("you", "them"), sides):
        if to_play:
            names = ", ".join(
                f"{lg.name(p)} ({lg.points(p):.1f}{', live' if lg.game(p) != 'pre_game' else ''})"
                for p in to_play)
            print(f"  Still to play ({label}): {names}")

    _, total, _, current, _ = _unlocked_lineup(lg, r)
    gain = total - sum(lg.points(p) for p in current)
    if gain > 0.05:
        print(f"  Changing your unlocked slots adds +{gain:.1f} projected (see ff.py lineup).")


def _waiver_board(lg, mine, fas, trend, weeks, active=None, capacity=None):
    """Free agents ranked by what adding them does to your optimal lineup.

    Returns (rows, drops). Each row is (gain, ros_net, pts, pid, drop): gain
    is this week's lineup change, ros_net the change summed over `weeks`
    ({week: projections}), and drop the player cut to make room, or None
    while `active` (players holding active spots) is under `capacity`.
    `drops` lists [(ros value, pid)] for the droppable players, least
    valuable first. Free agents with no projection are skipped unless
    they're trending.

    Drops are chosen on rest-of-season value, never this week alone, so an
    injured starter who scores 0 this week isn't the one to cut. Each add is
    tried against the least valuable player overall and the least valuable
    one at its own position (so a kicker replaces your kicker).
    """
    active = list(mine) if active is None else list(active)
    full = capacity is not None and len(active) >= capacity

    def season(pools):
        totals = [0.0] * len(pools)
        for vals in week_values(lg, weeks, pools).values():
            totals = [t + v for t, v in zip(totals, vals)]
        return totals

    base = season([mine])[0]
    drops = []
    if full:
        vals = season([[p for p in mine if p != d] for d in active])
        drops = sorted(zip((base - v for v in vals), active))

    pools, tried = [], []
    for fa in fas:
        if lg.points(fa) <= 0 and fa not in trend:
            continue
        options = [None]
        if full:
            same = next((d for _, d in drops if lg.pos(d) == lg.pos(fa)), None)
            options = list(dict.fromkeys(d for d in (drops[0][1], same) if d))
        for d in options:
            pools.append([p for p in mine if p != d] + [fa])
            tried.append((fa, d))

    best = {}
    for (fa, d), pool, v in zip(tried, pools, season(pools)):
        if fa not in best or v - base > best[fa][0]:
            best[fa] = (v - base, d, pool)

    week_base = lg.starter_value(mine)
    rows = [(lg.starter_value(pool) - week_base, net, lg.points(fa), fa, d)
            for fa, (net, d, pool) in best.items()]
    rows.sort(key=lambda r: (-r[0], -r[1], -r[2]))
    return rows, drops


def faab_bid(left, ros_net, n_weeks):
    """Rough max FAAB bid: weekly lineup gain over 20 points, as a share of
    what's left, capped at half of it. A starting point, not a market price."""
    if left <= 0 or n_weeks <= 0 or ros_net <= 0:
        return 0
    return round(left * min(0.5, ros_net / n_weeks / 20))


def waiver_status(lg, roster):
    """One line on how your claims compete: FAAB left or waiver priority."""
    st = roster.get("settings") or {}
    n = len(lg.rosters)
    if lg.setting("waiver_type") == 2:
        budget = lg.setting("waiver_budget", 100)
        left = budget - st.get("waiver_budget_used", 0)
        return f"FAAB: ${left} of ${budget} left.", left
    pos = st.get("waiver_position")
    if not pos:
        return "Waiver priority unknown.", None
    kind = "reverse standings" if lg.setting("waiver_type") == 1 else "rolling"
    ahead = "nobody claims" if pos == 1 else f"{pos - 1} team{'s' * (pos != 2)} claim"
    return f"Waiver priority: #{pos} of {n} ({kind}); {ahead} before you.", None


def cmd_waivers(args):
    cfg = _cfg_or_die()
    lg = League(cfg["league_id"], args.week)
    r = _my_roster(lg, cfg)
    mine = r.get("players") or []
    active = lg.active_players(r)

    fas = lg.free_agents()
    if args.pos:
        want = {p.strip().upper() for p in args.pos.split(",")}
        fas = [p for p in fas if lg.pos(p) in want]

    start = args.week or sleeper.first_open_week(lg.season)
    weeks = load_weeks(lg.season, start, END_WEEK)
    trend = {t["player_id"]: t["count"] for t in sleeper.trending("add", 24, 200)}
    rows, drops = _waiver_board(lg, mine, fas, trend, weeks, active, lg.active_capacity)
    if args.sort == "ros":
        rows.sort(key=lambda x: (-x[1], -x[0], -x[2]))

    status, faab_left = waiver_status(lg, r)
    n_weeks = len(weeks)
    print(f"\nTOP FREE AGENTS | Week {lg.week} | {cfg['league_name']}")
    ir = len(mine) - len(active)
    print(f"  Roster {len(active)}/{lg.active_capacity} active"
          + (f" (+{ir} IR/taxi)" if ir else "") + f". {status}")
    if drops:
        val, pid = drops[0]
        print(f"  Roster is full, so each add assumes the DROP shown. Least valuable overall: "
              f"{lg.name(pid)} ({val:+.1f} to your lineup, weeks {start}-{END_WEEK}).")
    print(f"  GAIN = change to this week's optimal lineup; ROS = change over weeks "
          f"{start}-{END_WEEK}" + (" | BID = rough max FAAB bid" if faab_left is not None else ""))
    print()
    bid_col = f"{'BID':>5}" if faab_left is not None else ""
    print(f"  {'PLAYER':<{W}} {'POS':<3} {'TM':<3} {'OPP':<7} {'PROJ':>5} {'GAIN':>6} "
          f"{'ROS':>6}{bid_col}  {'DROP':<18} ADDS/24h")
    for gain, net, pts, pid, drop in rows[:args.num]:
        nm = lg.name(pid) + _inj_tag(lg.injury(pid))
        opp = lg.opponent(pid) or ("BYE" if lg.proj.get(pid) else "n/a")
        adds = trend.get(pid)
        a = f"{adds:,}" if adds else "-"
        bid = f"{'$' + str(faab_bid(faab_left, net, n_weeks)):>5}" if faab_left is not None else ""
        dn = lg.name(drop)[:18] if drop else "-"
        print(f"  {nm:<{W}} {lg.pos(pid):<3} {lg.team_of(pid):<3} vs {opp:<4} "
              f"{pts:5.1f} {gain:+6.1f} {net:+6.1f}{bid}  {dn:<18} {a}")

    fa_set = set(fas)
    hot = [(c, p) for p, c in trend.items() if p in fa_set]
    hot.sort(reverse=True)
    if hot:
        print("\n  TRENDING & AVAILABLE (league-wide adds, last 24h)")
        for c, pid in hot[:8]:
            print(f"    {lg.name(pid):<{W}} {lg.pos(pid):<3} {lg.team_of(pid):<3} "
                  f"{c:>8,} adds   proj {lg.points(pid):.1f}")


def _resolve(names, pool=None):
    out = []
    for n in names:
        try:
            out.append(sleeper.resolve_player(n, pool))
        except ValueError as e:
            sys.exit(str(e))
    return out


def cmd_trade(args):
    cfg = _cfg_or_die()
    lg = League(cfg["league_id"], args.week)
    r = _my_roster(lg, cfg)
    mine = list(r.get("players") or [])
    err = trade_guard(lg, args.give.split(",") + args.get.split(","), args.ignore_deadline)
    if err:
        sys.exit(err)

    give = _resolve(args.give.split(","), set(mine))
    get = _resolve(args.get.split(","), lg.rostered_ids())

    for pid in give:
        if pid not in mine:
            sys.exit(f"{lg.name(pid)} is not on your roster.")

    # rosters holding the incoming players; with more than one we can't tell
    # who receives what you give, so partner impact needs a single partner
    partners = [rr for rr in lg.rosters if rr["roster_id"] != r["roster_id"]
                and any(p in (rr.get("players") or []) for p in get)]
    partner = partners[0] if len(partners) == 1 else None

    my_after = [p for p in mine if p not in give] + get
    my_before = lg.starter_value(mine)
    my_after_v = lg.starter_value(my_after)

    print(f"\nTRADE EVALUATION | Week {lg.week} | {cfg['league_name']}")
    print("  " + trade_window(lg.trade_deadline, sleeper.current_week()[0])[1] + "\n")
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
            print("  WIN-WIN: both lineups improve. Strong case to offer this.")
        elif d > 0 and td <= 0:
            print("  GOOD FOR YOU: they lose lineup value, so expect pushback.")
        elif d <= 0 and td > 0:
            print("  BAD FOR YOU: decline unless you're buying long-term upside.")
        else:
            print("  LOSE-LOSE on current-week value.")
    elif len(partners) > 1:
        names = ", ".join(lg.owner_name(rr) for rr in partners)
        print(f"  (incoming players are on {len(partners)} teams: {names}. Partner impact "
              "not evaluated; check one team's players at a time)")
    else:
        print("  (incoming players are free agents / unrostered)")
    print("\n  Note: single-week lineup impact only. Weigh schedule, bye weeks,")
    print("  injury recovery and rest-of-season upside before deciding (ros.py).")
    note = lg.trade_note()
    if note:
        print(f"  {note}")


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
    print(f"\nPOWER RANKINGS | Week {lg.week} | {cfg['league_name']}")
    print(f"(by projected optimal lineup this week)\n")
    print(f"  {'#':<3}{'TEAM':<26}{'PROJ':>6}{'REC':>8}{'PTS':>9}")
    for i, (proj, nm, w, l, pts, me) in enumerate(rows, 1):
        star = " <- you" if me else ""
        print(f"  {i:<3}{nm:<26}{proj:6.1f}{f'{w}-{l}':>8}{pts:9.1f}{star}")


def describe_tx(lg, t, team):
    """One line for a completed transaction. `team` maps roster_id -> name."""
    kind = {"trade": "TRADE", "waiver": "WAIVER", "free_agent": "FA",
            "commissioner": "COMM"}.get(t.get("type"), (t.get("type") or "?").upper())
    adds, drops = t.get("adds") or {}, t.get("drops") or {}
    if t.get("type") == "trade":
        got = {}
        for pid, rid in adds.items():
            got.setdefault(rid, []).append(lg.name(pid))
        for pk in t.get("draft_picks") or []:
            got.setdefault(pk["owner_id"], []).append(
                f"{pk['season']} round {pk['round']} pick (orig. {team.get(pk['roster_id'], '?')})")
        sides = "; ".join(f"{team.get(rid, '?')} gets {', '.join(v)}" for rid, v in got.items())
        return f"{kind:<7}{sides}"
    rid = (t.get("roster_ids") or [None])[0]
    parts = [f"+{lg.name(p)}" for p in adds] + [f"-{lg.name(p)}" for p in drops]
    bid = (t.get("settings") or {}).get("waiver_bid")
    if bid is not None:
        parts.append(f"(${bid})")
    return f"{kind:<7}{team.get(rid, '?')}: {' '.join(parts)}"


def contested_claims(txs):
    """{pid: (claims, winning roster_id or None)} for waiver targets claimed
    by more than one team."""
    out = {}
    for t in txs:
        if t.get("type") != "waiver":
            continue
        for pid, rid in (t.get("adds") or {}).items():
            n, won = out.get(pid, (0, None))
            out[pid] = (n + 1, rid if t.get("status") == "complete" else won)
    return {p: v for p, v in out.items() if v[0] > 1}


def cmd_activity(args):
    cfg = _cfg_or_die()
    lg = League(cfg["league_id"])
    cw = sleeper.current_week()[0]
    weeks = range(max(1, cw - args.weeks + 1), cw + 1)
    txs = []
    for wk in weeks:
        for t in sleeper.transactions(lg.id, wk):
            txs.append(dict(t, week=wk))
    txs.sort(key=lambda t: -(t.get("status_updated") or 0))
    team = {r["roster_id"]: lg.owner_name(r) for r in lg.rosters}
    done = [t for t in txs if t.get("status") == "complete"]

    print(f"\nLEAGUE ACTIVITY | weeks {weeks[0]}-{weeks[-1]} | {cfg['league_name']}\n")
    if not done:
        print("  No completed transactions.")
    for t in done[:args.num]:
        when = datetime.datetime.fromtimestamp((t.get("status_updated") or 0) / 1000)
        print(f"  wk {t['week']:<3}{when:%a %b %d}  {describe_tx(lg, t, team)}")
    if len(done) > args.num:
        print(f"  ... {len(done) - args.num} more (raise -n)")

    contested = contested_claims(txs)
    if contested:
        print("\n  CONTESTED WAIVER CLAIMS (what the league is chasing)")
        for pid, (n, won) in sorted(contested.items(), key=lambda x: -x[1][0]):
            print(f"    {lg.name(pid):<{W}} {n} claims, won by {team.get(won, 'nobody')}")

    rostered = lg.rostered_ids()
    dropped = {p for t in done for p in (t.get("drops") or {}) if p not in rostered}
    back = sorted((p for p in dropped if lg.points(p) > 0), key=lambda p: -lg.points(p))
    if back:
        print(f"\n  DROPPED AND STILL AVAILABLE (week {lg.week} projection)")
        for pid in back[:8]:
            print(f"    {lg.name(pid):<{W}} {lg.pos(pid):<3} {lg.team_of(pid):<3} "
                  f"{lg.points(pid):5.1f}")
    print("\n  Injuries on other rosters (trade triggers):  injuries.py --league")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup"); s.add_argument("username"); s.set_defaults(f=cmd_setup)
    s = sub.add_parser("leagues"); s.set_defaults(f=cmd_leagues)
    s = sub.add_parser("use"); s.add_argument("league"); s.set_defaults(f=cmd_use)
    s = sub.add_parser("activity"); s.add_argument("--weeks", type=int, default=2)
    s.add_argument("-n", "--num", type=int, default=25); s.set_defaults(f=cmd_activity)
    for nm, fn in (("roster", cmd_roster), ("lineup", cmd_lineup),
                   ("matchup", cmd_matchup), ("league", cmd_league)):
        s = sub.add_parser(nm); s.add_argument("--week", type=int); s.set_defaults(f=fn)
    s = sub.add_parser("waivers"); s.add_argument("--week", type=int)
    s.add_argument("--pos"); s.add_argument("-n", "--num", type=int, default=15)
    s.add_argument("--sort", choices=("week", "ros"), default="week",
                   help="rank by this week's gain (default) or rest-of-season gain")
    s.set_defaults(f=cmd_waivers)
    s = sub.add_parser("trade"); s.add_argument("--week", type=int)
    s.add_argument("--give", required=True); s.add_argument("--get", required=True)
    s.add_argument("--ignore-deadline", action="store_true")
    s.set_defaults(f=cmd_trade)

    args = ap.parse_args()
    args.f(args)


if __name__ == "__main__":
    main()
