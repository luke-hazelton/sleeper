"""Core Sleeper API client with on-disk caching.

Sleeper's API is unauthenticated and rate-limited to ~1000 calls/min.
We cache aggressively: the player DB daily, projections/stats per-week.
"""
import datetime
import json
import os
import subprocess
import time
import urllib.request
import urllib.error

try:
    import certifi
    _SSL_CTX = __import__("ssl").create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = None

BASE = "https://api.sleeper.app/v1"
PROJ_BASE = "https://api.sleeper.app"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CONFIG = os.path.join(ROOT, "config.json")

# Cache lifetimes in seconds
TTL_PLAYERS = 86400      # player DB: once a day (14MB, changes slowly)
TTL_PROJ = 3600 * 6      # projections: refresh a few times a week
TTL_LIVE = 300           # rosters/matchups: 5 min during game days
TTL_TRENDING = 1800      # trending adds/drops: 30 min


def _fetch(url):
    """Fetch JSON. Uses urllib, falling back to curl when system root certs
    are missing (common on python.org macOS builds)."""
    req = urllib.request.Request(url, headers={"User-Agent": "sleeper-helper/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} for {url}") from e
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" not in str(e.reason):
            raise
    out = subprocess.run(
        ["curl", "-sS", "--fail", "-H", "User-Agent: sleeper-helper/1.0", url],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(f"curl failed for {url}: {out.stderr.strip()}")
    return json.loads(out.stdout)


def _cache_path(key):
    return os.path.join(DATA, "cache", key.replace("/", "_") + ".json")


def get(url, cache_key=None, ttl=TTL_LIVE):
    """GET a URL with optional on-disk caching."""
    if cache_key:
        p = _cache_path(cache_key)
        if os.path.exists(p) and (time.time() - os.path.getmtime(p)) < ttl:
            with open(p) as f:
                return json.load(f)
    data = _fetch(url)
    if cache_key:
        p = _cache_path(cache_key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(data, f)
    return data


# ---------- config ----------

def load_config():
    if not os.path.exists(CONFIG):
        return {}
    with open(CONFIG) as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------- state ----------

def nfl_state():
    return get(f"{BASE}/state/nfl", "nfl_state", 3600)


def current_week():
    s = nfl_state()
    return int(s.get("display_week") or s.get("week") or 1), s["season"]


def first_open_week(season=None, today=None):
    """First week with no games played yet: the earliest week a roster move made
    today can affect in full. Mid-slate that's next week; after Sleeper rolls
    over to a new week, it's that week."""
    cw, s = current_week()
    today = today or datetime.date.today().isoformat()
    dates = [r["date"] for r in projections(season or s, cw).values() if r.get("date")]
    return cw if dates and min(dates) >= today else cw + 1


# ---------- players ----------

_players_cache = None


def players():
    """Full player DB keyed by player_id. Slimmed on first load for speed."""
    global _players_cache
    if _players_cache is not None:
        return _players_cache
    slim = os.path.join(DATA, "players_slim.json")
    raw = os.path.join(DATA, "players_raw.json")
    if os.path.exists(slim) and (time.time() - os.path.getmtime(slim)) < TTL_PLAYERS:
        with open(slim) as f:
            _players_cache = json.load(f)
            return _players_cache
    if not os.path.exists(raw) or (time.time() - os.path.getmtime(raw)) > TTL_PLAYERS:
        data = get(f"{BASE}/players/nfl")
        with open(raw, "w") as f:
            json.dump(data, f)
    else:
        with open(raw) as f:
            data = json.load(f)
    keep = ("full_name", "first_name", "last_name", "position", "team", "age",
            "injury_status", "injury_body_part", "injury_notes", "status",
            "depth_chart_order", "depth_chart_position", "years_exp",
            "search_rank", "number", "practice_participation", "news_updated")
    out = {}
    for pid, p in data.items():
        if not isinstance(p, dict):
            continue
        pos = p.get("position")
        if pos not in ("QB", "RB", "WR", "TE", "K", "DEF"):
            continue
        out[pid] = {k: p.get(k) for k in keep}
        if not out[pid].get("full_name"):
            out[pid]["full_name"] = f"{p.get('first_name','')} {p.get('last_name','')}".strip()
    with open(slim, "w") as f:
        json.dump(out, f)
    _players_cache = out
    return out


def find_player(name):
    """Fuzzy-find players by name. Returns list of (pid, player) sorted by relevance."""
    n = name.lower().strip()
    hits = []
    for pid, p in players().items():
        fn = (p.get("full_name") or "").lower()
        if n == fn:
            hits.append((0, pid, p))
        elif fn.startswith(n) or n in fn:
            hits.append((1, pid, p))
    hits.sort(key=lambda h: (h[0], h[2].get("search_rank") or 99999))
    return [(pid, p) for _, pid, p in hits]


def resolve_player(name, pool=None):
    """Resolve a name to exactly one player_id, or raise ValueError.

    Exact full-name matches beat partial ones. Among several, prefer players in
    `pool` (e.g. rostered ids), then players on an NFL team. If it's still
    ambiguous, the error lists the candidates instead of guessing.
    """
    n = name.lower().strip()
    hits = find_player(n)
    if not hits:
        raise ValueError(f"No player matching '{name.strip()}'")
    cands = [h for h in hits if (h[1].get("full_name") or "").lower() == n] or hits
    for keep in (lambda h: pool is not None and h[0] in pool,
                 lambda h: bool(h[1].get("team"))):
        if len(cands) > 1:
            cands = [h for h in cands if keep(h)] or cands
    if len(cands) > 1:
        opts = "; ".join(f"{p.get('full_name')} ({p.get('position')}, {p.get('team') or 'FA'})"
                         for _, p in cands[:6])
        raise ValueError(f"'{name.strip()}' matches several players: {opts}. Be more specific.")
    return cands[0][0]


# ---------- league ----------

def user(username):
    return get(f"{BASE}/user/{username}", f"user_{username}", 86400)


def user_leagues(user_id, season=None):
    season = season or nfl_state()["season"]
    return get(f"{BASE}/user/{user_id}/leagues/nfl/{season}", f"leagues_{user_id}_{season}", 3600)


def league(league_id):
    return get(f"{BASE}/league/{league_id}", f"league_{league_id}", 3600)


def rosters(league_id):
    return get(f"{BASE}/league/{league_id}/rosters", f"rosters_{league_id}", TTL_LIVE)


def league_users(league_id):
    return get(f"{BASE}/league/{league_id}/users", f"users_{league_id}", 3600)


def matchups(league_id, week):
    return get(f"{BASE}/league/{league_id}/matchups/{week}", f"matchups_{league_id}_{week}", TTL_LIVE)


def transactions(league_id, week):
    return get(f"{BASE}/league/{league_id}/transactions/{week}", f"tx_{league_id}_{week}", TTL_LIVE)


# ---------- projections & stats ----------

POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF"]


def projections(season, week):
    """Weekly projections keyed by player_id. Includes opponent + injury status."""
    qs = "&".join(f"position[]={p}" for p in POSITIONS)
    url = f"{PROJ_BASE}/projections/nfl/{season}/{week}?season_type=regular&{qs}&order_by=pts_ppr"
    raw = get(url, f"proj_{season}_{week}", TTL_PROJ)
    out = {}
    for row in raw:
        pid = row.get("player_id")
        if not pid:
            continue
        out[pid] = {
            "stats": row.get("stats") or {},
            "opponent": row.get("opponent"),
            "team": row.get("team"),
            "injury_status": (row.get("player") or {}).get("injury_status"),
            "game_id": row.get("game_id"),
            "date": row.get("date"),
        }
    return out


def weekly_stats(season, week):
    """Actual stats for a completed week, keyed by player_id."""
    return get(f"{BASE}/stats/nfl/regular/{season}/{week}", f"stats_{season}_{week}", TTL_PROJ)


def schedule(season, season_type="regular"):
    """Every game of the season with live status (pre_game / in_game / complete).
    Undocumented endpoint; it has game dates but no kickoff times or clock."""
    return get(f"{PROJ_BASE}/schedule/nfl/{season_type}/{season}",
               f"schedule_{season}_{season_type}", TTL_LIVE)


def game_status(season, week):
    """{team: game status} for one week. Teams on bye are absent."""
    out = {}
    for g in schedule(season):
        if g.get("week") == week:
            for team in (g.get("home"), g.get("away")):
                out[team] = g.get("status")
    return out


def trending(kind="add", hours=24, limit=50):
    url = f"{BASE}/players/nfl/trending/{kind}?lookback_hours={hours}&limit={limit}"
    return get(url, f"trending_{kind}_{hours}_{limit}", TTL_TRENDING)


# ---------- scoring ----------

def score_key(league_obj):
    """Pick the projection stat key matching the league's scoring settings."""
    rec = (league_obj.get("scoring_settings") or {}).get("rec", 0)
    if rec >= 1:
        return "pts_ppr"
    if rec >= 0.4:
        return "pts_half_ppr"
    return "pts_std"


def proj_points(proj_row, key):
    if not proj_row:
        return 0.0
    s = proj_row["stats"]
    return float(s.get(key) or s.get("pts_ppr") or 0.0)
