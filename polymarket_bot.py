"""
Polymarket IPL Cricket Bot
──────────────────────────
Direct REST API — no browser, no geo-block, Railway-deployable.

HOW IT WORKS:
  • Polymarket has live IPL match-winner markets (YES/NO binary)
  • Price 0.65 = 65% chance = 1/0.65 = 1.54x equivalent odds
  • We buy YES (team to win) when our model says >68% confidence
  • We sell position when profit locked (price rose) or stop-loss

SETUP:
  1. Create/import a Polygon wallet  (MetaMask → export private key)
  2. Bridge USDC to Polygon (Polygon Bridge or CEX withdraw to Polygon)
  3. Approve CTF contract once:  python polymarket_bot.py --approve
  4. Set env vars (see .env section below) then:  python polymarket_bot.py

ENV VARS (create .env file or set in Railway):
  POLY_PRIVATE_KEY=0x...          your wallet private key
  POLY_API_KEY=...                from --create-api-key step
  POLY_API_SECRET=...
  POLY_API_PASSPHRASE=...
  MAX_STAKE_USDC=1.0              max $ per bet
  MIN_CONFIDENCE=0.68
"""

import asyncio, json, logging, sys, io, os, time, re, httpx
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Config ────────────────────────────────────────────────────────────────────
PRIVATE_KEY     = os.getenv("POLY_PRIVATE_KEY", "")
API_KEY         = os.getenv("POLY_API_KEY", "")
API_SECRET      = os.getenv("POLY_API_SECRET", "")
API_PASSPHRASE  = os.getenv("POLY_API_PASSPHRASE", "")
MAX_STAKE_USDC  = float(os.getenv("MAX_STAKE_USDC", "1.0"))
MIN_CONFIDENCE  = float(os.getenv("MIN_CONFIDENCE", "0.68"))

GAMMA_URL   = "https://gamma-api.polymarket.com"
CLOB_URL    = "https://clob.polymarket.com"
CHAIN_ID    = 137   # Polygon mainnet

# Optional: Brave CDP for India (Polymarket DNS-blocked by Indian ISPs)
# Set USE_BRAVE=1 to route Polymarket API calls through Brave browser
USE_BRAVE   = os.getenv("USE_BRAVE", "0") == "1"
CDP_URL     = os.getenv("CDP_URL", "http://localhost:9222")
_brave_page = None  # set during init if USE_BRAVE=1

LOOP_SECS   = 15
TOTAL_PAR   = 167   # IPL average total
BOOKSET_PCT = 0.25  # take profit when price moves 25% in our favour
STOP_LOSS   = 0.20  # stop loss when price moves 20% against us

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("polymarket_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("poly_bot")


# ══════════════════════════════════════════════════════════════════════════════
# Polymarket API helpers
# ══════════════════════════════════════════════════════════════════════════════

def _get_clob_client():
    """Build py-clob-client with stored credentials."""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    if not PRIVATE_KEY:
        raise RuntimeError("POLY_PRIVATE_KEY not set")
    creds = None
    if API_KEY:
        creds = ApiCreds(
            api_key=API_KEY,
            api_secret=API_SECRET,
            api_passphrase=API_PASSPHRASE,
        )
    return ClobClient(
        host=CLOB_URL,
        chain_id=CHAIN_ID,
        private_key=PRIVATE_KEY,
        creds=creds,
    )


async def _http_get(url: str, params: dict = None) -> any:
    """
    GET with httpx; if Polymarket is DNS-blocked (India) falls back to Brave CDP fetch.
    """
    full = url
    if params:
        from urllib.parse import urlencode
        full = f"{url}?{urlencode(params)}"
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.get(full)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        if _brave_page and ("polymarket" in url or "clob." in url):
            log.debug(f"Direct request failed ({e}) — using Brave browser fetch")
            js = f"""async () => {{
                const r = await fetch({json.dumps(full)});
                return {{status: r.status, body: await r.text()}};
            }}"""
            res = await _brave_page.evaluate(js)
            return json.loads(res.get("body","{}"))
        raise


async def gamma_get(path: str, params: dict = None) -> any:
    """GET from Polymarket Gamma API (public, no auth)."""
    return await _http_get(f"{GAMMA_URL}{path}", params)


async def clob_get(path: str, params: dict = None) -> any:
    """GET from CLOB API (public endpoints)."""
    return await _http_get(f"{CLOB_URL}{path}", params)


# ── Market discovery ──────────────────────────────────────────────────────────

IPL_KEYWORDS = ["ipl", "indian premier", "rajasthan", "mumbai", "chennai",
                "kolkata", "delhi", "punjab", "hyderabad", "gujarat",
                "lucknow", "bengaluru", "bangalore", "sunrisers", "rcb",
                "csk", "mi ", "kkr", "dc ", "srh", "gt ", "lsg", "pbks"]

def _is_ipl_market(market: dict) -> bool:
    text = f"{market.get('question','')} {market.get('description','')} {market.get('slug','')}".lower()
    if any(k in text for k in ["ipl", "indian premier"]):
        return True
    return sum(1 for k in IPL_KEYWORDS if k in text) >= 2


async def get_ipl_markets() -> list:
    """
    Fetch active IPL cricket markets from Polymarket Gamma API.
    Returns list of market dicts with outcome token IDs and current prices.
    """
    markets = []
    raw = []
    try:
        # Try multiple search strategies
        for params in [
            {"active": "true", "closed": "false", "tag_slug": "cricket",  "limit": 100, "order": "volume24hr"},
            {"active": "true", "closed": "false", "tag_id":   "1143",     "limit": 100, "order": "volume24hr"},  # cricket tag id
            {"active": "true", "closed": "false", "q":        "IPL",      "limit": 100, "order": "volume24hr"},
            {"active": "true", "closed": "false", "q":        "cricket",  "limit": 100, "order": "volume24hr"},
        ]:
            try:
                data = await gamma_get("/markets", params)
                if isinstance(data, list):
                    raw = data
                else:
                    raw = data.get("data") or data.get("markets") or []
                if raw:
                    log.debug(f"Gamma API returned {len(raw)} markets with params {params}")
                    break
            except Exception as ex:
                log.debug(f"Gamma query {params}: {ex}")
                continue

        for m in raw:
            if not _is_ipl_market(m):
                continue
            tokens = m.get("tokens") or m.get("outcomes") or []
            if len(tokens) < 2:
                continue
            # Parse prices — stored as JSON string or list
            prices = m.get("outcomePrices") or m.get("prices") or []
            if isinstance(prices, str):
                try:
                    prices = json.loads(prices)
                except Exception:
                    prices = []
            # Build market object
            outcomes = []
            for i, tok in enumerate(tokens):
                tok_id = tok.get("token_id") or tok.get("tokenId") or tok.get("id","")
                name   = tok.get("outcome") or tok.get("name","")
                price  = 0.5
                if i < len(prices):
                    try:
                        price = float(prices[i])
                    except Exception:
                        pass
                if tok_id:
                    outcomes.append({"token_id": tok_id, "name": name, "price": price})

            if outcomes:
                markets.append({
                    "condition_id": m.get("conditionId") or m.get("id",""),
                    "question":     m.get("question",""),
                    "slug":         m.get("slug",""),
                    "volume":       float(m.get("volume","0") or m.get("volume24hr","0") or 0),
                    "outcomes":     outcomes,
                })

        log.debug(f"Found {len(markets)} IPL markets on Polymarket")
    except Exception as e:
        log.warning(f"get_ipl_markets: {e}")

    return markets


async def refresh_prices(markets: list) -> list:
    """Update current prices for each outcome from CLOB order book."""
    for mkt in markets:
        for oc in mkt.get("outcomes", []):
            try:
                data = await clob_get("/midpoint", {"token_id": oc["token_id"]})
                mid = float(data.get("mid") or oc["price"])
                oc["price"] = round(mid, 4)
            except Exception:
                pass
    return markets


# ── Order placement ───────────────────────────────────────────────────────────

def place_order_sync(token_id: str, price: float, size_usdc: float, side: str = "BUY") -> dict:
    """
    Place a limit order on Polymarket CLOB.
    price: 0.0–1.0 (probability / USDC per share)
    size_usdc: dollar amount to spend
    side: "BUY" or "SELL"
    Returns order response dict.
    """
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.constants import BUY, SELL
    client = _get_clob_client()
    side_const = BUY if side == "BUY" else SELL
    # size in shares = USDC_amount / price_per_share
    size_shares = round(size_usdc / price, 2) if price > 0 else size_usdc
    order_args = OrderArgs(
        token_id=token_id,
        price=round(price, 4),
        size=size_shares,
        side=side_const,
        order_type=OrderType.FOK,   # Fill-or-Kill for immediacy
    )
    signed = client.create_order(order_args)
    resp   = client.post_order(signed, orderType=OrderType.FOK)
    return resp if isinstance(resp, dict) else {"error": str(resp)}


def get_wallet_balance_sync() -> float:
    """Get USDC balance from Polymarket CLOB."""
    try:
        client = _get_clob_client()
        bal    = client.get_balance()
        return float(bal)
    except Exception as e:
        log.warning(f"get_wallet_balance: {e}")
        return 0.0


# ══════════════════════════════════════════════════════════════════════════════
# Live IPL score — httpx direct OR Brave browser fetch (India bypass)
# ══════════════════════════════════════════════════════════════════════════════

# JS for fetching live score via browser (works from India, bypasses API blocks)
_BROWSER_SCORE_JS = r"""
async () => {
    const TEAMS = ["mumbai","chennai","kolkata","rajasthan","delhi","punjab",
                   "hyderabad","gujarat","lucknow","bengaluru","bangalore"];
    function isIPL(s) {
        const t=(s||"").toLowerCase();
        if(t.includes("ipl")||t.includes("indian premier")) return true;
        return TEAMS.filter(n=>t.includes(n)).length>=2;
    }
    function parseOv(s){try{return parseFloat(s)||0;}catch{return 0;}}
    // CricInfo
    try {
        const r=await fetch("https://hs-consumer-api.espncricinfo.com/v1/pages/matches/current?lang=en&latest=true",
                            {headers:{"Accept":"application/json"}});
        if(r.ok){
            const d=await r.json();
            const matches=d.matches||(d.content||{}).matches||[];
            for(const m of matches){
                if(!isIPL((m.shortTitle||"")+" "+(m.description||""))) continue;
                if(!["live","in progress","in"].includes((m.state||"").toLowerCase())) continue;
                const teams=m.teams||[]; if(teams.length<2) continue;
                const ta=teams[0].longName||teams[0].name||"A";
                const tb=teams[1].longName||teams[1].name||"B";
                const inns=(m.matchScore||{}).innings||[];
                if(!inns.length) continue;
                const i1=inns[0]||{},i2=inns[1]||{};
                const r1=i1.runs||0,w1=i1.wickets||0,o1=parseOv(i1.overs);
                const r2=i2.runs||0,w2=i2.wickets||0,o2=parseOv(i2.overs);
                let runs,wkts,overs,batting,innings,target;
                if(!o2){runs=r1;wkts=w1;overs=o1;batting=ta;innings=1;target=0;}
                else   {runs=r2;wkts=w2;overs=o2;batting=tb;innings=2;target=r1+1;}
                const crr=overs>0?Math.round(runs/overs*100)/100:0;
                let rrr=0;
                if(innings==2&&target>0&&overs<20){const b=Math.max(1,(20-overs)*6);rrr=Math.round((target-runs)/b*600)/100;}
                return {team_a:ta,team_b:tb,batting,innings,runs,wkts,overs,crr,rrr,target,match_id:String(m.id||"1"),venue:""};
            }
        }
    } catch(e){}
    // Cricbuzz
    try {
        const r2=await fetch("https://www.cricbuzz.com/api/cricket-match/live",{headers:{Referer:"https://www.cricbuzz.com/"}});
        if(r2.ok){
            const d2=await r2.json();
            for(const tm of(d2.typeMatches||[])) for(const sm of(tm.seriesMatches||[])) for(const m of((sm.seriesAdWrapper||{}).matches||[])){
                const info=m.matchInfo||{};
                if(!isIPL((info.seriesName||"")+" "+((info.team1||{}).teamName||""))) continue;
                if(info.state!=="In Progress") continue;
                const t1=(info.team1||{}).teamName||"A",t2=(info.team2||{}).teamName||"B";
                const sc=m.matchScore||{};
                const i1=((sc.team1Score||{}).inngs1)||{},i2=((sc.team2Score||{}).inngs1)||{};
                const r1=i1.runs||0,w1=i1.wickets||0,o1=parseOv(i1.overs);
                const r2b=i2.runs||0,w2=i2.wickets||0,o2=parseOv(i2.overs);
                let runs,wkts,overs,batting,innings,target;
                if(!o2){runs=r1;wkts=w1;overs=o1;batting=t1;innings=1;target=0;}
                else   {runs=r2b;wkts=w2;overs=o2;batting=t2;innings=2;target=r1+1;}
                const crr=overs>0?Math.round(runs/overs*100)/100:0;
                let rrr=0;
                if(innings==2&&target>0&&overs<20){const b=Math.max(1,(20-overs)*6);rrr=Math.round((target-runs)/b*600)/100;}
                return {team_a:t1,team_b:t2,batting,innings,runs,wkts,overs,crr,rrr,target,match_id:String(info.matchId||"1"),venue:""};
            }
        }
    } catch(e){}
    return null;
}
"""

TEAM_NAMES = ["mumbai","chennai","kolkata","rajasthan","delhi","punjab",
              "hyderabad","gujarat","lucknow","bengaluru","bangalore"]

def _is_ipl(text: str) -> bool:
    t = text.lower()
    if "ipl" in t or "indian premier" in t:
        return True
    return sum(1 for n in TEAM_NAMES if n in t) >= 2

def _parse_ov(v) -> float:
    try: return float(v)
    except: return 0.0


async def get_live_score() -> Optional[dict]:
    """Fetch live IPL score. Uses Brave browser if available (India), else plain httpx."""
    # Brave browser path — works from India, bypasses API blocks
    if _brave_page:
        try:
            result = await _brave_page.evaluate(_BROWSER_SCORE_JS)
            if result and isinstance(result, dict):
                log.debug("Score via Brave browser fetch")
                return result
        except Exception as e:
            log.debug(f"Browser score: {e}")

    hdrs_es = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    hdrs_cb = {**hdrs_es, "Referer": "https://www.cricbuzz.com/"}

    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as c:
        # ── Try CricInfo ──────────────────────────────────────────────────────
        try:
            r = await c.get(
                "https://hs-consumer-api.espncricinfo.com/v1/pages/matches/current"
                "?lang=en&latest=true",
                headers=hdrs_es,
            )
            if r.status_code == 200:
                data = r.json()
                for m in (data.get("matches") or (data.get("content") or {}).get("matches") or []):
                    title = f"{m.get('shortTitle','')} {m.get('description','')}".lower()
                    if not _is_ipl(title): continue
                    if (m.get("state","")).lower() not in ("live","in progress","in"): continue
                    teams = m.get("teams",[])
                    if len(teams) < 2: continue
                    ta = teams[0].get("longName") or teams[0].get("name","A")
                    tb = teams[1].get("longName") or teams[1].get("name","B")
                    sd   = m.get("matchScore",{})
                    inns = sd.get("innings",[])
                    if not inns: continue
                    i1,i2 = (inns[0] if inns else {}),(inns[1] if len(inns)>1 else {})
                    r1,w1,o1 = i1.get("runs",0),i1.get("wickets",0),_parse_ov(i1.get("overs",0))
                    r2,w2,o2 = i2.get("runs",0),i2.get("wickets",0),_parse_ov(i2.get("overs",0))
                    if not o2: runs,wkts,overs,batting,innings,target = r1,w1,o1,ta,1,0
                    else:      runs,wkts,overs,batting,innings,target = r2,w2,o2,tb,2,r1+1
                    crr = round(runs/overs,2) if overs>0 else 0.0
                    rrr = 0.0
                    if innings==2 and target>0 and overs<20:
                        balls = max(1,(20-overs)*6)
                        rrr = round((target-runs)/balls*6,2) if target>runs else 0.0
                    return {"team_a":ta,"team_b":tb,"batting":batting,"innings":innings,
                            "runs":runs,"wkts":wkts,"overs":overs,"crr":crr,"rrr":rrr,
                            "target":target,"match_id":str(m.get("id","1")),"venue":""}
        except Exception as e:
            log.debug(f"CricInfo: {e}")

        # ── Try Cricbuzz ──────────────────────────────────────────────────────
        try:
            r = await c.get("https://www.cricbuzz.com/api/cricket-match/live", headers=hdrs_cb)
            if r.status_code == 200:
                data = r.json()
                for tm in (data.get("typeMatches") or []):
                    for sm in (tm.get("seriesMatches") or []):
                        for m in ((sm.get("seriesAdWrapper") or {}).get("matches") or []):
                            info = m.get("matchInfo",{})
                            if not _is_ipl(f"{info.get('seriesName','')} {(info.get('team1') or {}).get('teamName','')}"):
                                continue
                            if info.get("state") != "In Progress": continue
                            t1 = (info.get("team1") or {}).get("teamName","A")
                            t2 = (info.get("team2") or {}).get("teamName","B")
                            sc = m.get("matchScore",{})
                            i1 = ((sc.get("team1Score") or {}).get("inngs1") or {})
                            i2 = ((sc.get("team2Score") or {}).get("inngs1") or {})
                            r1,w1,o1 = i1.get("runs",0),i1.get("wickets",0),_parse_ov(i1.get("overs",0))
                            r2,w2,o2 = i2.get("runs",0),i2.get("wickets",0),_parse_ov(i2.get("overs",0))
                            if not o2: runs,wkts,overs,batting,innings,target = r1,w1,o1,t1,1,0
                            else:      runs,wkts,overs,batting,innings,target = r2,w2,o2,t2,2,r1+1
                            crr = round(runs/overs,2) if overs>0 else 0.0
                            rrr = 0.0
                            if innings==2 and target>0 and overs<20:
                                balls=max(1,(20-overs)*6)
                                rrr=round((target-runs)/balls*6,2) if target>runs else 0.0
                            return {"team_a":t1,"team_b":t2,"batting":batting,"innings":innings,
                                    "runs":runs,"wkts":wkts,"overs":overs,"crr":crr,"rrr":rrr,
                                    "target":target,"match_id":str(info.get("matchId","1")),"venue":""}
        except Exception as e:
            log.debug(f"Cricbuzz: {e}")

        # ── Try ESPN scoreboard ───────────────────────────────────────────────
        for url in [
            "https://site.api.espn.com/apis/site/v2/sports/cricket/ipl/scoreboard",
            "https://site.api.espn.com/apis/site/v2/sports/cricket/scoreboard",
        ]:
            try:
                r = await c.get(url, headers=hdrs_es)
                if not r.is_success: continue
                data = r.json()
                for ev in (data.get("events") or []):
                    name = f"{ev.get('name','')} {ev.get('shortName','')}".lower()
                    if not _is_ipl(name): continue
                    comp = (ev.get("competitions") or [{}])[0]
                    if (comp.get("status") or {}).get("type",{}).get("state") != "in": continue
                    cs = comp.get("competitors",[])
                    if len(cs) < 2: continue
                    def _ps(c):
                        s=str(c.get("score","0")).strip(); runs=0; wkts=0; overs=0
                        try:
                            if "(" in s: s,ov = s.split("("); overs=float(ov.rstrip(") ")) if ov else 0
                            if "/" in s: r,w=s.strip().split("/"); runs=int(r); wkts=int(w) if w.strip().isdigit() else 0
                            else: runs=int(s.strip() or "0")
                        except: pass
                        return {"runs":runs,"wkts":wkts,"overs":overs}
                    a,b = _ps(cs[0]),_ps(cs[1])
                    ta=(cs[0].get("team") or {}).get("displayName","A")
                    tb=(cs[1].get("team") or {}).get("displayName","B")
                    if a["overs"]>0 and not b["overs"]: runs,wkts,overs,batting,innings,target=a["runs"],a["wkts"],a["overs"],ta,1,0
                    elif b["overs"]>0: runs,wkts,overs,batting,innings,target=b["runs"],b["wkts"],b["overs"],tb,2,a["runs"]+1
                    else: continue
                    crr = round(runs/overs,2) if overs>0 else 0.0
                    rrr = 0.0
                    if innings==2 and target>0 and overs<20:
                        balls=max(1,(20-overs)*6); rrr=round((target-runs)/balls*6,2) if target>runs else 0.0
                    return {"team_a":ta,"team_b":tb,"batting":batting,"innings":innings,
                            "runs":runs,"wkts":wkts,"overs":overs,"crr":crr,"rrr":rrr,
                            "target":target,"match_id":str(ev.get("id","1")),"venue":""}
            except Exception as e:
                log.debug(f"ESPN: {e}")

    return None


# ══════════════════════════════════════════════════════════════════════════════
# Team matching — score team ↔ Polymarket outcome name
# ══════════════════════════════════════════════════════════════════════════════

TEAMS = {
    "mumbai indians":              ["mi","mumbai"],
    "chennai super kings":         ["csk","chennai"],
    "kolkata knight riders":       ["kkr","kolkata"],
    "royal challengers bengaluru": ["rcb","bangalore","bengaluru","royal challengers"],
    "rajasthan royals":            ["rr","rajasthan"],
    "delhi capitals":              ["dc","delhi"],
    "punjab kings":                ["pbks","punjab","kings xi"],
    "sunrisers hyderabad":         ["srh","hyderabad","sunrisers"],
    "gujarat titans":              ["gt","gujarat"],
    "lucknow super giants":        ["lsg","lucknow","super giants"],
}

def _keywords(name: str) -> list:
    n = name.lower()
    for full, aliases in TEAMS.items():
        if any(a in n for a in aliases) or n in full:
            return [full] + aliases
    return [n]


def find_market(markets: list, ta: str, tb: str) -> Optional[dict]:
    ka, kb = _keywords(ta), _keywords(tb)
    best, best_vol = None, -1
    for mkt in markets:
        q = mkt.get("question","").lower()
        if (any(k in q for k in ka) and any(k in q for k in kb)):
            if mkt.get("volume",0) > best_vol:
                best = mkt
                best_vol = mkt.get("volume",0)
    return best


def find_outcome(market: dict, team: str) -> Optional[dict]:
    """Find the YES token for 'team to win'."""
    kw = _keywords(team)
    for oc in (market.get("outcomes") or []):
        name = (oc.get("name") or "").lower()
        if any(k in name for k in kw):
            return oc
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Strategy — identical logic, adapted for Polymarket prices (0–1)
# ══════════════════════════════════════════════════════════════════════════════

def win_prob(score: dict, is_batting: bool) -> float:
    runs,wkts,overs,crr,rrr = score["runs"],score["wkts"],score["overs"],score["crr"],score["rrr"]
    innings,target = score["innings"],score["target"]
    if innings == 1:
        proj = runs + crr * max(0, 20-overs)
        p    = (proj - TOTAL_PAR) / 80 + 0.5 - wkts*0.04
    else:
        if target<=0 or rrr<=0: return 0.5
        rr_ratio = crr/rrr
        p = 0.3 + rr_ratio*0.35 - wkts*0.06
        if overs>=15: p -= 0.05
    p = max(0.05, min(0.95, p))
    return p if is_batting else 1-p


def decide(score: dict, market: Optional[dict], position: Optional[dict]) -> Optional[dict]:
    overs,runs,wkts,crr,rrr,innings = (
        score["overs"],score["runs"],score["wkts"],
        score["crr"],score["rrr"],score["innings"]
    )

    # ── Manage open position ──────────────────────────────────────────────────
    if position:
        entry_price = position["entry_price"]   # 0–1
        cur_price   = entry_price               # will be updated from market
        if market:
            oc = find_outcome(market, position["team"])
            if oc:
                cur_price = oc.get("price", entry_price)

        # TAKE PROFIT: our YES price rose (we're winning) — sell near top
        if cur_price >= entry_price + BOOKSET_PCT:
            return {"action": "SELL",
                    "token_id": position["token_id"],
                    "size_shares": position["size_shares"],
                    "price": round(cur_price - 0.01, 4),   # slightly below market
                    "reason": f"Take profit: {entry_price:.2f}→{cur_price:.2f}"}
        # STOP LOSS: our YES price dropped (we're losing)
        if cur_price <= entry_price - STOP_LOSS:
            if wkts >= 4 or (innings==2 and rrr>crr*1.6):
                return {"action": "SELL",
                        "token_id": position["token_id"],
                        "size_shares": position["size_shares"],
                        "price": round(cur_price - 0.02, 4),
                        "reason": f"Stop loss: {entry_price:.2f}→{cur_price:.2f}"}
        return None  # hold

    # ── Look for entry ────────────────────────────────────────────────────────
    if not market or overs > 16 or overs < 0.5:
        return None

    best_action, best_conf = None, 0.0
    kw_batting = _keywords(score["batting"])

    for oc in (market.get("outcomes") or []):
        price = oc.get("price", 0.5)
        if not (0.05 <= price <= 0.95): continue  # skip extreme prices

        name      = (oc.get("name") or "").lower()
        is_bat    = any(k in name for k in kw_batting)
        p         = win_prob(score, is_bat)
        # Edge: our model probability vs market-implied
        market_p  = price
        edge      = p - market_p       # positive = we think this outcome is underpriced

        if edge < 0.05 or p < 0.55: continue

        # Convert to confidence score
        conf = min(0.95, 0.55 + edge*1.2)
        if overs<=6 and wkts<=1 and crr>=8.5: conf += 0.05
        if price<0.3 and wkts<=3 and innings==1 and overs<=12: conf += 0.08
        conf = min(0.95, conf)

        if conf > best_conf and conf >= MIN_CONFIDENCE:
            best_conf = conf
            stake = min(MAX_STAKE_USDC, 2.0*conf)   # scale with confidence
            implied_odds = round(1/price, 2) if price>0 else 0
            best_action = {
                "action":      "BUY",
                "token_id":    oc["token_id"],
                "team":        oc.get("name","?"),
                "price":       price,
                "implied_odds": implied_odds,
                "confidence":  round(conf, 3),
                "stake_usdc":  round(stake, 4),
                "reason":      f"P_model={p:.0%} P_mkt={market_p:.0%} edge={edge:.0%} conf={conf:.0%} @ {overs:.1f}ov {runs}/{wkts}",
            }

    return best_action


# ══════════════════════════════════════════════════════════════════════════════
# One-time setup helpers
# ══════════════════════════════════════════════════════════════════════════════

def setup_credentials():
    """
    Generate Polymarket API credentials from private key.
    Run once: python polymarket_bot.py --setup
    """
    client = _get_clob_client()
    try:
        # derive deterministic API key from wallet
        creds = client.create_or_derive_api_creds()
        print("\n=== Polymarket API Credentials ===")
        print(f"POLY_API_KEY={creds.api_key}")
        print(f"POLY_API_SECRET={creds.api_secret}")
        print(f"POLY_API_PASSPHRASE={creds.api_passphrase}")
        print("\nAdd these to your .env file or Railway env vars.\n")
    except Exception as e:
        print(f"Error: {e}")


def approve_contracts():
    """
    Approve Polymarket CTF + USDC contracts on Polygon.
    Run once before trading: python polymarket_bot.py --approve
    """
    client = _get_clob_client()
    try:
        resp = client.approve_all_allowances()
        print(f"Approvals: {resp}")
    except Exception as e:
        print(f"Error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Main loop
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    global _brave_page

    log.info("="*60)
    log.info("Polymarket IPL Bot — direct API, no browser")
    log.info(f"Max stake: {MAX_STAKE_USDC} USDC | Min conf: {MIN_CONFIDENCE:.0%}")
    log.info("="*60)

    # India mode: use Brave to bypass ISP DNS block on polymarket.com
    if USE_BRAVE:
        try:
            from playwright.async_api import async_playwright
            pw = await async_playwright().__aenter__()
            browser = await pw.chromium.connect_over_cdp(CDP_URL, timeout=4000)
            ctx = browser.contexts[0]
            _brave_page = next((p for p in ctx.pages if p.url), ctx.pages[0] if ctx.pages else None)
            if _brave_page:
                log.info(f"Brave CDP connected — routing Polymarket calls through browser")
        except Exception as e:
            log.warning(f"Brave CDP failed ({e}) — will try direct httpx anyway")

    # Validate credentials
    if not PRIVATE_KEY:
        log.error("POLY_PRIVATE_KEY not set. Create a .env file with your Polygon wallet private key.")
        log.error("Run 'python polymarket_bot.py --setup' after setting POLY_PRIVATE_KEY to get API creds.")
        return

    # Show wallet balance
    try:
        bal = await asyncio.to_thread(get_wallet_balance_sync)
        log.info(f"Wallet USDC balance: {bal:.4f}")
    except Exception as e:
        log.warning(f"Could not fetch balance: {e}")
        bal = 0.0

    position   = None
    last_match = None
    markets    = []
    cycle      = 0

    while True:
        cycle += 1
        try:
            # ── Refresh markets every 10 cycles ──────────────────────────────
            if cycle % 10 == 1 or not markets:
                markets = await get_ipl_markets()
                if markets:
                    log.info(f"IPL markets on Polymarket: {len(markets)}")
                    for mkt in markets[:5]:
                        log.info(f"  [{mkt.get('volume',0):.0f}$] {mkt.get('question','')[:80]}")
                        for oc in mkt.get("outcomes",[]):
                            p = oc.get("price",0)
                            log.info(f"    {oc['name']}: price={p:.3f} (≈{1/p:.2f}x odds)")
                else:
                    if cycle % 8 == 1:
                        log.info("No active IPL markets on Polymarket — waiting for match day...")
                    await asyncio.sleep(LOOP_SECS)
                    continue

            # ── Refresh prices ────────────────────────────────────────────────
            markets = await refresh_prices(markets)

            # ── Live score ────────────────────────────────────────────────────
            score = await get_live_score()
            if not score:
                if cycle % 8 == 1:
                    log.info("No live IPL score — pre-match or between innings")
                await asyncio.sleep(LOOP_SECS)
                continue

            mid = score["match_id"]
            if mid != last_match:
                log.info(f"MATCH: {score['team_a']} vs {score['team_b']}")
                last_match = mid
                position   = None

            log.info(
                f"[{score['overs']:.1f}ov] {score['runs']}/{score['wkts']} "
                f"CRR:{score['crr']:.1f} RRR:{score['rrr']:.1f} Inn:{score['innings']} "
                f"Batting:{score['batting']}"
            )

            # ── Find matching market ──────────────────────────────────────────
            market = find_market(markets, score["team_a"], score["team_b"])
            if not market and cycle % 5 == 1:
                log.info("No Polymarket market matched for this match — will retry")

            # ── Strategy ─────────────────────────────────────────────────────
            action = decide(score, market, position)
            if action is None:
                await asyncio.sleep(LOOP_SECS)
                continue

            # ── Execute ───────────────────────────────────────────────────────
            if action["action"] == "BUY":
                log.info(
                    f"ENTRY: BUY YES({action['team']}) price={action['price']:.3f} "
                    f"(≈{action['implied_odds']}x) stake={action['stake_usdc']:.2f} USDC | {action['reason']}"
                )
                try:
                    resp = await asyncio.to_thread(
                        place_order_sync,
                        action["token_id"],
                        action["price"],
                        action["stake_usdc"],
                        "BUY",
                    )
                    order_id = resp.get("orderID") or resp.get("id","")
                    if order_id or resp.get("success"):
                        size_shares = round(action["stake_usdc"] / action["price"], 2)
                        position = {
                            "token_id":    action["token_id"],
                            "team":        action["team"],
                            "entry_price": action["price"],
                            "size_shares": size_shares,
                            "stake_usdc":  action["stake_usdc"],
                            "placed_at":   datetime.now().isoformat(),
                            "order_id":    order_id,
                        }
                        log.info(f"ORDER PLACED id={order_id} | {size_shares} shares @ {action['price']:.3f}")
                        bal = await asyncio.to_thread(get_wallet_balance_sync)
                        log.info(f"Balance: {bal:.4f} USDC")
                    else:
                        log.warning(f"Order not confirmed: {resp}")
                except Exception as e:
                    log.error(f"place_order: {e}")

            elif action["action"] == "SELL":
                log.info(f"EXIT ({action['reason']})")
                try:
                    resp = await asyncio.to_thread(
                        place_order_sync,
                        action["token_id"],
                        action["price"],
                        action["size_shares"] * action["price"],   # USDC value
                        "SELL",
                    )
                    order_id = resp.get("orderID") or resp.get("id","")
                    log.info(f"SELL order placed id={order_id}")
                    position = None
                    bal = await asyncio.to_thread(get_wallet_balance_sync)
                    log.info(f"Balance after exit: {bal:.4f} USDC")
                except Exception as e:
                    log.error(f"sell_order: {e}")
                    position = None  # clear anyway

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Cycle error: {e}", exc_info=False)

        await asyncio.sleep(LOOP_SECS)

    log.info("Bot stopped.")


if __name__ == "__main__":
    import sys
    if "--setup" in sys.argv:
        setup_credentials()
    elif "--approve" in sys.argv:
        approve_contracts()
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            log.info("Stopped by user.")
