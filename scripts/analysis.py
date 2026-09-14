"""Analysis layer: roster valuation, lineup optimization, waiver & trade logic."""
import sleeper

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
        self.players = sleeper.players()
        self.score_key = sleeper.score_key(self.info)
        self.proj = sleeper.projections(self.season, self.week)
        self.rosters = sleeper.rosters(league_id)
        self.users = {u["user_id"]: u for u in sleeper.league_users(league_id)}
        self.roster_positions = self.info["roster_positions"]

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

    def injury(self, pid):
        """Prefer the projection feed's status (fresher), fall back to player DB."""
        pr = self.proj.get(pid)
        if pr and pr.get("injury_status"):
            return pr["injury_status"]
        p = self.players.get(pid)
        return (p or {}).get("injury_status")

    def opponent(self, pid):
        pr = self.proj.get(pid)
        return (pr or {}).get("opponent")

    def points(self, pid, adjust=True):
        """Projected points, optionally discounted for injury risk."""
        raw = sleeper.proj_points(self.proj.get(pid), self.score_key)
        if adjust:
            raw *= injury_multiplier(self.injury(pid))
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

    def optimal_lineup(self, player_ids):
        """Greedy best-first fill of starting slots. Returns (slots, total, bench).

        Fills the most constrained slots first (dedicated positions before
        flex) so a flex-eligible stud isn't burned on a slot only he can fill.
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

        used = set()
        assigned = {}
        for i in order:
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
