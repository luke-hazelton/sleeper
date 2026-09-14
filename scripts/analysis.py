"""Analysis layer: roster valuation, lineup optimization, waiver & trade logic."""
import json
import os

import sleeper

# Curated expected-return weeks, keyed by player_id (edit with scripts/injuries.py)
RETURNS_FILE = os.path.join(sleeper.DATA, "injury_returns.json")
SEASON_OVER = 99  # return_week meaning "not back this season"


def load_returns(season=None):
    """Return-week overrides keyed by player_id; other seasons' entries ignored."""
    if not os.path.exists(RETURNS_FILE):
        return {}
    with open(RETURNS_FILE) as f:
        data = json.load(f)
    return {pid: r for pid, r in data.items() if season is None or r.get("season") == season}

# Flex eligibility by roster slot
FLEX_OK = {
    "FLEX": {"RB", "WR", "TE"},
    "WRRB_FLEX": {"RB", "WR"},
    "REC_FLEX": {"WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
    "IDP_FLEX": {"DL", "LB", "DB"},
}
BENCH_SLOTS = {"BN", "IR", "TAXI"}

# Injury designations that make a player risky or unusable
OUT_STATUSES = {"Out", "IR", "PUP", "Sus", "NA", "DNR"}
RISK_STATUSES = {"Questionable", "Doubtful"}


def injury_multiplier(status):
    """Discount projected points by availability risk."""
    if status in OUT_STATUSES:
        return 0.0
    if status == "Doubtful":
        return 0.25
    if status == "Questionable":
        return 0.85
    return 1.0


class League:
    """Bundles a league's settings, rosters, projections and player data."""

    def __init__(self, league_id, week=None):
        self.id = league_id
        self.info = sleeper.league(league_id)
        self.season = self.info["season"]
        cw, _ = sleeper.current_week()
        self.week = week or cw
        # Injury tags describe the latest report, so they only apply through the
        # next week that hasn't started (covering an in-progress week too)
        self.status_horizon = sleeper.first_open_week(self.season)
        self.returns = load_returns(self.season)
        self.players = sleeper.players()
        self.score_key = sleeper.score_key(self.info)
        self.proj = sleeper.projections(self.season, self.week)
        self.rosters = sleeper.rosters(league_id)
        self.users = {u["user_id"]: u for u in sleeper.league_users(league_id)}
        self.roster_positions = self.info["roster_positions"]
        self._games = None  # this week's game statuses, loaded on first use

    def set_week(self, week, proj):
        """Point valuation at another week's projections (used by ROS loops)."""
        self.week, self.proj = week, proj
        self._games = None

    # ---------- helpers ----------

    def name(self, pid):
        p = self.players.get(pid)
        return p["full_name"] if p else f"<{pid}>"

    def pos(self, pid):
        p = self.players.get(pid)
        return p["position"] if p else "?"

    def team_of(self, pid):
        p = self.players.get(pid)
        return (p.get("team") if p else None) or "FA"

    def injury(self, pid, proj=None):
        """Prefer the projection feed's status (fresher), fall back to player DB."""
        pr = (self.proj if proj is None else proj).get(pid)
        if pr and pr.get("injury_status"):
            return pr["injury_status"]
        p = self.players.get(pid)
        return (p or {}).get("injury_status")

    def availability(self, pid, week=None, proj=None):
        """Share of a player's projection to count for a week.

        A recorded return week wins. Otherwise the injury tag only applies
        through the next unplayed week; beyond that the projection feed zeroes
        multi-week IR stints, so applying today's tag would double-count.
        """
        week = self.week if week is None else week
        ret = self.returns.get(pid)
        if ret:
            return 0.0 if week < ret["return_week"] else 1.0
        if week > self.status_horizon:
            return 1.0
        return injury_multiplier(self.injury(pid, proj))

    def game(self, pid):
        """This week's status of a player's game: pre_game, in_game, complete,
        or None when his team is on bye (or unknown)."""
        if self._games is None:
            self._games = sleeper.game_status(self.season, self.week)
        return self._games.get(self.team_of(pid))

    def locked(self, pid):
        """Sleeper locks a player once his game kicks off."""
        return self.game(pid) not in (None, "pre_game")

    def opponent(self, pid):
        pr = self.proj.get(pid)
        return (pr or {}).get("opponent")

    def points(self, pid, adjust=True):
        """Projected points, optionally discounted for injury risk."""
        raw = sleeper.proj_points(self.proj.get(pid), self.score_key)
        if adjust:
            raw *= self.availability(pid)
        return raw

    def owner_name(self, roster):
        u = self.users.get(roster.get("owner_id"))
        if not u:
            return "(orphan)"
        return u.get("metadata", {}).get("team_name") or u.get("display_name") or "?"

    def roster_for_user(self, user_id):
        for r in self.rosters:
            if r.get("owner_id") == user_id:
                return r
        return None

    def rostered_ids(self):
        out = set()
        for r in self.rosters:
            out.update(r.get("players") or [])
        return out

    def free_agents(self):
        taken = self.rostered_ids()
        return [pid for pid in self.players if pid not in taken]

    # ---------- lineup optimizer ----------

    def optimal_lineup(self, player_ids, fixed=None):
        """Greedy best-first fill of starting slots. Returns (slots, total, bench).

        Fills the most constrained slots first (dedicated positions before
        flex) so a flex-eligible stud isn't burned on a slot only he can fill.
        `fixed` maps starter-slot index -> player_id for slots that can't
        change (e.g. players whose games have kicked off).
        """
        avail = {pid: self.points(pid) for pid in player_ids
                 if self.players.get(pid)}
        starters = [s for s in self.roster_positions if s not in BENCH_SLOTS]
        # dedicated slots first, then flex by increasing eligibility breadth
        def slot_rank(s):
            if s in FLEX_OK:
                return (1, len(FLEX_OK[s]))
            return (0, 1)
        order = sorted(range(len(starters)), key=lambda i: slot_rank(starters[i]))

        fixed = fixed or {}
        used = set(fixed.values())
        assigned = {i: (starters[i], pid, self.points(pid)) for i, pid in fixed.items()}
        for i in order:
            if i in assigned:
                continue
            slot = starters[i]
            elig = FLEX_OK.get(slot, {slot})
            best, best_pts = None, -1.0
            for pid, pts in avail.items():
                if pid in used or self.pos(pid) not in elig:
                    continue
                if pts > best_pts:
                    best, best_pts = pid, pts
            if best:
                used.add(best)
                assigned[i] = (slot, best, best_pts)
            else:
                assigned[i] = (slot, None, 0.0)

        slots = [assigned[i] for i in range(len(starters))]
        total = sum(s[2] for s in slots)
        bench = sorted((p for p in avail if p not in used),
                       key=lambda p: -avail[p])
        return slots, total, bench

    def starter_value(self, player_ids):
        """Total projected points of the optimal lineup from a player pool."""
        return self.optimal_lineup(player_ids)[1]
