"""
Cloudbet IPL Cricket Trading Bot — data-backed
================================================
Feed API  : https://sports-api.cloudbet.com/pub/v2/odds/
Trading   : https://sports-api.cloudbet.com/pub/v4/bets/place/straight
Auth      : X-Api-Key: <JWT>

Markets traded:
  PRE-MATCH  : cricket.match_odds (when available), cricket.team_totals
  IN-PLAY    : cricket.match_odds, cricket.over_team_total, cricket.team_totals

Model:
  - ELO ratings for all 10 IPL teams → match winner probability
  - Historical average first-innings totals per team → team total over/under
  - Live score run-rate model → in-play match odds edge detection

ENV:
  CLOUDBET_API_KEY   — JWT from cloudbet.com/en/player/api
  CB_CURRENCY        — USDT (default), BTC, ETH, USDC
  CB_STAKE           — stake per bet in currency units (default 1.0 USDT)
  CB_DRY_RUN         — 1=scan only, 0=place real bets (default 1)
  ODDS_API_KEY       — for live score fetch
"""

import os, sys, json, time, logging, math, uuid
from datetime import datetime, timezone
from typing import Optional
import httpx
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY    = os.getenv("CLOUDBET_API_KEY", "")
CURRENCY   = os.getenv("CB_CURRENCY", "USDT")
STAKE      = float(os.getenv("CB_STAKE", "1.0"))
DRY_RUN    = os.getenv("CB_DRY_RUN", "1") != "0"
ODDS_KEY   = os.getenv("ODDS_API_KEY", "")
MIN_EDGE   = 0.04   # minimum model edge (4%) to place bet
MAX_ODDS   = 8.0
MIN_ODDS   = 1.15
LOOP_SECS  = 60

FEED_BASE   = "https://sports-api.cloudbet.com/pub/v2/odds"
TRADE_BASE  = "https://sports-api.cloudbet.com/pub/v4"
IPL_KEY     = "cricket-india-indian-premier-league"

# ── IPL Team ELO (2025-26 calibrated) ────────────────────────────────────────
ELO = {
    "Mumbai Indians":              1680,
    "Kolkata Knight Riders":       1645,
    "Chennai Super Kings":         1660,
    "Rajasthan Royals":            1635,
    "Royal Challengers Bangalore": 1625,
    "Gujarat Titans":              1605,
    "Sunrisers Hyderabad":         1615,
    "Lucknow Super Giants":        1585,
    "Delhi Capitals":              1595,
    "Punjab Kings":                1575,
}

# Historical avg first-innings T20 total per team (IPL 2022-25)
AVG_SCORE = {
    "Mumbai Indians":              174,
    "Kolkata Knight Riders":       170,
    "Chennai Super Kings":         172,
    "Rajasthan Royals":            176,
    "Royal Challengers Bangalore": 178,
    "Gujarat Titans":              168,
    "Sunrisers Hyderabad":         182,
    "Lucknow Super Giants":        165,
    "Delhi Capitals":              169,
    "Punjab Kings":                177,
}

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("cloudbet_bot.log", encoding="utf-8"),
    ]
)
log = logging.getLogger("cb_bot")

# ── HTTP helpers ───────────────────────────────────────────────────────────────
def _headers():
    return {"X-Api-Key": API_KEY, "Accept": "application/json", "Content-Type": "application/json"}

def feed_get(path: str, params: dict = None) -> dict:
    r = httpx.get(f"{FEED_BASE}{path}", headers=_headers(), params=params, timeout=10)
    r.raise_for_status()
    return r.json()

def trade_post(path: str, body: dict) -> dict:
    r = httpx.post(f"{TRADE_BASE}{path}", headers=_headers(), json=body, timeout=10)
    return r.json()

def trade_get(path: str, params: dict = None) -> dict:
    r = httpx.get(f"{TRADE_BASE}{path}", headers=_headers(), params=params, timeout=10)
    r.raise_for_status()
    return r.json()

# ── Models ─────────────────────────────────────────────────────────────────────
def elo_win_prob(home: str, away: str) -> tuple[float, float]:
    rh = ELO.get(home, 1600)
    ra = ELO.get(away, 1600)
    ph = 1 / (1 + 10 ** ((ra - rh) / 400))
    return round(ph, 4), round(1 - ph, 4)

def fair_odds(p: float) -> float:
    return round(1 / p, 3) if p > 0 else 999.0

def team_total_model(team: str, opponent: str, is_home: bool) -> float:
    """Predict first-innings total for `team` vs `opponent`."""
    base = AVG_SCORE.get(team, 168)
    # Opponent bowling quality — weaker batting avg opponent → higher total
    opp_avg = AVG_SCORE.get(opponent, 170)
    # Adjust: strong batting team + weak bowling opponent = higher
    adj = (base - 170) * 0.4 + (170 - opp_avg) * 0.3
    total = base + adj + (3 if is_home else 0)
    return round(total, 1)

def inplay_model(score_str_batting: str, target: Optional[int], overs_done: float) -> float:
    """
    Returns win probability for batting team.
    score_str_batting: e.g. '87/3'
    target: runs needed to win (2nd innings) or None (1st innings)
    """
    try:
        parts = score_str_batting.split("/")
        runs = int(parts[0])
        wkts = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return 0.5

    total_overs = 20.0
    if target is None:
        # First innings: project total and compare to avg
        if overs_done <= 0:
            return 0.5
        proj = runs / overs_done * total_overs
        # Higher projected total → batting team stronger position
        p = min(0.85, max(0.15, 0.5 + (proj - 168) / 200))
        return round(p, 4)
    else:
        # Second innings chase
        remaining_runs = target - runs
        remaining_overs = total_overs - overs_done
        if remaining_overs <= 0:
            return 1.0 if runs >= target else 0.0
        req_rate = remaining_runs / remaining_overs
        curr_rate = runs / max(overs_done, 0.1)
        wkts_left = 10 - wkts
        rate_diff = curr_rate - req_rate
        wkt_f = wkts_left / 10
        raw = rate_diff * 0.35 + wkt_f * 0.45
        p = 1 / (1 + math.exp(-raw * 5))
        return round(max(0.05, min(0.95, p)), 4)

# ── Live score ─────────────────────────────────────────────────────────────────
def fetch_score(team_a: str, team_b: str) -> dict:
    """Fetch live score from The Odds API scores endpoint."""
    score = {"team_a": team_a, "team_b": team_b, "live": False}
    if not ODDS_KEY:
        return score
    try:
        r = httpx.get(
            "https://api.the-odds-api.com/v4/sports/cricket_ipl/scores/",
            params={"apiKey": ODDS_KEY, "daysFrom": 1},
            timeout=8,
        )
        if r.status_code != 200:
            return score
        for ev in r.json():
            names = [s.get("name", "") for s in (ev.get("scores") or [])]
            if team_a in names or team_b in names:
                score["live"] = not ev.get("completed", True)
                score["completed"] = ev.get("completed", False)
                for s in (ev.get("scores") or []):
                    if s["name"] == team_a:
                        score["score_a"] = s.get("score", "0/0")
                    elif s["name"] == team_b:
                        score["score_b"] = s.get("score", "0/0")
                break
    except Exception as e:
        log.debug(f"Score fetch: {e}")
    return score

# ── Market discovery ───────────────────────────────────────────────────────────
def get_ipl_events() -> list:
    """Return all active IPL match events with their available markets."""
    data = feed_get(f"/competitions/{IPL_KEY}")
    events = []
    for ev in data.get("events", []):
        if ev.get("type") == "EVENT_TYPE_OUTRIGHT":
            continue
        if ev.get("status") not in ("TRADING", "TRADING_LIVE"):
            continue
        home = (ev.get("home") or {}).get("name", "")
        away = (ev.get("away") or {}).get("name", "")
        if not home or not away:
            continue
        events.append({
            "id":       ev["id"],
            "name":     ev["name"],
            "home":     home,
            "away":     away,
            "status":   ev["status"],
            "cutoff":   ev.get("cutoffTime", ""),
            "markets":  list(ev.get("markets", {}).keys()),
        })
    return events

def get_event_markets(event_id: int, market_keys: list) -> dict:
    """Get full market data for specific markets."""
    all_markets = {}
    for mkt in market_keys:
        try:
            d = feed_get(f"/events/{event_id}", params={"markets": mkt})
            mkts = d.get("markets", {})
            all_markets.update(mkts)
        except Exception as e:
            log.debug(f"Market {mkt} fetch: {e}")
    return all_markets

def parse_selections(market_data: dict) -> list:
    """
    Extract (label, price, marketUrl) from a market dict.
    Returns list of {label, price, market_url, outcome_key}
    """
    selections = []
    for sub_key, sub in market_data.get("submarkets", {}).items():
        for sel in sub.get("selections", []):
            price = sel.get("price", 0)
            if price and price > 1.0:
                selections.append({
                    "label":      sel.get("label", ""),
                    "price":      float(price),
                    "outcome":    sel.get("outcome", ""),
                    "params":     sel.get("params", ""),
                    "sub_key":    sub_key,
                })
    return selections

# ── Bet placement ──────────────────────────────────────────────────────────────
def place_bet(event_id: int, market_url: str, outcome: str,
              price: float, stake: float, currency: str) -> dict:
    ref_id = str(uuid.uuid4())
    body = {
        "currency":        currency,
        "eventId":         event_id,
        "marketUrl":       market_url,
        "outcome":         outcome,
        "price":           str(round(price, 4)),
        "stake":           str(round(stake, 4)),
        "referenceId":     ref_id,
        "priceVariation":  "NONE",
    }
    log.info(f"{'[DRY]' if DRY_RUN else '[LIVE]'} placeBet event={event_id} market={market_url} "
             f"outcome={outcome} price={price} stake={stake} {currency} ref={ref_id}")
    if DRY_RUN:
        return {"status": "DRY_RUN", "referenceId": ref_id}
    result = trade_post("/bets/place/straight", body)
    log.info(f"Bet result: {result}")
    return result

# ── Core trading logic ─────────────────────────────────────────────────────────
def trade_event(ev: dict):
    event_id = ev["id"]
    home     = ev["home"]
    away     = ev["away"]
    status   = ev["status"]
    live     = status == "TRADING_LIVE"

    log.info(f"{'[LIVE]' if live else '[Pre]'} | {home} vs {away} | id={event_id}")

    # ── Match odds (when available) ────────────────────────────────────────────
    if "cricket.match_odds" in ev["markets"]:
        mkts = get_event_markets(event_id, ["cricket.match_odds"])
        mkt  = mkts.get("cricket.match_odds", {})
        sels = parse_selections(mkt)

        ph, pa = elo_win_prob(home, away)
        fair_h = fair_odds(ph)
        fair_a = fair_odds(pa)

        if live:
            score = fetch_score(home, away)
            log.info(f"  Score: {home} {score.get('score_a','?')} | {away} {score.get('score_b','?')}")

        for sel in sels:
            label = sel["label"]
            price = sel["price"]
            if home.lower() in label.lower() or "1" == sel.get("outcome"):
                fair = fair_h
                p    = ph
            elif away.lower() in label.lower() or "2" == sel.get("outcome"):
                fair = fair_a
                p    = pa
            else:
                continue

            edge = price / fair - 1
            log.info(f"  {label}: market={price} fair={fair} edge={edge:+.1%}")

            if edge >= MIN_EDGE and MIN_ODDS <= price <= MAX_ODDS:
                log.info(f"  ✅ VALUE BET: {label} @ {price} (edge={edge:.1%})")
                market_url = f"{IPL_KEY}/{sel.get('params', '')}"
                place_bet(event_id, market_url, sel["outcome"], price, STAKE, CURRENCY)

    # ── Team totals (pre-match data-backed) ────────────────────────────────────
    if "cricket.team_totals" in ev["markets"] and not live:
        mkts = get_event_markets(event_id, ["cricket.team_totals"])
        mkt  = mkts.get("cricket.team_totals", {})
        sels = parse_selections(mkt)

        pred_home = team_total_model(home, away, is_home=True)
        pred_away = team_total_model(away, home, is_home=False)
        log.info(f"  Model: {home} proj={pred_home} | {away} proj={pred_away}")

        for sel in sels:
            label = sel["label"]
            price = sel["price"]
            # Label like "Delhi Capitals Over 165.5" or "RCB Under 172.5"
            parts = label.split()
            if len(parts) < 3:
                continue
            try:
                line = float(parts[-1])
                direction = "over" if "over" in label.lower() else "under"
                team_in_label = home if home.split()[0].lower() in label.lower() \
                                     or home.split()[-1].lower() in label.lower() \
                                     else away
                pred = pred_home if team_in_label == home else pred_away
                # Fair prob
                if direction == "over":
                    p_model = min(0.95, max(0.05, 0.5 + (pred - line) / 40))
                else:
                    p_model = min(0.95, max(0.05, 0.5 + (line - pred) / 40))
                fair = fair_odds(p_model)
                edge = price / fair - 1
                log.info(f"  {label}: market={price} model_fair={fair:.3f} edge={edge:+.1%}")
                if edge >= MIN_EDGE and MIN_ODDS <= price <= MAX_ODDS:
                    log.info(f"  ✅ TOTAL BET: {label} @ {price}")
                    place_bet(event_id, f"cricket.team_totals/{sel['params']}",
                              sel["outcome"], price, STAKE, CURRENCY)
            except Exception as e:
                log.debug(f"  Parse error {label}: {e}")

    # ── Over totals (in-play) ──────────────────────────────────────────────────
    if "cricket.over_team_total" in ev["markets"] and live:
        log.info("  [over_team_total available — in-play mode active]")
        # Could add per-over trading here based on bowler/batter matchups


def main():
    log.info("=" * 60)
    log.info(f"Cloudbet IPL Cricket Bot | DRY_RUN={DRY_RUN} | Currency={CURRENCY} | Stake={STAKE}")
    log.info("=" * 60)

    while True:
        try:
            events = get_ipl_events()
            log.info(f"Found {len(events)} active IPL match events")
            for ev in events:
                trade_event(ev)
        except Exception as e:
            log.error(f"Loop error: {e}", exc_info=True)
        time.sleep(LOOP_SECS)


def run_once():
    log.info("=== cloudbet_bot --once ===")
    if not API_KEY:
        log.error("CLOUDBET_API_KEY not set")
        return
    try:
        events = get_ipl_events()
        log.info(f"Found {len(events)} active IPL match events")
        for ev in events:
            trade_event(ev)
    except Exception as e:
        log.error(f"run_once error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_once()
    elif "--events" in sys.argv:
        # Just list events
        for ev in get_ipl_events():
            print(f"  {ev['id']} | {ev['home']} vs {ev['away']} | {ev['status']} | markets={len(ev['markets'])}")
    else:
        try:
            main()
        except KeyboardInterrupt:
            log.info("Stopped.")
