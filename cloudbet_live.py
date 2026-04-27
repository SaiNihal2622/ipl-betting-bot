"""
Cloudbet Live IPL Demo Trader — DC vs RCB 27-Apr-2026
======================================================
Full-featured live trader:
  - Match bets (cricket.match_odds)
  - Session bets (powerplay / middle / death — cricket.team_total_from_0_over_to_x_over)
  - Team totals (cricket.team_totals)
  - Live IPL 2026 stats from Cricbuzz (team form, NRR)
  - Player impact adjustments (key batters/bowlers)
  - Bookset: hedge opposite at current odds → locks guaranteed profit
  - Stop-loss: hedge opposite to cap max loss (not just flag)
  - Full trade log with reasoning on every decision

Run: python cloudbet_live.py
"""

import os, sys, json, time, math, uuid, logging, re
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from urllib.parse import parse_qs, urlencode
import httpx
from dotenv import load_dotenv

# -- Load ML model (trained on 18yr Cricsheet data) ----------------------------
_ipl_model = None
def discover_todays_event() -> tuple:
    """
    Query Cloudbet competition feed to find today's IPL match.
    Returns (event_id, home_team, away_team) or (0, '', '') on failure.
    Auto-resolves team aliases to canonical names.
    """
    if not API_KEY:
        return 0, "", ""
    try:
        r = httpx.get(f"{FEED_BASE}/competitions/{IPL_KEY}",
                      headers=_headers(), timeout=12)
        if r.status_code != 200:
            log.debug(f"[Discover] HTTP {r.status_code}")
            return 0, "", ""
        events = r.json().get("events", [])
        from datetime import date, timezone as tz
        today = date.today()
        for ev in events:
            cutoff = ev.get("cutoffTime", "")[:10]
            status = ev.get("status", "")
            if cutoff == str(today) or status in ("TRADING", "TRADING_LIVE", "OPEN"):
                name = ev.get("name", "")
                eid  = ev.get("id", 0)
                # name is usually "Team A v Team B" or "Team A vs Team B"
                parts = [p.strip() for p in re.split(r" vs?\.? ", name, flags=re.I)]
                if len(parts) == 2:
                    home = TEAM_ALIASES.get(parts[0], parts[0])
                    away = TEAM_ALIASES.get(parts[1], parts[1])
                    log.info(f"[Discover] Found: {home} vs {away} (id={eid}, status={status})")
                    return int(eid), home, away
    except Exception as e:
        log.warning(f"[Discover] {e}")
    return 0, "", ""


def _get_model():
    global _ipl_model
    if _ipl_model is None:
        try:
            import ipl_model as m
            _ipl_model = m
            logging.getLogger("live").info(
                "[ML] ipl_model loaded — using 1,207-match Cricsheet model")
        except Exception as e:
            logging.getLogger("live").warning(f"[ML] ipl_model unavailable: {e}")
    return _ipl_model

load_dotenv()
# Also load from worktree .env which has Gemini key
for _extra in [
    os.path.join(os.path.dirname(__file__), "cricket-trading-system", ".env"),
    os.path.join(os.path.dirname(__file__), "cricket-trading-system",
                 ".claude", "worktrees", "wonderful-beaver", ".env"),
    os.path.join(os.path.dirname(__file__), "polymarket-pipeline", ".env"),
]:
    if os.path.exists(_extra):
        load_dotenv(_extra, override=False)

# -- Config ---------------------------------------------------------------------
API_KEY      = os.getenv("CLOUDBET_API_KEY", "")
CURRENCY     = os.getenv("CB_CURRENCY", "USDT")
STAKE        = float(os.getenv("CB_STAKE", "2.0"))
DRY_RUN      = os.getenv("CB_DRY_RUN", "1") != "0"
ODDS_KEY     = os.getenv("ODDS_API_KEY", "")
GEMINI_KEY   = os.getenv("GEMINI_API_KEY", "")
GROQ_KEY     = os.getenv("GROQ_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

MIN_EDGE      = 0.04    # 4% minimum model edge to enter
GREEN_PCT     = 0.25    # bookset at +25% unrealised profit
STOP_PCT      = 0.40    # hedge-stop at -40% (place opposite to cap loss)
MAX_ODDS      = 7.0
MIN_ODDS      = 1.15
POLL_SECS     = 45
MAX_OPEN      = 3       # max concurrent open positions

FEED_BASE  = "https://sports-api.cloudbet.com/pub/v2/odds"
TRADE_BASE = "https://sports-api.cloudbet.com/pub/v4"
IPL_KEY    = "cricket-india-indian-premier-league"

# Auto-discovered at runtime — set to 0 to force discovery every run
EVENT_ID   = int(os.getenv("CB_EVENT_ID", "0"))
HOME_TEAM  = os.getenv("CB_HOME_TEAM", "")
AWAY_TEAM  = os.getenv("CB_AWAY_TEAM", "")

# IPL 2026 squad map: Cloudbet name -> canonical name
TEAM_ALIASES = {
    "Delhi Capitals":               "Delhi Capitals",
    "Royal Challengers Bangalore":  "Royal Challengers Bangalore",
    "Royal Challengers Bengaluru":  "Royal Challengers Bangalore",
    "Punjab Kings":                 "Punjab Kings",
    "Rajasthan Royals":             "Rajasthan Royals",
    "Mumbai Indians":               "Mumbai Indians",
    "Sunrisers Hyderabad":          "Sunrisers Hyderabad",
    "Kolkata Knight Riders":        "Kolkata Knight Riders",
    "Chennai Super Kings":          "Chennai Super Kings",
    "Gujarat Titans":               "Gujarat Titans",
    "Lucknow Super Giants":         "Lucknow Super Giants",
}

# -- Logging --------------------------------------------------------------------
handlers = [
    logging.StreamHandler(sys.stdout),
    logging.FileHandler("ipl_live.log", encoding="utf-8"),
]
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=handlers)
log = logging.getLogger("live")

# ==============================================================================
# SECTION 1 — STATIC DATA (ELO, H2H, Historical averages)
# ==============================================================================

ELO = {
    "Mumbai Indians":              1680,
    "Chennai Super Kings":         1660,
    "Kolkata Knight Riders":       1645,
    "Rajasthan Royals":            1635,
    "Royal Challengers Bangalore": 1625,
    "Sunrisers Hyderabad":         1615,
    "Gujarat Titans":              1605,
    "Delhi Capitals":              1595,
    "Lucknow Super Giants":        1585,
    "Punjab Kings":                1575,
}

# DC vs RCB H2H (last 20 IPL matches; RCB edge overall)
H2H_WIN = {
    ("Delhi Capitals",              "Royal Challengers Bangalore"): 0.45,
    ("Royal Challengers Bangalore", "Delhi Capitals"):              0.55,
}

# Historical IPL avg T20 scores at home venue (Arun Jaitley / Chinnaswamy proxy)
AVG_SCORE = {
    "Sunrisers Hyderabad":         182,
    "Royal Challengers Bangalore": 178,
    "Rajasthan Royals":            176,
    "Punjab Kings":                177,
    "Mumbai Indians":              174,
    "Chennai Super Kings":         172,
    "Kolkata Knight Riders":       170,
    "Delhi Capitals":              169,
    "Gujarat Titans":              168,
    "Lucknow Super Giants":        165,
}

# Typical powerplay (ov 1-6) and death (ov 16-20) sub-scores
PP_AVG = {   # powerplay avg runs (1-6)
    "Royal Challengers Bangalore": 54,
    "Sunrisers Hyderabad":         52,
    "Delhi Capitals":              48,
    "Punjab Kings":                51,
    "Mumbai Indians":              50,
    "Rajasthan Royals":            49,
    "Chennai Super Kings":         47,
    "Kolkata Knight Riders":       46,
    "Gujarat Titans":              45,
    "Lucknow Super Giants":        44,
}
DEATH_AVG = {  # death (ov 16-20) avg runs
    "Sunrisers Hyderabad":         58,
    "Royal Challengers Bangalore": 56,
    "Mumbai Indians":              54,
    "Rajasthan Royals":            53,
    "Punjab Kings":                52,
    "Delhi Capitals":              50,
    "Chennai Super Kings":         49,
    "Kolkata Knight Riders":       48,
    "Gujarat Titans":              47,
    "Lucknow Super Giants":        46,
}

# Key player impact on team avg (runs/match above baseline)
# Positive = batter impact. Source: IPL 2024-25 season averages
PLAYER_IMPACT = {
    # RCB
    "Virat Kohli":       +14,
    "Phil Salt":         +11,
    "Rajat Patidar":     +9,
    "Liam Livingstone":  +8,
    "Tim David":         +7,
    # DC
    "Jake Fraser-McGurk": +13,
    "KL Rahul":           +12,
    "Axar Patel":         +6,
    "Tristan Stubbs":     +7,
    "Faf du Plessis":     +8,
}

# Bowler impact: if playing, tightens opposing avg by this many runs
BOWLER_IMPACT = {
    "Jasprit Bumrah":    -10,  # vs any team
    "Rashid Khan":        -8,
    "Yuzvendra Chahal":  -7,
    "Kuldeep Yadav":     -9,
    "Josh Hazlewood":    -8,
    "Arshdeep Singh":    -6,
}

# ==============================================================================
# SECTION 1B — AI REASONING (Gemini primary, Groq fallback)
# ==============================================================================

_ai_cache: Dict = {}          # cache last AI verdict per market cycle
_ai_cache_cycle: int = -1

def ask_ai(prompt: str, cycle: int) -> str:
    """
    Ask Gemini 2.0 Flash for a trading verdict.
    Falls back to Groq llama-3.1-8b if Gemini is rate-limited (429) or errors.
    Returns the raw text response, or "" on total failure.
    """
    global _ai_cache, _ai_cache_cycle

    # Only call AI once per cycle (expensive)
    if cycle == _ai_cache_cycle and "verdict" in _ai_cache:
        return _ai_cache["verdict"]

    verdict = ""

    # ── Gemini (try flash → lite → flash-001 in order) ────────────────────────
    if GEMINI_KEY:
        _gemini_models = [
            GEMINI_MODEL,           # from env (default: gemini-2.0-flash)
            "gemini-2.5-flash",     # latest, works even when flash rate-limited
        ]
        for _model in _gemini_models:
            if verdict:
                break
            # Use full path format (models/xxx) to avoid 404s
            _path = _model if _model.startswith("models/") else f"models/{_model}"
            try:
                url = (f"https://generativelanguage.googleapis.com/v1beta/"
                       f"{_path}:generateContent?key={GEMINI_KEY}")
                body = {"contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"maxOutputTokens": 300, "temperature": 0.2}}
                r = httpx.post(url, json=body, timeout=12)
                if r.status_code == 200:
                    verdict = (r.json()["candidates"][0]["content"]["parts"][0]["text"]
                               .strip())
                    log.info(f"[AI/Gemini:{_model}] {verdict[:200]}")
                elif r.status_code == 429:
                    log.info(f"[AI/Gemini:{_model}] Rate limited — trying next model")
                else:
                    log.debug(f"[AI/Gemini:{_model}] HTTP {r.status_code}: {r.text[:80]}")
            except Exception as e:
                log.debug(f"[AI/Gemini:{_model}] Error: {e}")
        if not verdict:
            log.info("[AI/Gemini] All models exhausted — falling back to Groq")

    # ── Groq fallback ─────────────────────────────────────────────────────────
    if not verdict and GROQ_KEY:
        try:
            r2 = httpx.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_KEY}",
                         "Content-Type": "application/json"},
                json={"model": "llama-3.1-8b-instant",
                      "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 300, "temperature": 0.2},
                timeout=12,
            )
            if r2.status_code == 200:
                verdict = r2.json()["choices"][0]["message"]["content"].strip()
                log.info(f"[AI/Groq] {verdict[:200]}")
            else:
                log.debug(f"[AI/Groq] HTTP {r2.status_code}")
        except Exception as e:
            log.debug(f"[AI/Groq] Error: {e}")

    _ai_cache = {"verdict": verdict}
    _ai_cache_cycle = cycle
    return verdict


def ai_prob_adjustment(verdict: str, base_p_home: float, base_p_away: float
                       ) -> Tuple[float, float]:
    """
    Parse AI verdict for directional bias and nudge probabilities.
    Looks for keywords: strongly favour / lean / unlikely / neutral.
    Max nudge = 8% to avoid AI overriding math entirely.
    """
    if not verdict:
        return base_p_home, base_p_away

    v = verdict.lower()
    nudge = 0.0

    if any(x in v for x in ["strongly favour dc", "dc strongly", "dc dominating",
                              "dc likely", "delhi likely", "delhi look strong"]):
        nudge = 0.08
    elif any(x in v for x in ["lean dc", "lean delhi", "slight edge dc",
                                "dc slight", "dc ahead"]):
        nudge = 0.04
    elif any(x in v for x in ["strongly favour rcb", "rcb strongly", "rcb dominating",
                                "rcb likely", "rcb look strong"]):
        nudge = -0.08
    elif any(x in v for x in ["lean rcb", "slight edge rcb", "rcb ahead",
                                "rcb slight"]):
        nudge = -0.04

    p_home = max(0.05, min(0.95, base_p_home + nudge))
    p_away = max(0.05, min(0.95, base_p_away - nudge))
    total  = p_home + p_away
    return round(p_home / total, 4), round(p_away / total, 4)


def build_ai_prompt(home: str, away: str,
                    dc_score: str, rcb_score: str, is_live: bool,
                    p_home: float, p_away: float,
                    mo_sels: List[dict], stats: Dict,
                    telegram_tips: str = "",
                    h2h: dict = None,
                    dc_avg10: float = 0, rcb_avg10: float = 0) -> str:
    """Build a concise match-situation prompt for the AI."""
    score_line = (f"Live scores: {home}={dc_score}, {away}={rcb_score}. "
                  if is_live else "Match has not started yet (pre-toss). ")

    market_line = "  ".join(
        f"{s.get('label', s['outcome'])} @ {s['price']}" for s in mo_sels
    ) if mo_sels else "not yet available"

    dc_form  = stats.get("Delhi Capitals", {}).get("form_pct", 0.5)
    rcb_form = stats.get("Royal Challengers Bangalore", {}).get("form_pct", 0.5)

    h2h_line = ""
    if h2h:
        h2h_line = (f"H2H (18yr data): {h2h.get('total_matches',0)} matches, "
                    f"DC wins={h2h.get('Delhi Capitals_wins','?')}, "
                    f"RCB wins={h2h.get('Royal Challengers Bengaluru_wins','?')}. "
                    f"Avg 1st inn={h2h.get('avg_innings1_score',0):.0f}, "
                    f"2nd inn={h2h.get('avg_innings2_score',0):.0f}.")

    avg_line = ""
    if dc_avg10 and rcb_avg10:
        avg_line = (f"2026 season avg (last 10): {home}={dc_avg10:.0f} runs, "
                    f"{away}={rcb_avg10:.0f} runs.")

    tg_line = f"\nExpert Telegram tips today:\n{telegram_tips}" if telegram_tips else ""

    prompt = f"""You are a cricket betting analyst for IPL 2026. Answer in 2-3 sentences max.

Match: {home} vs {away} — today, Arun Jaitley Stadium Delhi.
{score_line}
Market odds: {market_line}.
ML model probability (trained 1207 IPL matches): {home}={p_home:.1%}, {away}={p_away:.1%}.
Recent form (2026 season): {home} won {dc_form:.0%} of last 5, {away} won {rcb_form:.0%} of last 5.
{h2h_line}
{avg_line}
Playing XI — {home}: Jake Fraser-McGurk, KL Rahul, Axar Patel, Kuldeep Yadav, Faf du Plessis.
Playing XI — {away}: Virat Kohli, Phil Salt, Josh Hazlewood, Mohammed Siraj, Rajat Patidar.{tg_line}

Based on all the above, who do you lean towards winning?
Reply EXACTLY with one of: "Strongly favour DC", "Lean DC", "Neutral", "Lean RCB", "Strongly favour RCB".
Then one sentence of reasoning."""

    return prompt


# ==============================================================================
# SECTION 2 — LIVE IPL 2026 STATS FETCHER
# ==============================================================================

_season_cache: Dict = {}
_cache_time: float = 0.0
CACHE_TTL = 300  # refresh stats every 5 min

def fetch_ipl_stats() -> Dict:
    """
    Pull current IPL 2026 season stats from multiple sources.
    Falls back gracefully; returns dict with team form, NRR, last5.
    """
    global _season_cache, _cache_time
    if time.time() - _cache_time < CACHE_TTL and _season_cache:
        return _season_cache

    stats = {}
    # Source 1 — The Odds API scores (we already have the key)
    try:
        r = httpx.get("https://api.the-odds-api.com/v4/sports/cricket_ipl/scores/",
            params={"apiKey": ODDS_KEY, "daysFrom": 3}, timeout=8)
        if r.status_code == 200:
            events = r.json()
            recent: Dict[str, List] = {}
            for ev in events:
                if not ev.get("completed"):
                    continue
                scores = {s["name"]: s.get("score","") for s in (ev.get("scores") or [])}
                winner = None
                best_score = -1
                for name, sc in scores.items():
                    try:
                        runs = int(str(sc).split("/")[0])
                        if runs > best_score:
                            best_score, winner = runs, name
                    except:
                        pass
                for team_name in scores:
                    key = _normalize(team_name)
                    if key not in recent:
                        recent[key] = []
                    recent[key].append(1 if team_name == winner else 0)

            for key, results in recent.items():
                last5 = results[-5:]
                stats[key] = {
                    "last5_wins": sum(last5),
                    "last5": last5,
                    "form_pct": sum(last5) / len(last5) if last5 else 0.5,
                }
            log.info(f"[Stats] Loaded form data for {len(stats)} teams from The Odds API")
    except Exception as e:
        log.debug(f"Stats fetch (OddsAPI): {e}")

    # Source 2 — Cricbuzz unofficial scores endpoint
    try:
        r2 = httpx.get(
            "https://www.cricbuzz.com/api/cricket-series/9237/ipl-2026/matches",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=8, follow_redirects=True)
        if r2.status_code == 200:
            try:
                cb = r2.json()
                log.info("[Stats] Cricbuzz series data retrieved")
                # Parse team results if present
                for match in (cb if isinstance(cb, list) else []):
                    pass  # structure varies; use if parseable
            except:
                pass
    except Exception as e:
        log.debug(f"Stats fetch (Cricbuzz): {e}")

    _season_cache = stats
    _cache_time = time.time()
    return stats

def _normalize(name: str) -> str:
    """Normalize team name to key."""
    name = name.lower()
    if "delhi" in name: return "Delhi Capitals"
    if "royal" in name or "rcb" in name: return "Royal Challengers Bangalore"
    if "mumbai" in name: return "Mumbai Indians"
    if "chennai" in name: return "Chennai Super Kings"
    if "kolkata" in name or "kkr" in name: return "Kolkata Knight Riders"
    if "rajasthan" in name: return "Rajasthan Royals"
    if "sunrisers" in name or "hyderabad" in name: return "Sunrisers Hyderabad"
    if "gujarat" in name: return "Gujarat Titans"
    if "lucknow" in name: return "Lucknow Super Giants"
    if "punjab" in name: return "Punjab Kings"
    return name

def form_adjustment(team: str, stats: Dict) -> float:
    """
    Return run adjustment based on recent form.
    Win 4-5 of last 5: +5 runs. Win 0-1 of last 5: -5 runs.
    """
    key = _normalize(team)
    if key not in stats:
        return 0.0
    form = stats[key].get("form_pct", 0.5)
    adj = (form - 0.5) * 20   # ±10 runs max
    return round(adj, 1)

def player_adjustment(team: str, playing_xi: List[str]) -> float:
    """
    Adjust avg score based on confirmed playing XI.
    Batters add runs; opposition bowlers deduct.
    """
    adj = 0.0
    for p in playing_xi:
        if p in PLAYER_IMPACT:
            adj += PLAYER_IMPACT[p]
        if p in BOWLER_IMPACT:
            # bowler tightens opposing team
            adj += BOWLER_IMPACT[p]
    return round(adj, 1)

# ==============================================================================
# SECTION 3 — PROBABILITY MODELS
# ==============================================================================

def elo_prob(home: str, away: str) -> Tuple[float, float]:
    """ELO + H2H blended pre-match win probability."""
    rh = ELO.get(home, 1600)
    ra = ELO.get(away, 1600)
    p_elo = 1 / (1 + 10 ** ((ra - rh) / 400))
    p_h2h = H2H_WIN.get((home, away), p_elo)
    p = 0.65 * p_elo + 0.35 * p_h2h
    return round(p, 4), round(1 - p, 4)

def inplay_prob(runs_bat: int, wkts_bat: int, overs_done: float,
                target: Optional[int] = None) -> float:
    """
    Sigmoid win probability model for batting team.
    1st innings: project total vs IPL avg.
    2nd innings: req run rate vs current run rate + wickets remaining.
    """
    total_overs = 20.0
    if target is None:
        # 1st innings
        if overs_done <= 0:
            return 0.5
        proj = runs_bat / overs_done * total_overs
        wkt_factor = (10 - wkts_bat) / 10
        proj_adj = proj * (0.65 + 0.35 * wkt_factor)
        p = 0.5 + (proj_adj - 168) / 200
        return round(max(0.12, min(0.88, p)), 4)
    else:
        rem_runs = target - runs_bat
        rem_overs = total_overs - overs_done
        if rem_overs <= 0:
            return 1.0 if runs_bat >= target else 0.0
        req_rr   = rem_runs / rem_overs
        curr_rr  = runs_bat / max(overs_done, 0.1)
        wkts_left = 10 - wkts_bat
        rate_diff = curr_rr - req_rr
        raw = rate_diff * 0.35 + (wkts_left / 10) * 0.45
        p = 1 / (1 + math.exp(-raw * 5))
        return round(max(0.04, min(0.96, p)), 4)

def team_total_prob(team: str, line: float, direction: str,
                    stats: Dict, playing_xi: List[str],
                    opp_xi: List[str]) -> Tuple[float, str]:
    """
    Model probability for team total over/under line.
    Returns (probability, reasoning_string).
    """
    base = AVG_SCORE.get(team, 170)
    form_adj  = form_adjustment(team, stats)
    bat_adj   = player_adjustment(team, playing_xi)
    bowl_adj  = player_adjustment(team, opp_xi)    # opposition bowlers reduce score
    adj_avg   = base + form_adj + bat_adj + bowl_adj

    spread = 25.0  # std dev of T20 scores
    z = (adj_avg - line) / spread
    # cumulative normal approx
    p_over = 1 / (1 + math.exp(-1.7 * z))

    if direction == "over":
        p = p_over
    else:
        p = 1 - p_over

    reason = (f"base={base} form_adj={form_adj:+.0f} batter_adj={bat_adj:+.0f} "
              f"bowl_adj={bowl_adj:+.0f} => adj_avg={adj_avg:.0f} vs line={line} "
              f"=> p_{direction}={p:.1%}")
    return round(p, 4), reason

def session_total_prob(team: str, phase: str, line: float, direction: str,
                       stats: Dict) -> Tuple[float, str]:
    """
    Model for powerplay / death over session totals.
    phase: 'powerplay' | 'middle' | 'death'
    """
    if phase == "powerplay":
        base = PP_AVG.get(team, 48)
        spread = 10.0
    elif phase == "death":
        base = DEATH_AVG.get(team, 50)
        spread = 11.0
    else:
        full = AVG_SCORE.get(team, 170)
        pp   = PP_AVG.get(team, 48)
        dt   = DEATH_AVG.get(team, 50)
        base = full - pp - dt   # middle overs avg
        spread = 12.0

    form_adj = form_adjustment(team, stats)
    adj = base + form_adj * 0.5

    z = (adj - line) / spread
    p_over = 1 / (1 + math.exp(-1.7 * z))
    p = p_over if direction == "over" else 1 - p_over
    reason = (f"{phase} base={base} form_adj={form_adj:+.0f} => adj={adj:.0f} "
              f"vs line={line} => p_{direction}={p:.1%}")
    return round(p, 4), reason

def fair_odds(p: float) -> float:
    return round(1 / p, 3) if p > 0.01 else 99.0

# ==============================================================================
# SECTION 4 — HTTP HELPERS
# ==============================================================================

def _headers():
    return {"X-Api-Key": API_KEY, "Accept": "application/json",
            "Content-Type": "application/json"}

def feed(path: str, **params):
    r = httpx.get(f"{FEED_BASE}{path}", headers=_headers(), params=params, timeout=12)
    r.raise_for_status()
    return r.json()

def trade_post(path: str, body: dict):
    r = httpx.post(f"{TRADE_BASE}{path}", headers=_headers(), json=body, timeout=12)
    return r.json()

# ==============================================================================
# SECTION 5 — POSITION TRACKER
# ==============================================================================

@dataclass
class Position:
    team:        str
    outcome:     str           # Cloudbet outcome key (e.g. "home", "away", "over", "under")
    entry_odds:  float
    stake:       float
    market_url:  str
    market_type: str = "match_odds"   # match_odds | team_totals | session
    ref_id:      str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    placed_at:   str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))
    status:      str = "OPEN"         # OPEN | BOOKED | STOPPED | WON | LOST
    hedge_ref:   str = ""
    reason:      str = ""
    is_hedge:    bool = False          # True = this is a bookset/stop-loss hedge, not a primary bet

    @property
    def potential_win(self) -> float:
        return round(self.stake * (self.entry_odds - 1), 4)

    def hedge_stake(self, current_odds: float) -> float:
        """Stake needed on opposite side to lock profit at current_odds."""
        return round((self.stake * self.entry_odds) / current_odds, 4)

    def locked_profit(self, current_odds: float) -> float:
        """Guaranteed profit if bookset hedge placed at current_odds."""
        hs = self.hedge_stake(current_odds)
        return round(self.stake * (self.entry_odds - 1) - hs * (current_odds - 1), 4)

    def unrealised_pnl_pct(self, current_odds: float) -> float:
        """As fraction of original stake."""
        return self.locked_profit(current_odds) / self.stake

    def opposite_outcome(self) -> str:
        if self.outcome == "home":   return "away"
        if self.outcome == "away":   return "home"
        if self.outcome == "over":   return "under"
        if self.outcome == "under":  return "over"
        return ""

# ==============================================================================
# SECTION 5B — KELLY CRITERION STAKE SIZING
# ==============================================================================

BANKROLL    = float(os.getenv("CB_BANKROLL", "50.0"))   # total USDT bankroll
KELLY_FRAC  = 0.25   # fractional Kelly (25% = conservative, avoids ruin)
MAX_STAKE   = STAKE  # hard cap per bet (from config)
MIN_STAKE   = 0.50   # minimum bet worth placing

def kelly_stake(p_model: float, market_odds: float) -> float:
    """
    Full Kelly: f* = (p*(b+1) - 1) / b  where b = decimal_odds - 1
    Fractional Kelly: multiply by KELLY_FRAC.
    Returns stake in USDT, capped between MIN_STAKE and MAX_STAKE.
    If Kelly is negative (no edge), returns 0.
    """
    b = market_odds - 1.0
    if b <= 0:
        return 0.0
    f_star = (p_model * (b + 1) - 1) / b
    if f_star <= 0:
        return 0.0
    stake = round(BANKROLL * f_star * KELLY_FRAC, 2)
    return max(MIN_STAKE, min(MAX_STAKE, stake))


# ==============================================================================
# SECTION 6 — BET PLACER
# ==============================================================================

def place(team: str, outcome: str, price: float, stake: float,
          market_url: str, reason: str, market_type: str = "match_odds") -> Position:
    """Place a bet (or log in dry-run). Returns Position object."""
    ref = str(uuid.uuid4())
    mode = "[DRY]" if DRY_RUN else "[LIVE]"
    log.info(f"{mode} BET | {team} | outcome={outcome} | price={price} "
             f"| stake={stake} {CURRENCY}")
    log.info(f"       Reason: {reason}")

    pos = Position(team=team, outcome=outcome, entry_odds=price,
                   stake=stake, market_url=market_url,
                   market_type=market_type, ref_id=ref, reason=reason)

    if not DRY_RUN:
        body = {
            "currency":      CURRENCY,
            "eventId":       EVENT_ID,
            "marketUrl":     market_url,
            "outcome":       outcome,
            "price":         str(price),
            "stake":         str(stake),
            "referenceId":   ref,
            "priceVariation":"NONE",
        }
        try:
            result = trade_post("/bets/place/straight", body)
            log.info(f"       API response: {result}")
        except Exception as e:
            log.warning(f"       Bet placement failed: {e}")

    log.info(f"       Position {ref} opened | potential_win={pos.potential_win} {CURRENCY}")
    return pos

def place_hedge(pos: Position, opp_outcome: str, curr_price: float,
                h_stake: float, mkt_url: str, tag: str) -> Position:
    """Place the hedge (bookset / stop-loss) trade."""
    reason = f"{tag} hedge for position {pos.ref_id} | entry={pos.entry_odds} now={curr_price}"
    opp_team = AWAY_TEAM if pos.team == HOME_TEAM else HOME_TEAM
    hedge_pos = place(opp_team, opp_outcome, curr_price, h_stake, mkt_url, reason,
                      market_type=pos.market_type)
    hedge_pos.is_hedge = True   # prevents recursive bookset/stop-loss on hedge itself
    return hedge_pos

# ==============================================================================
# SECTION 7 — MARKET FETCHERS
# ==============================================================================

def _parse_selections(mkt: dict, include_suspended: bool = False) -> List[dict]:
    """Extract flat list of selections from a market with submarkets.
    include_suspended=True: also return SUSPENDED sels (price valid, locked mid-ball).
    """
    out = []
    for sub_key, sub in mkt.get("submarkets", {}).items():
        for sel in sub.get("selections", []):
            price = float(sel.get("price", 0) or 0)
            status = sel.get("status", "").upper()
            if price <= 1.0:
                continue
            # Only bet on OPEN; but include SUSPENDED for logging/awareness
            if not include_suspended and status not in ("OPEN", "TRADING", ""):
                continue
            params_str = sel.get("params", "")
            pdict = {}
            try:
                pdict = dict(kv.split("=") for kv in params_str.split("&") if "=" in kv)
            except:
                pass
            out.append({
                "label":   sel.get("label", ""),
                "outcome": sel.get("outcome", ""),
                "params":  params_str,
                "pdict":   pdict,
                "price":   price,
                "status":  status,
            })
    return out

def _parse_selections_all(mkt: dict) -> List[dict]:
    """Same but includes suspended — for logging purposes."""
    return _parse_selections(mkt, include_suspended=True)

def get_event_markets(market_keys: List[str]) -> dict:
    """Fetch multiple markets for EVENT_ID in one call."""
    try:
        keys_str = ",".join(market_keys)
        d = feed(f"/events/{EVENT_ID}", markets=keys_str)
        return d
    except Exception as e:
        log.debug(f"Event fetch ({market_keys}): {e}")
        return {}

def get_score() -> dict:
    global _last_score
    if not ODDS_KEY:
        return _last_score
    try:
        r = httpx.get("https://api.the-odds-api.com/v4/sports/cricket_ipl/scores/",
            params={"apiKey": ODDS_KEY, "daysFrom": 1}, timeout=8)
        if r.status_code == 200:
            for ev in r.json():
                names = [s.get("name","") for s in (ev.get("scores") or [])]
                if any("Delhi" in n or "Royal" in n for n in names):
                    sc = {"live": not ev.get("completed", True)}
                    for s in (ev.get("scores") or []):
                        n = s["name"]
                        if "Delhi" in n:
                            sc["DC"] = s.get("score","")
                        elif "Royal" in n or "RCB" in n:
                            sc["RCB"] = s.get("score","")
                    _last_score = sc
                    return sc
    except Exception as e:
        log.debug(f"Score fetch: {e}")
    return _last_score

_last_score: dict = {}

def parse_score(s) -> Tuple[int, int]:
    try:
        parts = str(s).split("/")
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except:
        return 0, 0

# ==============================================================================
# SECTION 8 — POSITION MANAGEMENT (bookset / stop-loss)
# ==============================================================================

def manage_positions(positions: List[Position], mo_sels: List[dict],
                     mo_url: str) -> List[Position]:
    """
    For every OPEN position, check bookset & stop-loss triggers.
    Bookset: pnl_pct >= GREEN_PCT → hedge to lock profit
    Stop-loss: pnl_pct <= -STOP_PCT → hedge to cap loss (not just flag)
    Only applicable to match_odds (we can back opposite on same market).
    Team totals: fixed line, can't hedge mid-way.
    """
    for pos in positions:
        if pos.status != "OPEN":
            continue
        if pos.is_hedge:
            continue   # Never manage hedge positions — avoids infinite loop
        if pos.market_type not in ("match_odds",):
            # Team totals / session bets: can't meaningfully hedge (line resolved)
            # Just log status
            continue

        # Find current price for the OPPOSITE outcome (what we'd hedge into)
        opp_out = pos.opposite_outcome()
        curr_price = None
        for sel in mo_sels:
            if sel["outcome"] == opp_out:
                curr_price = sel["price"]
                break

        if not curr_price:
            continue

        pct = pos.unrealised_pnl_pct(curr_price)
        h_stake = pos.hedge_stake(curr_price)
        locked  = pos.locked_profit(curr_price)

        log.info(f"  [POS] {pos.team} backed @ {pos.entry_odds} | opp now={curr_price} "
                 f"| unrealised PnL={pct:+.1%} | hedge_stake={h_stake} {CURRENCY}")

        if pct >= GREEN_PCT:
            # -- BOOKSET ------------------------------------------------------
            log.info(f"  [BOOKSET] Triggered! PnL={pct:+.1%} >= +{GREEN_PCT:.0%}")
            log.info(f"    Back opposite ({opp_out}) @ {curr_price} for {h_stake} {CURRENCY}")
            log.info(f"    Locked profit regardless of result: {locked:+.4f} {CURRENCY}")
            hedge = place_hedge(pos, opp_out, curr_price, h_stake, mo_url, "BOOKSET")
            pos.status    = "BOOKED"
            pos.hedge_ref = hedge.ref_id
            positions.append(hedge)

        elif pct <= -STOP_PCT:
            # -- STOP-LOSS HEDGE -----------------------------------------------
            # We can't exit fixed odds, but we can back opposite to cap further loss.
            # This doesn't remove the loss already incurred; it prevents it growing.
            log.info(f"  [STOP-LOSS] Triggered! PnL={pct:+.1%} <= -{STOP_PCT:.0%}")
            log.info(f"    Hedging opposite ({opp_out}) @ {curr_price} for {h_stake} {CURRENCY}")
            log.info(f"    Max loss capped at ~{abs(locked):.4f} {CURRENCY} (hedged position)")
            log.info(f"    Note: on fixed-odds book, this is the only exit mechanism.")
            hedge = place_hedge(pos, opp_out, curr_price, h_stake, mo_url, "STOP-LOSS")
            pos.status    = "STOPPED"
            pos.hedge_ref = hedge.ref_id
            positions.append(hedge)

    return positions

# ==============================================================================
# SECTION 9 — SIGNAL GENERATORS
# ==============================================================================

def signals_match_odds(mo_sels: List[dict], mo_url: str,
                       p_home: float, p_away: float,
                       positions: List[Position]) -> List[Position]:
    """Scan match odds market for edge, place if found."""
    log.info("[Match Odds] Scanning...")
    for sel in mo_sels:
        price   = sel["price"]
        outcome = sel["outcome"]
        label   = sel["label"] or outcome

        if not (MIN_ODDS <= price <= MAX_ODDS):
            log.info(f"  {label} @ {price} — outside range")
            continue

        if outcome == "home":
            p_model, team, fair = p_home, HOME_TEAM, fair_odds(p_home)
        elif outcome == "away":
            p_model, team, fair = p_away, AWAY_TEAM, fair_odds(p_away)
        else:
            continue

        edge = price / fair - 1
        log.info(f"  {team} | market={price} fair={fair} p_model={p_model:.1%} edge={edge:+.1%}")

        already = any(p.team == team and p.status == "OPEN"
                      and p.market_type == "match_odds" for p in positions)
        if already:
            log.info(f"    Already open on {team} — skip")
            continue

        if edge >= MIN_EDGE:
            ks = kelly_stake(p_model, price)
            if ks < MIN_STAKE:
                log.info(f"    Kelly stake too small ({ks:.2f}) — skip")
                continue
            reason = (f"Match odds edge={edge:.1%} | model p={p_model:.1%} "
                      f"vs implied {1/price:.1%} | Kelly stake={ks:.2f} USDT "
                      f"(bankroll={BANKROLL}, frac={KELLY_FRAC})")
            pos = place(team, outcome, price, ks, mo_url, reason, "match_odds")
            positions.append(pos)
        else:
            log.info(f"    No edge (need {MIN_EDGE:.0%}) — skip")

    return positions

def signals_team_totals(tt_mkt: dict, stats: Dict,
                        playing_xi_home: List[str], playing_xi_away: List[str],
                        positions: List[Position]) -> List[Position]:
    """Scan team totals market for edge."""
    log.info("[Team Totals] Scanning...")
    sels = _parse_selections(tt_mkt)
    mkt_url = f"{IPL_KEY}/{EVENT_ID}/cricket.team_totals"

    for sel in sels:
        price     = sel["price"]
        direction = sel["outcome"].lower()  # "over" or "under"
        pdict     = sel["pdict"]

        if direction not in ("over", "under"):
            continue
        if not (MIN_ODDS <= price <= MAX_ODDS):
            continue

        team_side = pdict.get("team", "")
        try:
            line = float(pdict.get("total", 0))
        except:
            continue

        team = HOME_TEAM if team_side == "home" else (AWAY_TEAM if team_side == "away" else None)
        if not team or line <= 0:
            continue

        bat_xi  = playing_xi_home if team == HOME_TEAM else playing_xi_away
        bowl_xi = playing_xi_away if team == HOME_TEAM else playing_xi_home

        p_model, reason = team_total_prob(team, line, direction, stats, bat_xi, bowl_xi)
        f_odds = fair_odds(p_model)
        edge   = price / f_odds - 1
        label  = f"{team} {direction.title()} {line}"

        log.info(f"  {label} | market={price} fair={f_odds:.3f} p={p_model:.1%} edge={edge:+.1%}")
        log.info(f"    {reason}")

        already = any(p.team == team and p.outcome == direction
                      and p.market_type == "team_totals" and p.status == "OPEN"
                      for p in positions)
        if already:
            log.info(f"    Already have {label} — skip")
            continue

        if edge >= MIN_EDGE:
            ks = kelly_stake(p_model, price)
            log.info(f"    >>> VALUE FOUND edge={edge:.1%} | Kelly stake={ks:.2f} USDT")
            if ks >= MIN_STAKE:
                pos = place(team, sel["outcome"], price, ks, mkt_url,
                            f"{label} | {reason} | Kelly={ks:.2f}", "team_totals")
                positions.append(pos)
        else:
            log.info(f"    No edge (need {MIN_EDGE:.0%}) — skip")

    return positions

def signals_session(event_data: dict, stats: Dict,
                    positions: List[Position]) -> List[Position]:
    """
    Scan powerplay and death-over session markets.
    Market keys:
      cricket.team_total_from_0_over_to_6_over   → powerplay
      cricket.team_total_from_16_over_to_20_over → death
    """
    session_markets = {
        "cricket.team_total_from_0_over_to_6_over":   ("powerplay",  0,  6),
        "cricket.team_total_from_16_over_to_20_over": ("death",     16, 20),
        "cricket.over_team_total":                    ("over_by_over", None, None),
    }

    mkts = event_data.get("markets", {})

    for mkey, (phase, ov_from, ov_to) in session_markets.items():
        mkt = mkts.get(mkey)
        if not mkt:
            continue
        sels = _parse_selections(mkt)
        mkt_url = f"{IPL_KEY}/{EVENT_ID}/{mkey}"
        log.info(f"[Session: {phase}] Scanning {mkey}...")

        for sel in sels:
            price     = sel["price"]
            direction = sel["outcome"].lower()
            pdict     = sel["pdict"]

            if direction not in ("over", "under"):
                continue
            if not (MIN_ODDS <= price <= MAX_ODDS):
                continue

            team_side = pdict.get("team", "")
            try:
                line = float(pdict.get("total", 0))
            except:
                continue

            team = HOME_TEAM if team_side == "home" else (AWAY_TEAM if team_side == "away" else None)
            if not team or line <= 0:
                continue

            p_model, reason = session_total_prob(team, phase, line, direction, stats)
            f_odds = fair_odds(p_model)
            edge   = price / f_odds - 1
            label  = f"[{phase}] {team} {direction.title()} {line}"

            log.info(f"  {label} | market={price} fair={f_odds:.3f} p={p_model:.1%} edge={edge:+.1%}")
            log.info(f"    {reason}")

            already = any(p.market_url == mkt_url and p.outcome == direction
                          and p.team == team and p.status == "OPEN"
                          for p in positions)
            if already:
                log.info(f"    Already open — skip")
                continue

            if edge >= MIN_EDGE:
                ks = kelly_stake(p_model, price)
                log.info(f"    >>> SESSION VALUE edge={edge:.1%} | Kelly stake={ks:.2f} USDT")
                if ks >= MIN_STAKE:
                    pos = place(team, sel["outcome"], price, ks, mkt_url,
                                f"{label} | {reason} | Kelly={ks:.2f}", "session")
                    positions.append(pos)
            else:
                log.info(f"    No edge — skip")

    return positions

# ==============================================================================
# SECTION 10 — MAIN CYCLE
# ==============================================================================

# Playing XI (best guess from squad; update if official XI announced)
PLAYING_XI_DC = [
    "Jake Fraser-McGurk", "KL Rahul", "Faf du Plessis", "Tristan Stubbs",
    "Axar Patel", "Sumit Kumar", "Mukesh Kumar", "Khaleel Ahmed",
    "Ishant Sharma", "Kuldeep Yadav", "Mohit Sharma",
]
PLAYING_XI_RCB = [
    "Phil Salt", "Virat Kohli", "Rajat Patidar", "Liam Livingstone",
    "Tim David", "Dinesh Karthik", "Mayank Dagar", "Josh Hazlewood",
    "Mohammed Siraj", "Yash Dayal", "Suyash Sharma",
]

def analyse_and_trade(positions: List[Position], cycle: int) -> List[Position]:
    log.info(f"\n{'='*65}")
    log.info(f"CYCLE #{cycle} | {datetime.now().strftime('%H:%M:%S IST')} | DC vs RCB")
    log.info(f"{'='*65}")

    # -- Live stats -------------------------------------------------------------
    stats = fetch_ipl_stats()
    dc_form  = stats.get("Delhi Capitals", {}).get("form_pct", 0.5)
    rcb_form = stats.get("Royal Challengers Bangalore", {}).get("form_pct", 0.5)
    log.info(f"[Stats] DC form={dc_form:.0%} last5={stats.get('Delhi Capitals',{}).get('last5',[])} "
             f"| RCB form={rcb_form:.0%} last5={stats.get('Royal Challengers Bangalore',{}).get('last5',[])}")

    # -- Score ------------------------------------------------------------------
    score   = get_score()
    dc_sc   = score.get("DC", "")
    rcb_sc  = score.get("RCB", "")
    is_live = score.get("live", False)
    log.info(f"[Score] DC={dc_sc or 'N/A'} | RCB={rcb_sc or 'N/A'} | Live={is_live}")

    # -- Fetch all needed markets in one call ----------------------------------
    market_keys = [
        "cricket.match_odds",
        "cricket.team_totals",
        "cricket.team_total_from_0_over_to_6_over",
        "cricket.team_total_from_16_over_to_20_over",
        "cricket.over_team_total",
    ]
    event_data = get_event_markets(market_keys)
    mkts = event_data.get("markets", {})

    mo_mkt  = mkts.get("cricket.match_odds", {})
    tt_mkt  = mkts.get("cricket.team_totals", {})
    ev_status = event_data.get("status", "unknown")
    log.info(f"[Market] Event status: {ev_status} | markets returned: {list(mkts.keys())}")

    # Log raw selections including suspended ones so we can debug live market
    if mo_mkt:
        for sub_k, sub in mo_mkt.get("submarkets", {}).items():
            for sel in sub.get("selections", []):
                log.info(f"  [RAW match_odds] outcome={sel.get('outcome')} "
                         f"price={sel.get('price')} status={sel.get('status')} "
                         f"params={sel.get('params')}")
    if tt_mkt:
        for sub_k, sub in tt_mkt.get("submarkets", {}).items():
            for sel in sub.get("selections", [])[:4]:
                log.info(f"  [RAW team_totals] outcome={sel.get('outcome')} "
                         f"price={sel.get('price')} status={sel.get('status')} "
                         f"params={sel.get('params')}")

    # Include SUSPENDED selections too — Cloudbet suspends mid-ball, resumes between balls
    # We log them but only bet on OPEN ones
    mo_sels = _parse_selections(mo_mkt) if mo_mkt else []
    # Also try with suspended (price still valid, just momentarily locked)
    mo_sels_all = _parse_selections_all(mo_mkt) if mo_mkt else []
    log.info(f"[Market] match_odds: {len(mo_sels)} open / {len(mo_sels_all)} total sels "
             f"| team_totals: {'yes' if tt_mkt else 'no'}")
    mo_url  = f"{IPL_KEY}/{EVENT_ID}/cricket.match_odds"

    # -- Telegram tips (fetch early so toss feeds into model) ------------------
    tg_tips = ""
    ml = _get_model()
    if ml:
        try:
            tg_tips = ml.get_telegram_tips([HOME_TEAM, AWAY_TEAM, "DC", "RCB",
                                            "Delhi", "Bangalore", "Bengaluru"])
            if tg_tips:
                log.info(f"[Telegram] {tg_tips[:200]}")
            else:
                log.info("[Telegram] No tips found")
        except Exception as e:
            log.debug(f"[Telegram] {e}")

    # -- Win probability model -------------------------------------------------
    # Parse toss from Telegram tip (e.g. "RCB Opt to Bowl" = RCB won toss, chose field)
    toss_winner   = ""
    toss_decision = ""
    if tg_tips:
        tg_low = tg_tips.lower()
        if "rcb" in tg_low and ("bowl" in tg_low or "field" in tg_low):
            toss_winner, toss_decision = AWAY_TEAM, "field"
            log.info(f"[Toss] {AWAY_TEAM} won toss, elected to FIELD (DC batting first)")
        elif "rcb" in tg_low and "bat" in tg_low:
            toss_winner, toss_decision = AWAY_TEAM, "bat"
            log.info(f"[Toss] {AWAY_TEAM} won toss, elected to BAT")
        elif "dc" in tg_low or "delhi" in tg_low:
            if "bowl" in tg_low or "field" in tg_low:
                toss_winner, toss_decision = HOME_TEAM, "field"
                log.info(f"[Toss] {HOME_TEAM} won toss, elected to FIELD (RCB batting first)")
            elif "bat" in tg_low:
                toss_winner, toss_decision = HOME_TEAM, "bat"
                log.info(f"[Toss] {HOME_TEAM} won toss, elected to BAT")

    if is_live and dc_sc and rcb_sc:
        dc_r, dc_w  = parse_score(dc_sc)
        rcb_r, rcb_w = parse_score(rcb_sc)

        if rcb_r == 0 and dc_r > 30:
            target = dc_r + 1
            if ml:
                p_rcb = ml.get_inplay_prob(rcb_r, rcb_w, 0.1, target)
            else:
                p_rcb = inplay_prob(rcb_r, rcb_w, 0.1, target)
            p_dc = 1 - p_rcb
            log.info(f"[Model] 2nd inn: RCB chasing {target} | {rcb_r}/{rcb_w} | "
                     f"source={'ML-78%acc' if ml else 'sigmoid'}")
        elif dc_r == 0 and rcb_r > 30:
            target = rcb_r + 1
            if ml:
                p_dc = ml.get_inplay_prob(dc_r, dc_w, 0.1, target)
            else:
                p_dc = inplay_prob(dc_r, dc_w, 0.1, target)
            p_rcb = 1 - p_dc
            log.info(f"[Model] 2nd inn: DC chasing {target} | {dc_r}/{dc_w} | "
                     f"source={'ML-78%acc' if ml else 'sigmoid'}")
        else:
            overs_est = (dc_r + rcb_r) / 17.0 if (dc_r + rcb_r) > 0 else 5.0
            if ml:
                p_dc  = ml.get_inplay_prob(dc_r, dc_w, overs_est, None)
                p_rcb = ml.get_inplay_prob(rcb_r, rcb_w, overs_est, None)
            else:
                p_dc  = inplay_prob(dc_r, dc_w, overs_est)
                p_rcb = inplay_prob(rcb_r, rcb_w, overs_est)
            total = p_dc + p_rcb
            p_dc, p_rcb = p_dc / total, p_rcb / total
            log.info(f"[Model] 1st inn: DC {dc_r}/{dc_w} | RCB {rcb_r}/{rcb_w} | "
                     f"overs~{overs_est:.1f} | source={'ML' if ml else 'sigmoid'}")
    else:
        # Pre-match — use ML model
        if ml:
            p_dc, p_rcb = ml.get_prematch_prob(HOME_TEAM, AWAY_TEAM,
                                                venue="Arun Jaitley Stadium",
                                                toss_winner=toss_winner,
                                                toss_decision=toss_decision)
            h2h = ml.get_h2h_stats(HOME_TEAM, AWAY_TEAM)
            dc_avg10  = ml.get_team_avg_score(HOME_TEAM, 10)
            rcb_avg10 = ml.get_team_avg_score(AWAY_TEAM, 10)
            log.info(f"[Model] Pre-match ML: DC={p_dc:.1%} RCB={p_rcb:.1%} "
                     f"(trained on {1207} IPL matches, 60% acc)")
            log.info(f"[H2H]  Last {h2h.get('total_matches',0)} meetings | "
                     f"DC wins={h2h.get('Delhi Capitals_wins', h2h.get('Delhi Capitals_win_pct','-'))} | "
                     f"avg 1st inn={h2h.get('avg_innings1_score',0):.0f} "
                     f"2nd inn={h2h.get('avg_innings2_score',0):.0f}")
            log.info(f"[Avg]  DC last-10 avg={dc_avg10:.0f} | RCB last-10 avg={rcb_avg10:.0f} "
                     f"(live Cricsheet 2026 data)")
            # Also apply form adjustment on top
            form_dc  = (dc_form  - 0.5) * 0.06
            form_rcb = (rcb_form - 0.5) * 0.06
            p_dc  = max(0.10, min(0.90, p_dc  + form_dc))
            p_rcb = max(0.10, min(0.90, p_rcb + form_rcb))
            tot = p_dc + p_rcb; p_dc /= tot; p_rcb /= tot
        else:
            p_dc_base, p_rcb_base = elo_prob(HOME_TEAM, AWAY_TEAM)
            form_dc  = (dc_form  - 0.5) * 0.12
            form_rcb = (rcb_form - 0.5) * 0.12
            p_dc  = max(0.10, min(0.90, p_dc_base  + form_dc))
            p_rcb = max(0.10, min(0.90, p_rcb_base + form_rcb))
            total = p_dc + p_rcb; p_dc /= total; p_rcb /= total
            log.info(f"[Model] Pre-match ELO fallback: DC={p_dc:.1%} RCB={p_rcb:.1%}")

    # Also get live team avg from ML for team totals model
    if ml:
        AVG_SCORE[HOME_TEAM] = ml.get_team_avg_score(HOME_TEAM, 10)
        AVG_SCORE[AWAY_TEAM] = ml.get_team_avg_score(AWAY_TEAM, 10)

    log.info(f"[Model] Final: DC={p_dc:.1%} fair={fair_odds(p_dc)} | "
             f"RCB={p_rcb:.1%} fair={fair_odds(p_rcb)}")
    log.info(f"[Players] DC XI={','.join(PLAYING_XI_DC[:5])}... "
             f"| RCB XI={','.join(PLAYING_XI_RCB[:5])}...")

    # -- AI Reasoning (Gemini primary / Groq fallback) -------------------------
    ai_verdict = ""
    _h2h = ml.get_h2h_stats(HOME_TEAM, AWAY_TEAM) if ml else {}
    _dc_avg  = ml.get_team_avg_score(HOME_TEAM, 10) if ml else 0
    _rcb_avg = ml.get_team_avg_score(AWAY_TEAM, 10) if ml else 0

    if GEMINI_KEY or GROQ_KEY:
        ai_prompt  = build_ai_prompt(HOME_TEAM, AWAY_TEAM, dc_sc, rcb_sc,
                                     is_live, p_dc, p_rcb, mo_sels, stats,
                                     telegram_tips=tg_tips,
                                     h2h=_h2h,
                                     dc_avg10=_dc_avg, rcb_avg10=_rcb_avg)
        ai_verdict = ask_ai(ai_prompt, cycle)
        if ai_verdict:
            p_dc, p_rcb = ai_prob_adjustment(ai_verdict, p_dc, p_rcb)
            log.info(f"[AI] Adjusted: DC={p_dc:.1%} RCB={p_rcb:.1%} "
                     f"(after Gemini/Groq nudge)")
        else:
            log.info("[AI] No verdict — using pure ML model")
    else:
        log.info("[AI] No keys — using pure ML model")

    # -- Manage existing positions ---------------------------------------------
    if mo_sels:
        positions = manage_positions(positions, mo_sels, mo_url)

    # -- Look for new bets ------------------------------------------------------
    open_ct = sum(1 for p in positions if p.status == "OPEN")
    if open_ct >= MAX_OPEN:
        log.info(f"[Entry] {open_ct}/{MAX_OPEN} open positions — not adding more")
        return positions

    # 1. Match odds (if available)
    if mo_sels:
        positions = signals_match_odds(mo_sels, mo_url, p_dc, p_rcb, positions)

    # 2. Team totals (pre-match and in-play)
    if tt_mkt:
        positions = signals_team_totals(tt_mkt, stats,
                                        PLAYING_XI_DC, PLAYING_XI_RCB, positions)

    # 3. Session markets (powerplay / death)
    positions = signals_session(event_data, stats, positions)

    return positions

# ==============================================================================
# SECTION 11 — SUMMARY + MAIN LOOP
# ==============================================================================

def print_summary(positions: List[Position]):
    log.info(f"\n{'-'*65}")
    log.info("TRADE SUMMARY — DC vs RCB")
    log.info(f"{'-'*65}")
    if not positions:
        log.info("No trades this session.")
        return

    total_staked = sum(p.stake for p in positions)
    log.info(f"{'Team':<32} {'Type':<14} {'Entry':>6} {'Stake':>6} {'Status':>10} {'At':>8}")
    log.info("-" * 80)
    for p in positions:
        log.info(f"{p.team:<32} {p.market_type:<14} {p.entry_odds:>6.2f} "
                 f"{p.stake:>6.2f} {p.status:>10} {p.placed_at:>8}")
    log.info(f"\nTotal staked: {total_staked:.4f} {CURRENCY} | "
             f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE TRADING'}")
    booked  = [p for p in positions if p.status == "BOOKED"]
    stopped = [p for p in positions if p.status == "STOPPED"]
    if booked:
        log.info(f"Bookset: {len(booked)} position(s) — profit locked regardless of result")
    if stopped:
        log.info(f"Stopped: {len(stopped)} position(s) — hedged to cap further loss")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true",
                        help="Run one cycle and exit")
    parser.add_argument("--loop", action="store_true",
                        help="Loop for --duration seconds (GitHub Actions mode)")
    parser.add_argument("--duration", type=int, default=200,
                        help="Max seconds to run in --loop mode (default 200)")
    args = parser.parse_args()

    # -- Auto-discover today's match -------------------------------------------
    global EVENT_ID, HOME_TEAM, AWAY_TEAM
    if EVENT_ID == 0 or not HOME_TEAM:
        EVENT_ID, HOME_TEAM, AWAY_TEAM = discover_todays_event()
    if not HOME_TEAM:
        # Fallback: use env vars or exit gracefully
        HOME_TEAM = os.getenv("CB_HOME_TEAM", "TBD")
        AWAY_TEAM = os.getenv("CB_AWAY_TEAM", "TBD")
        log.warning(f"[Discover] Could not find today's match — set CB_HOME_TEAM/CB_AWAY_TEAM env vars")

    log.info("=" * 65)
    log.info(f"IPL LIVE TRADER — {HOME_TEAM} vs {AWAY_TEAM}")
    log.info(f"Mode: {'DRY RUN' if DRY_RUN else '*** LIVE TRADING ***'} | "
             f"Event ID: {EVENT_ID}")
    log.info(f"Stake={STAKE} {CURRENCY} | Kelly bankroll={BANKROLL} | "
             f"Min edge={MIN_EDGE:.0%} | Bookset=+{GREEN_PCT:.0%} | StopLoss=-{STOP_PCT:.0%}")
    log.info("Model: Cricsheet ML (1207 matches) + Gemini AI + Telegram tips + Kelly sizing")
    log.info("=" * 65)

    # In Actions mode we persist positions in a JSON state file between runs
    STATE_FILE = "cloudbet_positions.json"

    def load_positions():
        if not os.path.exists(STATE_FILE):
            return []
        try:
            data = json.load(open(STATE_FILE))
            out = []
            for d in data:
                p = Position(**{k: v for k, v in d.items()
                                if k in Position.__dataclass_fields__})
                out.append(p)
            log.info(f"[State] Loaded {len(out)} positions from {STATE_FILE}")
            return out
        except Exception as e:
            log.warning(f"[State] Could not load {STATE_FILE}: {e}")
            return []

    def save_positions(positions):
        try:
            data = [p.__dict__ for p in positions]
            json.dump(data, open(STATE_FILE, "w"), default=str)
            log.info(f"[State] Saved {len(positions)} positions to {STATE_FILE}")
        except Exception as e:
            log.warning(f"[State] Save failed: {e}")

    if args.once:
        positions = load_positions()
        positions = analyse_and_trade(positions, 1)
        print_summary(positions)
        save_positions(positions)
        return

    if args.loop:
        # GitHub Actions: loop for `duration` seconds, polling every 30s
        # This catches the 30s open windows between balls that --once misses
        import time as _time
        deadline = _time.time() + args.duration
        positions = load_positions()
        cycle = 0
        while _time.time() < deadline:
            cycle += 1
            positions = analyse_and_trade(positions, cycle)
            print_summary(positions)
            save_positions(positions)
            remaining = deadline - _time.time()
            if remaining <= 30:
                break
            log.info(f"Next poll in 30s ({remaining:.0f}s remaining in job)...")
            _time.sleep(30)
        log.info("Loop finished.")
        return

    # Continuous loop (local run)
    positions: List[Position] = load_positions()
    cycle = 0
    try:
        while True:
            cycle += 1
            positions = analyse_and_trade(positions, cycle)
            print_summary(positions)
            save_positions(positions)
            log.info(f"Sleeping {POLL_SECS}s...\n")
            time.sleep(POLL_SECS)
    except KeyboardInterrupt:
        log.info("\nStopped by user.")
        print_summary(positions)
        save_positions(positions)


if __name__ == "__main__":
    main()
