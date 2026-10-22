"""Offline tests for playoff odds, trade guards, waivers with drops, bye weeks,
projection accuracy, league activity and multi-league config. No network.

Run from the repo root:  python3 -m unittest discover tests
"""
import argparse
import contextlib
import io
import json
import math
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import accuracy  # noqa: E402
import ff  # noqa: E402
import needs  # noqa: E402
import playoffs  # noqa: E402
import ros  # noqa: E402
import sleeper  # noqa: E402
from analysis import League, looks_like_pick, trade_window  # noqa: E402
from test_core import make_league, player, row  # noqa: E402


class SettingsTests(unittest.TestCase):
    def test_reads_league_settings(self):
        lg = make_league({}, settings={"playoff_week_start": 14, "trade_deadline": 99,
                                       "pick_trading": 1, "max_keepers": 0})
        self.assertEqual((lg.playoff_start, lg.reg_end), (14, 13))
        self.assertIsNone(lg.trade_deadline)  # 99 = no deadline
        self.assertIn("draft picks", lg.trade_note())
        self.assertNotIn("keeper", lg.trade_note())

    def test_defaults(self):
        lg = make_league({})
        self.assertEqual((lg.playoff_start, lg.reg_end), (15, 14))
        self.assertIsNone(lg.trade_deadline)
        self.assertIsNone(lg.trade_note())

    def test_regular_season_split_follows_playoff_start(self):
        lg = make_league({"a": player("a", "RB")}, settings={"playoff_week_start": 14})
        r = object.__new__(ros.ROS)
        r.lg, r.start, r.end, r.playoff_start = lg, 12, 15, lg.playoff_start
        r.weeks = {wk: {"a": row(10)} for wk in range(12, 16)}
        # REG is weeks 12-13 and PLYF 14-15; a hardcoded week-14 cutoff
        # would count week 14 in both
        self.assertTrue(r.line("a").endswith("    20.0    20.0"))


class CapacityTests(unittest.TestCase):
    def test_ir_and_taxi_dont_take_active_spots(self):
        lg = make_league({}, positions=["QB", "RB", "BN", "BN", "IR"])
        self.assertEqual(lg.active_capacity, 4)
        roster = {"players": ["a", "b", "c", "d"], "reserve": ["c"], "taxi": ["d"]}
        self.assertEqual(League.active_players(roster), ["a", "b"])


class TradeGuardTests(unittest.TestCase):
    def test_trade_window(self):
        self.assertEqual(trade_window(None, 12)[0], True)
        ok, msg = trade_window(11, 7)
        self.assertTrue(ok)
        self.assertIn("4 weeks away", msg)
        self.assertIn("1 week away", trade_window(11, 10)[1])
        ok, msg = trade_window(11, 11)
        self.assertTrue(ok)
        self.assertIn("THIS week", msg)
        self.assertEqual(trade_window(11, 12)[0], False)

    def test_pick_detection(self):
        for name in ("2027 1st", "Round 2", "3rd round pick", "2026 R1 pick", "5th"):
            self.assertTrue(looks_like_pick(name), name)
        for name in ("George Pickens", "Josh Allen", "Brandon Aubrey", "Amon-Ra St. Brown",
                     "Tony Pollard"):
            self.assertFalse(looks_like_pick(name), name)

    def test_guard(self):
        lg = make_league({}, settings={"trade_deadline": 11})
        self.assertIsNone(ros.trade_guard(lg, ["Josh Allen"], week=11))
        self.assertIn("passed", ros.trade_guard(lg, ["Josh Allen"], week=12))
        self.assertIsNone(ros.trade_guard(lg, ["Josh Allen"], ignore_deadline=True, week=12))
        self.assertIn("Draft picks", ros.trade_guard(lg, ["Josh Allen", " 2027 1st"], week=3))


class WaiverDropTests(unittest.TestCase):
    def test_drop_is_chosen_on_rest_of_season_value(self):
        players = {"q1": player("q1", "QB"), "r1": player("r1", "RB"),
                   "r2": player("r2", "RB", status="IR"), "r4": player("r4", "RB"),
                   "k1": player("k1", "K"), "r3": player("r3", "RB")}
        w1 = {"q1": row(20), "r1": row(10), "r2": row(0), "r4": row(9), "k1": row(5), "r3": row(11)}
        w2 = {"q1": row(20), "r1": row(10), "r2": row(12), "r4": row(3), "k1": row(5), "r3": row(11)}
        lg = make_league(players, proj=w1, positions=["QB", "RB", "K", "BN", "BN"],
                         week=1, horizon=1)
        mine = ["q1", "r1", "r2", "r4", "k1"]
        rows, drops = ff._waiver_board(lg, mine, ["r3"], {}, {1: w1, 2: w2},
                                       active=mine, capacity=5)
        # r2 scores 0 this week (IR) but returns in week 2, so r4 is the cut
        self.assertEqual(drops[0], (0, "r4"))
        self.assertEqual(dict((p, v) for v, p in drops)["r2"], 2)
        self.assertEqual(rows, [(1, 1, 11, "r3", "r4")])

    def test_same_position_drop_can_win(self):
        players = {"q1": player("q1", "QB"), "b1": player("b1", "QB"),
                   "k1": player("k1", "K"), "k2": player("k2", "K")}
        w1 = {"q1": row(20), "b1": row(15), "k1": row(5), "k2": row(7)}
        w2 = {"q1": row(0), "b1": row(15), "k1": row(5), "k2": row(7)}  # q1 on bye
        lg = make_league(players, proj=w1, positions=["QB", "K", "BN"], week=1, horizon=1)
        mine = ["q1", "b1", "k1"]
        rows, drops = ff._waiver_board(lg, mine, ["k2"], {}, {1: w1, 2: w2},
                                       active=mine, capacity=3)
        # q1 is least valuable overall (b1 covers him), but cutting him for a
        # kicker loses points; replacing k1 gains 2 a week
        self.assertEqual(drops[0][1], "q1")
        self.assertEqual(rows, [(2, 4, 7, "k2", "k1")])

    def test_open_spot_means_no_drop(self):
        lg = make_league({"q1": player("q1", "QB"), "k2": player("k2", "K")},
                         proj={"q1": row(20), "k2": row(7)}, positions=["QB", "K", "BN"])
        rows, drops = ff._waiver_board(lg, ["q1"], ["k2"], {}, {2: lg.proj},
                                       active=["q1"], capacity=3)
        self.assertEqual((rows, drops), ([(7, 7, 7, "k2", None)], []))

    def test_faab_bid(self):
        self.assertEqual(ff.faab_bid(100, 40, 10), 20)   # 4 pts/week -> 20%
        self.assertEqual(ff.faab_bid(100, 500, 10), 50)  # capped at half
        self.assertEqual(ff.faab_bid(100, -3, 10), 0)
        self.assertEqual(ff.faab_bid(0, 40, 10), 0)

    def test_waiver_status(self):
        rosters = [{"roster_id": i} for i in range(12)]
        lg = make_league({}, settings={"waiver_type": 0}, rosters=rosters)
        msg, left = ff.waiver_status(lg, {"settings": {"waiver_position": 6}})
        self.assertIn("#6 of 12 (rolling); 5 teams claim", msg)
        self.assertIsNone(left)
        lg = make_league({}, settings={"waiver_type": 2, "waiver_budget": 100}, rosters=rosters)
        msg, left = ff.waiver_status(lg, {"settings": {"waiver_budget_used": 30}})
        self.assertEqual((msg, left), ("FAAB: $70 of $100 left.", 70))


class ByeWeekTests(unittest.TestCase):
    def test_flags_regulars_on_bye_and_open_slots(self):
        lg = make_league({"q1": player("q1", "QB", team="KC"), "r1": player("r1", "RB", team="BUF"),
                          "r2": player("r2", "RB", team="NYJ")},
                         positions=["QB", "RB", "BN"], week=1, horizon=1)
        weeks = {1: {"q1": row(20), "r1": row(10), "r2": row(5)},
                 2: {"q1": row(20), "r2": row(5)},
                 3: {"r1": row(10), "r2": row(5)}}
        playing = {1: {"KC", "BUF", "NYJ"}, 2: {"KC", "NYJ"}, 3: {"BUF", "NYJ"}}
        rows, regulars = ros.bye_weeks(lg, weeks, ["q1", "r1", "r2"], playing)
        self.assertEqual(sorted(regulars), ["q1", "r1"])
        self.assertEqual(rows, [(1, 30, 0, []), (2, 25, 0, ["r1"]), (3, 10, 1, ["q1"])])


class PlayoffTests(unittest.TestCase):
    def test_win_prob(self):
        self.assertEqual(playoffs.win_prob(0, 25), 0.5)
        self.assertAlmostEqual(playoffs.win_prob(10, 25) + playoffs.win_prob(-10, 25), 1.0)
        # a one-sd edge in the score difference is ~84%
        self.assertAlmostEqual(playoffs.win_prob(25 * math.sqrt(2), 25), 0.8413, places=3)

    def test_team_week_sd(self):
        sd = playoffs.team_week_sd({1: [100, 110, 120], 2: [90, 90]})
        self.assertAlmostEqual(sd, math.sqrt(200 / 3))
        self.assertEqual(playoffs.team_week_sd({1: [100]}), playoffs.DEFAULT_SD)

    def test_points_for_breaks_ties(self):
        # 4 is listed before 3, so only the points-for tiebreak puts 3 ahead
        standings = {1: (5, 800), 2: (0, 700), 4: (3, 850), 3: (3, 900)}
        res = playoffs.simulate(standings, [], {}, 25, playoff_teams=2, sims=10, seed=0)
        self.assertEqual({r: res[r]["playoffs"] for r in standings}, {1: 1, 2: 0, 3: 1, 4: 0})
        self.assertEqual([res[r]["seed"] for r in (1, 3, 4, 2)], [1, 2, 3, 4])

    def test_remaining_games_change_seeding(self):
        standings = {1: (5, 800), 2: (0, 700), 3: (3, 900), 4: (3, 850)}
        means = {(7, 3): 100, (7, 4): 200}
        res = playoffs.simulate(standings, [(7, 3, 4)], means, 0.001, playoff_teams=2,
                                sims=50, seed=0)
        self.assertEqual(res[4]["playoffs"], 1)
        self.assertEqual(res[4]["wins"], 4)
        self.assertEqual(res[3]["seed"], 3)

    def test_byes_and_coin_flips(self):
        standings = {r: (0, 0) for r in range(1, 7)}
        games = [(1, 1, 2), (1, 3, 4), (1, 5, 6)]
        means = {(1, r): 100 for r in range(1, 7)}
        res = playoffs.simulate(standings, games, means, 20, playoff_teams=6, sims=4000, seed=0)
        self.assertTrue(all(res[r]["playoffs"] == 1 for r in standings))
        self.assertAlmostEqual(sum(res[r]["bye"] for r in standings), 2)
        self.assertAlmostEqual(res[1]["wins"], 0.5, delta=0.04)

    def test_live_sd_override(self):
        standings = {1: (0, 0), 2: (0, 0)}
        means = {(1, 1): 110, (1, 2): 100}
        res = playoffs.simulate(standings, [(1, 1, 2)], means, 30, playoff_teams=1,
                                sims=200, seed=0, sds={(1, 1): 0.001, (1, 2): 0.001})
        self.assertEqual(res[1]["playoffs"], 1)  # settled game, no noise left

    def test_outlook(self):
        self.assertEqual(playoffs.outlook(0.85)[0], "CONTENDER")
        self.assertEqual(playoffs.outlook(0.5)[0], "BUBBLE")
        self.assertEqual(playoffs.outlook(0.1)[0], "LONG SHOT")


class AccuracyTests(unittest.TestCase):
    def test_compare(self):
        proj = {1: {"a": row(10), "b": row(3), "c": row(8), "x": row(9)},
                2: {"a": row(10)}}  # week 2 has no stats yet: skipped
        stats = {1: {"a": {"pts_ppr": 14}}}
        pos = {"a": "QB", "b": "RB", "c": "RB", "x": "LB"}.get
        rows = accuracy.compare(proj, stats, "pts_ppr", pos, min_proj=5)
        # b below the cutoff, x not an offensive position, c missing = 0
        self.assertEqual(sorted(rows), [("QB", 10, 14.0), ("RB", 8, 0.0)])

    def test_summarize(self):
        s = accuracy.summarize([("QB", 10, 14.0), ("RB", 8, 0.0)])
        a = s["ALL"]
        self.assertEqual((a["n"], a["proj"], a["actual"], a["bias"], a["mae"]), (2, 9, 7, -2, 6))
        self.assertAlmostEqual(a["sd"], math.sqrt(72))
        self.assertAlmostEqual(a["corr"], 1.0)
        self.assertTrue(math.isnan(s["QB"]["corr"]))
        self.assertEqual(s["QB"]["sd"], 0.0)

    def test_noise_floor_and_efficiency(self):
        self.assertEqual(accuracy.noise_floor(5, 8), 20)
        self.assertEqual(accuracy.noise_floor(5, 0), 0)
        rosters = [{"roster_id": 1, "settings": {"fpts": 800, "fpts_decimal": 50, "ppts": 900}},
                   {"roster_id": 2, "settings": {"fpts": 700}}]
        self.assertEqual(accuracy.efficiency(rosters), [(1, 800.5, 900)])


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.lg = make_league({"a": player("Al", "WR"), "b": player("Bo", "RB")})
        self.team = {1: "T1", 2: "T2"}

    def test_trade_with_picks(self):
        t = {"type": "trade", "adds": {"a": 1, "b": 2},
             "draft_picks": [{"season": "2027", "round": 1, "roster_id": 2, "owner_id": 1}]}
        self.assertEqual(ff.describe_tx(self.lg, t, self.team),
                         "TRADE  T1 gets Al, 2027 round 1 pick (orig. T2); T2 gets Bo")

    def test_waiver_with_bid(self):
        t = {"type": "waiver", "roster_ids": [1], "adds": {"a": 1}, "drops": {"b": 1},
             "settings": {"waiver_bid": 12}}
        self.assertEqual(ff.describe_tx(self.lg, t, self.team), "WAIVER T1: +Al -Bo ($12)")

    def test_contested_claims(self):
        txs = [{"type": "waiver", "status": "failed", "adds": {"a": 4}},
               {"type": "waiver", "status": "complete", "adds": {"a": 3}},
               {"type": "waiver", "status": "complete", "adds": {"b": 1}},
               {"type": "free_agent", "status": "complete", "adds": {"b": 2}}]
        self.assertEqual(ff.contested_claims(txs), {"a": (2, 3)})


class NeedsSlotTests(unittest.TestCase):
    def test_uncounted_slots(self):
        slots = ["QB", "RB", "WR", "TE", "FLEX", "FLEX", "SUPER_FLEX", "K", "DEF", "BN", "IR"]
        self.assertEqual(needs.uncounted_slots(slots), "FLEX x2, K, DEF")
        self.assertEqual(needs.uncounted_slots(["QB", "RB", "BN"]), "")


class ConfigTests(unittest.TestCase):
    LEAGUES = [{"league_id": "111", "league_name": "Main League"},
               {"league_id": "222", "league_name": "Work League"},
               {"league_id": "333", "league_name": "Work Dynasty"}]

    def setUp(self):
        d = tempfile.mkdtemp()
        self.path = os.path.join(d, "config.json")
        with open(self.path, "w") as f:
            json.dump({"username": "u", "user_id": "9", "league_id": "111",
                       "league_name": "Main League", "leagues": self.LEAGUES}, f)
        for patcher in (mock.patch.object(sleeper, "CONFIG", self.path),
                        mock.patch.dict(os.environ, {}, clear=False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("FF_LEAGUE", None)

    def test_pick_league(self):
        pick = lambda k: sleeper.pick_league(self.LEAGUES, k)["league_id"]  # noqa: E731
        self.assertEqual(pick("2"), "222")
        self.assertEqual(pick("333"), "333")
        self.assertEqual(pick("dynasty"), "333")
        with self.assertRaises(ValueError):
            pick("work")  # two matches
        with self.assertRaises(ValueError):
            pick("nothing")

    def test_env_override_is_per_command(self):
        os.environ["FF_LEAGUE"] = "dynasty"
        self.assertEqual(sleeper.load_config()["league_id"], "333")
        os.environ.pop("FF_LEAGUE")
        self.assertEqual(sleeper.load_config()["league_id"], "111")

    def test_bad_override_exits(self):
        os.environ["FF_LEAGUE"] = "nope"
        with self.assertRaises(SystemExit):
            sleeper.require_config()

    def test_old_single_league_config_still_works(self):
        with open(self.path, "w") as f:
            json.dump({"username": "u", "user_id": "9", "league_id": "111",
                       "league_name": "Main League"}, f)
        os.environ["FF_LEAGUE"] = "main"
        self.assertEqual(sleeper.require_config()["league_id"], "111")

    def test_use_switches_active_league(self):
        with contextlib.redirect_stdout(io.StringIO()):
            ff.cmd_use(argparse.Namespace(league="Work League"))
        with open(self.path) as f:
            saved = json.load(f)
        self.assertEqual((saved["league_id"], saved["league_name"]), ("222", "Work League"))
        self.assertEqual(saved["leagues"], self.LEAGUES)
        self.assertEqual(saved["username"], "u")

    def test_missing_config_exits(self):
        os.remove(self.path)
        with self.assertRaises(SystemExit):
            sleeper.require_config()


class ScheduleTests(unittest.TestCase):
    def test_schedule_pairs(self):
        rows = [{"roster_id": 1, "matchup_id": 2}, {"roster_id": 2, "matchup_id": 1},
                {"roster_id": 3, "matchup_id": 2}, {"roster_id": 4, "matchup_id": 1},
                {"roster_id": 5, "matchup_id": None}]
        with mock.patch.object(sleeper, "matchups", return_value=rows):
            self.assertEqual(sleeper.schedule_pairs("L", 9), [(2, 4), (1, 3)])


if __name__ == "__main__":
    unittest.main()
