"""
IPL Arbitrage Bot
─────────────────
Finds guaranteed-profit (arb) opportunities across multiple bookmakers.

HOW ARB WORKS:
  If Bookmaker A has RCB @ 2.10 and Bookmaker B has GT @ 2.20:
    1/2.10 + 1/2.20 = 0.476 + 0.454 = 0.930  ← < 1.0 = 7% guaranteed profit!
  Stake $100 total:
    → Bet $52.2 on RCB@2.10  (returns $109.6 if RCB wins)
    → Bet $47.8 on GT@2.20   (returns $105.2 if GT wins)
  Both outcomes return > $100 — locked profit regardless of result.

SOURCES:
  1. The Odds API  — 40+ bookmakers, IPL covered, free 500 req/month
  2. Polymarket    — crypto prediction market, IPL match markets
  3. stake.pet     — crypto sportsbook (via Brave browser)

SETUP:
  1. Get free API key: https://the-odds-api.com  (no CC needed)
  2. Get free CricAPI key: https://cricapi.com   (100 calls/day)
  3. Fill .env file:
       ODDS_API_KEY=your_key_here
       CRICAPI_KEY=your_key_here
       POLY_PRIVATE_KEY=0x...  (optional, for auto-placing on Polymarket)
  4. python arb_bot.py

MODES:
  Alert mode (default): prints arb opportunities to console
  Auto mode (--auto):   places bets on Polymarket when arb vs other bookmaker detected
"""

import asyncio, json, logging, sys, io, os, httpx, itertools
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Config ────────────────────────────────────────────────────────────────────
ODDS_API_KEY   = os.getenv("ODDS_API_KEY",   "")   # the-odds-api.com   free:500/month
BETSAPI_TOKEN  = os.getenv("BETSAPI_TOKEN",  "")   # betsapi.com        in-play real-time, $1 trial
ODDSPAPI_KEY   = os.getenv("ODDSPAPI_KEY",   "")   # oddspapi.io        free:250/month, 350+ bookmakers
POLY_PRIV_KEY  = os.getenv("POLY_PRIVATE_KEY", "")
POLY_API_KEY   = os.getenv("POLY_API_KEY",   "")
POLY_API_SECRET= os.getenv("POLY_API_SECRET","")
POLY_API_PASS  = os.getenv("POLY_API_PASSPHRASE", "")

TOTAL_BANK_USD = float(os.getenv("ARB_BANK_USD",  "50.0"))   # total bankroll to arb with
MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT", "0.5"))   # min 0.5% guaranteed profit
AUTO_BET       = "--auto" in sys.argv                         # auto-place bets on Polymarket
ONCE_MODE      = "--once" in sys.argv                         # run one cycle and exit (for GitHub Actions)

ODDS_API_URL   = "https://api.the-odds-api.com/v4"
GAMMA_URL      = "https://gamma-api.polymarket.com"
CLOB_URL       = "https://clob.polymarket.com"
LOOP_SECS      = 60    # poll every 60 seconds (conserve free API quota)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("arb_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("arb")

# ── Shared HTTP client ────────────────────────────────────────────────────────
_client: Optional[httpx.AsyncClient] = None

async def http_get(url: str, params: dict = None) -> any:
    global _client
    if not _client:
        _client = httpx.AsyncClient(timeout=12, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"})
    r = await _client.get(url, params=params or {})
    r.raise_for_status()
    return r.json()


# ══════════════════════════════════════════════════════════════════════════════
# Source 1: The Odds API — aggregates 40+ bookmakers
# ══════════════════════════════════════════════════════════════════════════════

CRICKET_SPORT_KEY = "cricket_ipl"   # The Odds API sport key for IPL

async def get_odds_api_markets() -> list:
    """
    Fetch IPL match-winner odds from The Odds API.
    Returns list of: {event, home, away, bookmakers: [{name, home_odds, away_odds}]}
    Free tier: 500 requests/month. Each call uses 1 request.
    """
    if not ODDS_API_KEY:
        return []
    try:
        data = await http_get(f"{ODDS_API_URL}/sports/{CRICKET_SPORT_KEY}/odds", {
            "apiKey":    ODDS_API_KEY,
            "regions":   "uk,eu,us,au",     # all regions = most bookmakers
            "markets":   "h2h",             # head-to-head = match winner
            "oddsFormat":"decimal",
        })
        if not isinstance(data, list):
            log.debug(f"Odds API: {data}")
            return []

        events = []
        for ev in data:
            home = ev.get("home_team","")
            away = ev.get("away_team","")
            start= ev.get("commence_time","")
            bkms = []
            for bk in (ev.get("bookmakers") or []):
                mkt = next((m for m in bk.get("markets",[]) if m.get("key")=="h2h"), None)
                if not mkt: continue
                ocs = mkt.get("outcomes",[])
                home_oc = next((o for o in ocs if o["name"]==home), None)
                away_oc = next((o for o in ocs if o["name"]==away), None)
                if home_oc and away_oc:
                    bkms.append({
                        "name":      bk.get("title","?"),
                        "home_odds": float(home_oc["price"]),
                        "away_odds": float(away_oc["price"]),
                    })
            if bkms:
                events.append({"event": f"{home} v {away}", "home": home, "away": away,
                                "start": start, "bookmakers": bkms})
        log.info(f"Odds API: {len(events)} IPL events, {sum(len(e['bookmakers']) for e in events)} bookmaker-event pairs")
        return events

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            log.warning("Odds API: invalid key. Get one free at https://the-odds-api.com")
        elif e.response.status_code == 422:
            log.warning(f"Odds API: sport key '{CRICKET_SPORT_KEY}' not found. Trying generic cricket...")
            return await _get_odds_api_cricket_fallback()
        else:
            log.warning(f"Odds API HTTP {e.response.status_code}")
        return []
    except Exception as e:
        log.warning(f"Odds API: {e}")
        return []


async def _get_odds_api_cricket_fallback() -> list:
    """Try to find the correct cricket sport key for IPL."""
    try:
        sports = await http_get(f"{ODDS_API_URL}/sports", {"apiKey": ODDS_API_KEY})
        cricket_keys = [s["key"] for s in sports if "cricket" in s.get("key","").lower()]
        log.info(f"Available cricket sport keys: {cricket_keys}")
        for key in cricket_keys:
            try:
                data = await http_get(f"{ODDS_API_URL}/sports/{key}/odds", {
                    "apiKey": ODDS_API_KEY, "regions": "uk,eu", "markets": "h2h", "oddsFormat": "decimal"
                })
                if isinstance(data, list) and data:
                    log.info(f"Found matches under sport key: {key}")
                    global CRICKET_SPORT_KEY
                    CRICKET_SPORT_KEY = key
                    return await get_odds_api_markets()
            except Exception:
                pass
    except Exception as e:
        log.debug(f"Odds API fallback: {e}")
    return []


# ══════════════════════════════════════════════════════════════════════════════
# Source 2: Polymarket — crypto prediction market
# ══════════════════════════════════════════════════════════════════════════════

IPL_KW = ["ipl","indian premier","rajasthan","mumbai","chennai","kolkata",
          "delhi","punjab","hyderabad","gujarat","lucknow","bengaluru","bangalore",
          "rcb","csk","kkr","srh","mi ","dc ","gt ","lsg","pbks"]

def _is_ipl(text: str) -> bool:
    t = text.lower()
    if "ipl" in t or "indian premier" in t: return True
    return sum(1 for k in IPL_KW if k in t) >= 2


async def get_polymarket_odds() -> list:
    """
    Fetch IPL match odds from Polymarket.
    Returns list of: {event, home, away, source:'polymarket', home_odds, away_odds, token_home, token_away}
    """
    results = []
    try:
        # Try multiple search strategies
        raw = []
        for params in [
            {"active":"true","closed":"false","tag_slug":"cricket","limit":100},
            {"active":"true","closed":"false","q":"IPL","limit":100},
        ]:
            try:
                data = await http_get(f"{GAMMA_URL}/markets", params)
                candidates = data if isinstance(data, list) else (data.get("data") or data.get("markets") or [])
                if candidates:
                    raw = candidates
                    break
            except Exception:
                pass

        for m in raw:
            if not _is_ipl(f"{m.get('question','')} {m.get('slug','')}"):
                continue
            tokens = m.get("tokens") or []
            if len(tokens) < 2: continue
            prices_raw = m.get("outcomePrices") or []
            if isinstance(prices_raw, str):
                try: prices_raw = json.loads(prices_raw)
                except: prices_raw = []
            prices = [float(p) for p in prices_raw[:2]] if prices_raw else [0.5, 0.5]

            t0 = tokens[0]; t1 = tokens[1]
            n0 = t0.get("outcome") or t0.get("name","Team A")
            n1 = t1.get("outcome") or t1.get("name","Team B")
            p0 = prices[0] if prices else 0.5
            p1 = prices[1] if prices else 0.5
            # Convert Polymarket price (0-1) to decimal odds
            odds0 = round(1/p0, 3) if p0 > 0.01 else 99.0
            odds1 = round(1/p1, 3) if p1 > 0.01 else 99.0

            results.append({
                "event":      m.get("question",""),
                "home":       n0, "away":       n1,
                "home_price": p0, "away_price": p1,
                "home_odds":  odds0, "away_odds": odds1,
                "source":     "polymarket",
                "token_home": t0.get("token_id",""), "token_away": t1.get("token_id",""),
                "volume":     float(m.get("volume","0") or 0),
            })
        log.debug(f"Polymarket: {len(results)} IPL markets")
    except Exception as e:
        log.debug(f"Polymarket: {e}")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Arbitrage detection
# ══════════════════════════════════════════════════════════════════════════════

TEAM_MAP = {
    "royal challengers bengaluru": ["rcb","bangalore","bengaluru","royal challengers"],
    "rajasthan royals":            ["rr","rajasthan"],
    "mumbai indians":              ["mi","mumbai"],
    "chennai super kings":         ["csk","chennai"],
    "kolkata knight riders":       ["kkr","kolkata"],
    "delhi capitals":              ["dc","delhi"],
    "punjab kings":                ["pbks","punjab"],
    "sunrisers hyderabad":         ["srh","hyderabad","sunrisers"],
    "gujarat titans":              ["gt","gujarat"],
    "lucknow super giants":        ["lsg","lucknow"],
}

def _canonical(name: str) -> str:
    n = name.lower()
    for full, aliases in TEAM_MAP.items():
        if any(a in n for a in aliases) or n in full:
            return full
    return n


def _teams_match(t1: str, t2: str) -> bool:
    return _canonical(t1) == _canonical(t2)


def find_arb_opportunities(
    odds_events: list,
    poly_events: list,
) -> list:
    """
    Cross-compare odds from The Odds API and Polymarket to find arb opportunities.
    Also finds arb WITHIN The Odds API events (across different bookmakers).

    Returns list of arb dicts with profit%, stake breakdown, and instructions.
    """
    opportunities = []

    # ── 1. Arb within The Odds API (across bookmakers) ────────────────────────
    for ev in odds_events:
        bkms = ev["bookmakers"]
        home, away = ev["home"], ev["away"]
        # Best odds for each side across all bookmakers
        best_home = max(bkms, key=lambda b: b["home_odds"])
        best_away = max(bkms, key=lambda b: b["away_odds"])
        oh = best_home["home_odds"]
        oa = best_away["away_odds"]
        arb_pct = (1/oh + 1/oa)
        profit_pct = (1 - arb_pct) * 100
        if profit_pct >= MIN_PROFIT_PCT:
            stake_h = TOTAL_BANK_USD * (1/oh) / arb_pct
            stake_a = TOTAL_BANK_USD * (1/oa) / arb_pct
            opportunities.append({
                "event":       ev["event"],
                "profit_pct":  round(profit_pct, 2),
                "profit_usd":  round(TOTAL_BANK_USD * profit_pct/100, 2),
                "legs": [
                    {"team": home, "odds": oh, "bookmaker": best_home["name"],
                     "stake": round(stake_h, 2), "returns": round(stake_h*oh, 2)},
                    {"team": away, "odds": oa, "bookmaker": best_away["name"],
                     "stake": round(stake_a, 2), "returns": round(stake_a*oa, 2)},
                ],
                "can_auto": False,   # need accounts on both bookmakers
                "sources":   "odds_api",
            })

    # ── 2. Arb between The Odds API and Polymarket ────────────────────────────
    for ev in odds_events:
        home, away = ev["home"], ev["away"]
        # Find matching Polymarket event
        poly_ev = None
        for pe in poly_events:
            if (_teams_match(home, pe["home"]) and _teams_match(away, pe["away"])
                    or _teams_match(home, pe["away"]) and _teams_match(away, pe["home"])):
                poly_ev = pe
                break
        if not poly_ev: continue

        # For each bookmaker, check arb against Polymarket
        for bk in ev["bookmakers"]:
            # Case A: back home on this bookmaker, back away on Polymarket
            oh = bk["home_odds"]
            if _teams_match(home, poly_ev["home"]):
                oa_poly = poly_ev["away_odds"]; tok = poly_ev.get("token_away","")
            else:
                oa_poly = poly_ev["home_odds"]; tok = poly_ev.get("token_home","")
            arb = 1/oh + 1/oa_poly
            profit_pct = (1 - arb) * 100
            if profit_pct >= MIN_PROFIT_PCT:
                stake_h = TOTAL_BANK_USD * (1/oh) / arb
                stake_a = TOTAL_BANK_USD * (1/oa_poly) / arb
                opportunities.append({
                    "event":      ev["event"],
                    "profit_pct": round(profit_pct, 2),
                    "profit_usd": round(TOTAL_BANK_USD * profit_pct/100, 2),
                    "legs": [
                        {"team": home, "odds": oh,      "bookmaker": bk["name"],
                         "stake": round(stake_h,2), "returns": round(stake_h*oh,2)},
                        {"team": away, "odds": oa_poly, "bookmaker": "Polymarket",
                         "stake": round(stake_a,2), "returns": round(stake_a*oa_poly,2),
                         "poly_token": tok, "poly_price": round(1/oa_poly,3)},
                    ],
                    "can_auto": bool(POLY_PRIV_KEY and tok),
                    "sources":  f"{bk['name']} + Polymarket",
                })

            # Case B: back away on this bookmaker, back home on Polymarket
            oa = bk["away_odds"]
            if _teams_match(home, poly_ev["home"]):
                oh_poly = poly_ev["home_odds"]; tok2 = poly_ev.get("token_home","")
            else:
                oh_poly = poly_ev["away_odds"]; tok2 = poly_ev.get("token_away","")
            arb2 = 1/oa + 1/oh_poly
            profit_pct2 = (1 - arb2) * 100
            if profit_pct2 >= MIN_PROFIT_PCT:
                stake_a = TOTAL_BANK_USD * (1/oa) / arb2
                stake_h = TOTAL_BANK_USD * (1/oh_poly) / arb2
                opportunities.append({
                    "event":      ev["event"],
                    "profit_pct": round(profit_pct2, 2),
                    "profit_usd": round(TOTAL_BANK_USD * profit_pct2/100, 2),
                    "legs": [
                        {"team": away, "odds": oa,      "bookmaker": bk["name"],
                         "stake": round(stake_a,2), "returns": round(stake_a*oa,2)},
                        {"team": home, "odds": oh_poly, "bookmaker": "Polymarket",
                         "stake": round(stake_h,2), "returns": round(stake_h*oh_poly,2),
                         "poly_token": tok2, "poly_price": round(1/oh_poly,3)},
                    ],
                    "can_auto": bool(POLY_PRIV_KEY and tok2),
                    "sources":  f"{bk['name']} + Polymarket",
                })

    # Sort by profit descending
    opportunities.sort(key=lambda x: x["profit_pct"], reverse=True)
    return opportunities


def print_arb(arb: dict):
    """Print a single arb opportunity clearly."""
    log.info("=" * 60)
    log.info(f"🎯 ARB FOUND: {arb['event']}")
    log.info(f"   Guaranteed profit: {arb['profit_pct']:.2f}%  =  ${arb['profit_usd']:.2f}  on ${TOTAL_BANK_USD:.0f} bank")
    log.info(f"   Sources: {arb['sources']}")
    for leg in arb["legs"]:
        extra = ""
        if "poly_price" in leg:
            extra = f"  [buy YES at ${leg['poly_price']:.3f}]"
        log.info(f"   → Bet ${leg['stake']:.2f} on {leg['team']} @ {leg['odds']:.2f}x on {leg['bookmaker']}{extra}")
        log.info(f"      Returns ${leg['returns']:.2f} if wins")
    if arb["can_auto"]:
        log.info("   ✅ Can AUTO-PLACE Polymarket leg (has private key)")
    else:
        log.info("   ⚠️  Manual action needed — place both bets yourself")
    log.info("=" * 60)


# ══════════════════════════════════════════════════════════════════════════════
# Auto-place on Polymarket (when arb detected with Polymarket leg)
# ══════════════════════════════════════════════════════════════════════════════

async def auto_place_polymarket(leg: dict) -> bool:
    """Place the Polymarket leg of an arb bet automatically."""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType, ApiCreds
    from py_clob_client.constants import BUY
    try:
        creds = None
        if POLY_API_KEY:
            creds = ApiCreds(api_key=POLY_API_KEY, api_secret=POLY_API_SECRET,
                             api_passphrase=POLY_API_PASS)
        client = ClobClient(host=CLOB_URL, chain_id=137, private_key=POLY_PRIV_KEY, creds=creds)
        price       = leg["poly_price"]
        stake_usdc  = leg["stake"]
        size_shares = round(stake_usdc / price, 2)
        order_args  = OrderArgs(
            token_id=leg["poly_token"],
            price=round(price, 4),
            size=size_shares,
            side=BUY,
            order_type=OrderType.FOK,
        )
        signed = client.create_order(order_args)
        resp   = await asyncio.to_thread(client.post_order, signed, orderType=OrderType.FOK)
        order_id = (resp or {}).get("orderID","")
        log.info(f"Polymarket order placed: id={order_id} | {size_shares} shares @ {price}")
        return bool(order_id)
    except Exception as e:
        log.error(f"auto_place_polymarket: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Main loop
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    log.info("=" * 60)
    log.info("IPL Arbitrage Bot — multi-source odds scanner")
    log.info(f"Bank: ${TOTAL_BANK_USD} | Min profit: {MIN_PROFIT_PCT}% | Auto: {AUTO_BET}")
    log.info("=" * 60)

    if not ODDS_API_KEY:
        log.warning("ODDS_API_KEY not set — get a FREE key at https://the-odds-api.com")
        log.warning("Free tier: 500 requests/month — enough for ~8 hours of polling")
        log.warning("Continuing with Polymarket-only arb detection...")

    seen_arbs: set = set()   # deduplicate alerts
    requests_used = 0
    cycle = 0

    while True:
        cycle += 1
        try:
            # Fetch odds from all sources in parallel
            tasks = [get_polymarket_odds()]
            if ODDS_API_KEY:
                tasks.append(get_odds_api_markets())
            results = await asyncio.gather(*tasks, return_exceptions=True)

            poly_events  = results[0] if not isinstance(results[0], Exception) else []
            odds_events  = results[1] if len(results)>1 and not isinstance(results[1], Exception) else []

            if cycle % 5 == 1:
                log.info(f"Odds API events: {len(odds_events)} | Polymarket IPL markets: {len(poly_events)}")
                # Show all current prices
                for ev in poly_events[:5]:
                    log.info(f"  Polymarket: {ev['event'][:60]}")
                    log.info(f"    {ev['home']} @ {ev['home_odds']:.2f}x  |  {ev['away']} @ {ev['away_odds']:.2f}x")
                for ev in odds_events[:3]:
                    best_h = max(ev["bookmakers"], key=lambda b: b["home_odds"])
                    best_a = max(ev["bookmakers"], key=lambda b: b["away_odds"])
                    log.info(f"  Odds API: {ev['event'][:60]}")
                    log.info(f"    {ev['home']} best @ {best_h['home_odds']:.2f}x ({best_h['name']})")
                    log.info(f"    {ev['away']} best @ {best_a['away_odds']:.2f}x ({best_a['name']})")

            # Detect arb
            arbs = find_arb_opportunities(odds_events, poly_events)
            if arbs:
                log.info(f"🎯 {len(arbs)} ARB OPPORTUNITIES FOUND!")
                for arb in arbs:
                    # Create dedup key
                    key = f"{arb['event']}:{arb['profit_pct']:.1f}:{arb['sources']}"
                    if key not in seen_arbs:
                        seen_arbs.add(key)
                        print_arb(arb)
                        # Auto-place Polymarket leg if enabled
                        if AUTO_BET and arb["can_auto"]:
                            poly_leg = next((l for l in arb["legs"] if l.get("bookmaker")=="Polymarket"), None)
                            if poly_leg:
                                log.info(f"AUTO-PLACING Polymarket leg...")
                                ok = await auto_place_polymarket(poly_leg)
                                if ok:
                                    log.info(f"✅ Polymarket bet placed. Manually place: {arb['legs'][0]['team']} on {arb['legs'][0]['bookmaker']}")
            else:
                if cycle % 5 == 1:
                    log.info("No arb found this cycle (normal — arbs are rare and short-lived)")

            # Clear old seen arbs every 30 min
            if cycle % 30 == 0:
                seen_arbs.clear()

            # GitHub Actions mode: run once and exit
            if ONCE_MODE:
                break

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Cycle error: {e}", exc_info=False)
            if ONCE_MODE:
                break

        if not ONCE_MODE:
            await asyncio.sleep(LOOP_SECS)

    log.info("Stopped.")
    if _client:
        await _client.aclose()


if __name__ == "__main__":
    if "--demo" in sys.argv:
        # Demo mode: show arb calculation with fake data
        print("\n=== ARB DEMO ===")
        demo_odds = [{"event": "RCB v GT", "home": "Royal Challengers Bengaluru",
                      "away": "Gujarat Titans",
                      "bookmakers": [
                          {"name": "Bet365",     "home_odds": 2.10, "away_odds": 1.80},
                          {"name": "Betfair",    "home_odds": 1.95, "away_odds": 2.05},
                          {"name": "1xBet",      "home_odds": 2.05, "away_odds": 1.95},
                      ]}]
        demo_poly = [{"event": "RCB to win vs GT?", "home": "Royal Challengers Bengaluru",
                      "away": "Gujarat Titans",
                      "home_odds": 1.85, "away_odds": 2.20,
                      "home_price": 0.54, "away_price": 0.46,
                      "source": "polymarket", "token_home": "FAKE1", "token_away": "FAKE2", "volume": 5000}]
        arbs = find_arb_opportunities(demo_odds, demo_poly)
        print(f"Found {len(arbs)} arb opportunities in demo data:")
        for arb in arbs:
            print_arb(arb)
        print("\nTo use for real: set ODDS_API_KEY in .env and run: python arb_bot.py")
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            log.info("Stopped by user.")
