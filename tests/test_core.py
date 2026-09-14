"""Offline unit tests for the toolkit's decision logic. No network access.

Run from the repo root:  python3 -m unittest discover tests
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import ff  # noqa: E402
import needs  # noqa: E402
import sleeper  # noqa: E402
from analysis import League, injury_multiplier  # noqa: E402

POSITIONS = ["QB", "RB", "WR", "FLEX", "SUPER_FLEX", "BN", "BN"]


def make_league(players, proj=None, positions=POSITIONS, week=2, horizon=2,
                returns=None, games=None):
    """A League built from in-memory data, skipping the API-backed __init__."""
    lg = object.__new__(League)
    lg.season = "2026"
    lg.week = week
    lg.status_horizon = horizon
    lg.returns = returns or {}
    lg.players = players
    lg.proj = proj or {}
    lg.score_key = "pts_ppr"
    lg.roster_positions = positions
    lg._games = games if games is not None else {}
    return lg


def player(name, pos, team="KC", status=None, rank=100):
    return {"full_name": name, "position": pos, "team": team,
            "injury_status": status, "search_rank": rank}


def row(pts):
    return {"stats": {"pts_ppr": pts}}


class InjuryTests(unittest.TestCase):
    def test_multiplier(self):
        self.assertEqual(injury_multiplier("Out"), 0.0)
        self.assertEqual(injury_multiplier("IR"), 0.0)
        self.assertEqual(injury_multiplier("Doubtful"), 0.25)
        self.assertEqual(injury_multiplier("Questionable"), 0.85)
        self.assertEqual(injury_multiplier(None), 1.0)

    def test_tag_only_applies_through_horizon(self):
        lg = make_league({"1": player("A", "QB", status="Out")})
        self.assertEqual(lg.availability("1", week=2), 0.0)
        self.assertEqual(lg.availability("1", week=3), 1.0)

    def test_questionable_discounts_points(self):
        lg = make_league({"1": player("A", "QB", status="Questionable")}, proj={"1": row(10)})
        self.assertAlmostEqual(lg.points("1"), 8.5)
        self.assertEqual(lg.points("1", adjust=False), 10)

    def test_recorded_return_week_wins(self):
        lg = make_league({"1": player("A", "QB", status="Out")}, returns={"1": {"return_week": 4}})
        self.assertEqual(lg.availability("1", week=3), 0.0)  # beyond horizon, still out
        self.assertEqual(lg.availability("1", week=4), 1.0)
        lg.returns = {"1": {"return_week": 2}}
        self.assertEqual(lg.availability("1", week=2), 1.0)  # overrides the Out tag


class LineupTests(unittest.TestCase):
    def setUp(self):
        pts = {"q1": ("QB", 20), "q2": ("QB", 15), "r1": ("RB", 12),
               "r2": ("RB", 9), "w1": ("WR", 11), "w2": ("WR", 7)}
        self.lg = make_league({k: player(k, pos) for k, (pos, _) in pts.items()},
                              proj={k: row(p) for k, (_, p) in pts.items()})
        self.ids = list(pts)

    def test_fills_dedicated_slots_before_flex(self):
        slots, total, bench = self.lg.optimal_lineup(self.ids)
        self.assertEqual([s[1] for s in slots], ["q1", "r1", "w1", "r2", "q2"])
        self.assertEqual(total, 67)
        self.assertEqual(bench, ["w2"])

    def test_fixed_slot_is_kept(self):
        slots, total, bench = self.lg.optimal_lineup(self.ids, fixed={3: "w2"})
        self.assertEqual(slots[3][1], "w2")
        self.assertEqual(total, 65)
        self.assertIn("r2", bench)

    def test_locked_once_game_starts(self):
        lg = make_league({"a": player("a", "WR", team="KC"),
                          "b": player("b", "WR", team="BUF"),
                          "c": player("c", "WR", team="NYJ")},
                         games={"KC": "in_game", "BUF": "pre_game"})
        self.assertTrue(lg.locked("a"))
        self.assertFalse(lg.locked("b"))
        self.assertFalse(lg.locked("c"))  # bye: no game this week


class ResolvePlayerTests(unittest.TestCase):
    DB = {
        "1": player("Travis Etienne", "RB", team="NO", rank=50),
        "2": player("Trevor Etienne", "RB", team="CAR", rank=90),
        "3": player("Mike Williams", "WR", team=None),
        "4": player("Mike Williams", "WR", team="LAR"),
    }

    def setUp(self):
        patcher = mock.patch.object(sleeper, "players", return_value=self.DB)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_exact_name(self):
        self.assertEqual(sleeper.resolve_player("travis etienne"), "1")

    def test_ambiguous_partial_lists_candidates(self):
        with self.assertRaises(ValueError) as ctx:
            sleeper.resolve_player("Etienne")
        self.assertIn("Travis Etienne", str(ctx.exception))
        self.assertIn("Trevor Etienne", str(ctx.exception))

    def test_pool_breaks_ties(self):
        self.assertEqual(sleeper.resolve_player("Etienne", pool={"2"}), "2")

    def test_prefers_player_on_a_team(self):
        self.assertEqual(sleeper.resolve_player("Mike Williams"), "4")

    def test_missing(self):
        with self.assertRaises(ValueError):
            sleeper.resolve_player("Nobody Here")


class FirstOpenWeekTests(unittest.TestCase):
    def setUp(self):
        proj = {"a": {"date": "2026-09-10"}, "b": {"date": "2026-09-14"}, "c": {"date": None}}
        for name, value in (("current_week", (1, "2026")), ("projections", proj)):
            patcher = mock.patch.object(sleeper, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_before_any_game(self):
        self.assertEqual(sleeper.first_open_week(today="2026-09-09"), 1)
        self.assertEqual(sleeper.first_open_week(today="2026-09-10"), 1)

    def test_once_week_is_underway(self):
        self.assertEqual(sleeper.first_open_week(today="2026-09-13"), 2)


class MatchupEstimateTests(unittest.TestCase):
    def setUp(self):
        self.lg = make_league(
            {"done": player("d", "WR", team="KC"), "pre": player("p", "WR", team="BUF"),
             "live": player("l", "WR", team="DEN"), "bye": player("b", "WR", team="NYJ")},
            proj={"done": row(10), "pre": row(10), "live": row(10)},
            games={"KC": "complete", "BUF": "pre_game", "DEN": "in_game"})
        self.live = {"done": 14.0, "live": 4.0}

    def test_each_game_state(self):
        est = lambda pid: ff._estimate(self.lg, pid, self.live)  # noqa: E731
        self.assertEqual(est("done"), (14.0, 0.0))
        self.assertEqual(est("pre"), (0.0, 10.0))
        self.assertEqual(est("live"), (4.0, 3.0))
        self.assertEqual(est("bye"), (0.0, 0.0))


class NeedsTests(unittest.TestCase):
    def test_core_needs_from_slots(self):
        slots = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "SUPER_FLEX", "K", "DEF", "BN"]
        self.assertEqual(needs.core_needs(slots), {"QB": 2, "RB": 2, "WR": 2, "TE": 1})
        self.assertEqual(needs.core_needs(["QB", "RB", "WR", "TE", "FLEX"])["QB"], 1)

    def test_zero_value_player_is_not_a_starter(self):
        lg = make_league({"hurt": player("h", "RB"), "ok": player("o", "RB")})
        value = {"hurt": 0.0, "ok": 8.0}.get
        prof = needs.team_profile(lg, {"players": ["hurt", "ok"]}, value, {"RB": 1})
        self.assertEqual(prof["RB"][2][0], "ok")
        self.assertEqual(prof["RB"][0], 8.0)

    def test_week_is_its_own_option(self):
        ap = needs.build_parser()
        args = ap.parse_args(["--week", "2"])
        self.assertEqual((args.week, args.weeks), (2, None))
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            ap.parse_args(["--wee", "2"])  # no silent prefix matching
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            ap.parse_args(["--week", "2", "--weeks", "3"])


if __name__ == "__main__":
    unittest.main()
