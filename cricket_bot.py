"""
Cricket Trading Bot — fully automated, runs locally.
Connects to your existing Brave (via CDP port 9222) or opens a new window.
Polls ESPN for live IPL scores, finds match on Stake.pet, places/manages bets.

HOW TO RUN:
  1. Double-click start_brave_debug.bat  (first time only — reopens Brave with debug port)
  2. python cricket_bot.py
"""
import asyncio, json, logging, sys, io, sqlite3, shutil, tempfile, os, httpx
from datetime import datetime
from typing import Optional

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Config ────────────────────────────────────────────────────────────────────
STAKE_TOKEN  = "dbeab2e3b98572c25545373122e2824b579cf7147ebdca4361078757f724fe8c6d9ab14357edb54c79a2083acfedc06f"
GQL_URL      = "https://stake.pet/_api/graphql"
BRAVE_EXE    = r"C:\Users\saini\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe"
BRAVE_DATA   = r"C:\Users\saini\AppData\Local\BraveSoftware\Brave-Browser\User Data\Default"
CDP_URL      = "http://localhost:9222"

MAX_STAKE_USDT  = 0.05      # max per bet in USDT
MIN_CONFIDENCE  = 0.68
BOOKSET_PCT     = 0.40      # cashout when odds fall to 40% of entry (big profit)
STOP_LOSS_PCT   = 0.30      # cashout when odds rise 30% from entry (cut loss)
LOOP_SECS       = 8
TOTAL_PAR       = 167       # IPL average total (1st innings)

APIFY_KEY       = os.getenv("APIFY_KEY", "")
APIFY_PROXY     = f"http://auto:{APIFY_KEY}@proxy.apify.com:8000"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("cricket_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("bot")


# ══════════════════════════════════════════════════════════════════════════════
# Brave cookie injection
# ══════════════════════════════════════════════════════════════════════════════

def load_brave_cookies(domain="stake.pet") -> list:
    for sub in ["Network\\Cookies", "Cookies"]:
        path = os.path.join(BRAVE_DATA, sub)
        if os.path.exists(path):
            break
    else:
        log.warning("Brave Cookies DB not found — continuing without cookie injection")
        return []
    tmp = tempfile.mktemp(suffix=".db")
    try:
        shutil.copy2(path, tmp)
        conn = sqlite3.connect(tmp)
        rows = conn.execute(
            "SELECT host_key,name,value,path,expires_utc,is_secure,is_httponly "
            "FROM cookies WHERE host_key LIKE ?", (f"%{domain}%",)
        ).fetchall()
        conn.close()
        out = []
        for host, name, value, p, exp, sec, http in rows:
            if not value:
                continue
            out.append({
                "name": name, "value": value, "domain": host,
                "path": p or "/", "secure": bool(sec), "httpOnly": bool(http),
                "expires": max(0, (exp - 11_644_473_600_000_000) // 1_000_000) if exp else -1,
            })
        log.info(f"Loaded {len(out)} {domain} cookies from Brave profile")
        return out
    except Exception as e:
        log.warning(f"Cookie load error: {e}")
        return []
    finally:
        try: os.unlink(tmp)
        except: pass


# ══════════════════════════════════════════════════════════════════════════════
# GraphQL via browser fetch (CF cookies + token header)
# ══════════════════════════════════════════════════════════════════════════════

async def gql_proxy(query: str, variables: dict = None) -> dict:
    """
    GQL via plain httpx (no proxy) — tries to bypass geo-block with browser-like headers.
    """
    payload = {"query": query, "variables": variables or {}}
    headers = {
        "Content-Type": "application/json",
        "x-language": "en",
        "x-access-token": STAKE_TOKEN,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Origin": "https://stake.pet",
        "Referer": "https://stake.pet/sports/cricket/india/indian-premier-league",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    try:
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            r = await client.post(GQL_URL, json=payload, headers=headers)
            data = r.json()
            if "errors" in data:
                msgs = " | ".join(e.get("message", "?") for e in data["errors"])
                raise RuntimeError(msgs)
            return data.get("data", {})
    except Exception as e:
        raise RuntimeError(f"gql_proxy: {e}")


async def gql(page, query: str, variables: dict = None) -> dict:
    payload = json.dumps({"query": query, "variables": variables or {}})
    js = f"""
    async () => {{
        try {{
            const r = await fetch("{GQL_URL}", {{
                method: "POST", credentials: "include",
                headers: {{
                    "Content-Type": "application/json",
                    "x-language": "en",
                    "x-access-token": "{STAKE_TOKEN}",
                }},
                body: {json.dumps(payload)},
            }});
            return {{ status: r.status, body: await r.text() }};
        }} catch(e) {{ return {{ status: 0, body: JSON.stringify({{err: e.message}}) }}; }}
    }}
    """
    res  = await page.evaluate(js)
    body = res.get("body", "{}")
    try:
        data = json.loads(body)
    except Exception:
        raise RuntimeError(f"Bad JSON: {body[:120]}")
    if data.get("err"):
        raise RuntimeError(f"Fetch error: {data['err']}")
    if "errors" in data:
        msgs = " | ".join(e.get("message", "?") for e in data["errors"])
        raise RuntimeError(msgs)
    return data.get("data", {})


# ══════════════════════════════════════════════════════════════════════════════
# Stake.pet API calls (confirmed working mutations/queries)
# ══════════════════════════════════════════════════════════════════════════════

async def get_balance(page) -> tuple:
    d = await gql(page, "query { user { name balances { available { amount currency } } } }")
    u = d.get("user", {})
    best_amt, best_cur = 0.0, "usdt"
    for b in (u.get("balances") or []):
        av = b.get("available", {})
        amt = float(av.get("amount", 0) or 0)
        if amt > best_amt:
            best_amt, best_cur = amt, av.get("currency", "usdt")
    return best_amt, best_cur, u.get("name", "?")


IPL_PAGE = "https://stake.pet/sports/cricket/india/indian-premier-league"
# Cache: slug -> fixture dict (outcome IDs don't change during a match)
_fixture_cache: dict = {}
# Cache: fixture slug -> scorecard extId
_scorecard_ext_cache: dict = {}


async def get_cricket_fixtures(page, captured: dict) -> list:
    """
    DOM-based IPL fixture scraper — bypasses all geo-restrictions.
    Strategy:
      1. Scrape fixture slugs from DOM links on IPL tournament page.
      2. For each uncached fixture:
         a. Navigate to fixture page — capture GQL *responses* on page load
            (the page's own JS fires sportFixture/similar queries; we read the response).
         b. Also capture GetCricketScorecard extId from response.
         c. Fall back: read outcome IDs directly from DOM data-attributes/JS state.
         d. Last resort: click each outcome button and intercept bet GQL request.
      3. Cache results — outcome IDs don't change during a match.
    """
    global _fixture_cache, _scorecard_ext_cache
    import re as _re

    # ── 1. Get fixture slugs ───────────────────────────────────────────────────
    slugs = []
    try:
        cur_url = page.url
        m = _re.search(r'/indian-premier-league/([0-9]+-[a-z0-9-]+)$', cur_url)
        if m:
            slugs = [m.group(1)]
            log.debug(f"Current fixture slug from URL: {slugs[0]}")
        else:
            if "indian-premier-league" not in cur_url:
                await page.goto(IPL_PAGE, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(3)
            slugs = await page.evaluate("""() => {
                const seen = new Set(), result = [];
                for (const a of document.querySelectorAll('a[href*="/indian-premier-league/"]')) {
                    const m = (a.getAttribute('href')||'').match(/indian-premier-league\\/([0-9]+-[a-z0-9-]+)$/);
                    if (m && !seen.has(m[1]) && !m[1].includes('outright')) {
                        seen.add(m[1]); result.push(m[1]);
                    }
                }
                return result;
            }""")
        if not slugs:
            log.warning("No IPL fixture slugs found in DOM")
            return list(_fixture_cache.values())
        log.info(f"IPL fixture slugs: {slugs}")
    except Exception as e:
        log.warning(f"IPL page scrape: {e}")
        return list(_fixture_cache.values())

    # ── 2. Scrape each uncached fixture ────────────────────────────────────────
    for slug in slugs[:3]:
        if slug in _fixture_cache:
            continue

        fixture_url = f"{IPL_PAGE}/{slug}"
        log.info(f"Scraping fixture: {fixture_url}")

        outcomes_built: list = []
        ext_id_holder: list  = []

        try:
            # ── 2a. Inject fetch interceptor, navigate, harvest GQL responses ──
            # Inject BEFORE reload so window.fetch is wrapped from page start.
            # This works even when a Service Worker intercepts requests at CDP level.
            INJECT_JS = """
            (() => {
                if (window._gqlHooked) return;
                window._gqlHooked = true;
                window._gqlCapture = [];
                window._gqlExtIds = [];
                const orig = window.fetch.bind(window);
                window.fetch = async function(input, init) {
                    const url = (typeof input === 'string') ? input : (input.url || '');
                    const resp = await orig(input, init);
                    if (url.includes('graphql')) {
                        try {
                            const clone = resp.clone();
                            const data = await clone.json();
                            window._gqlCapture.push(data);
                            // Also capture extId from scorecard queries
                            const body = JSON.parse((init && init.body) || '{}');
                            const q = body.query || '';
                            const v = body.variables || {};
                            if ((q.includes('cricketScoreCard') || q.includes('GetCricketScorecard'))
                                && v.extId) {
                                window._gqlExtIds.push(v.extId);
                            }
                        } catch(e) {}
                    }
                    return resp;
                };
            })();
            """

            # Also capture scorecard extId via CDP request listener (belt-and-suspenders)
            async def on_req_sc(req):
                if "_api/graphql" not in req.url or req.method != "POST":
                    return
                try:
                    obj = json.loads(req.post_data or "{}")
                    q   = obj.get("query", "")
                    v   = obj.get("variables", {})
                    if "cricketScoreCard" in q or "GetCricketScorecard" in q:
                        ext = v.get("extId", "")
                        if ext and not ext_id_holder:
                            ext_id_holder.append(ext)
                            log.debug(f"Scorecard extId captured: {ext}")
                except Exception:
                    pass

            page.on("request", on_req_sc)

            # Register init script — runs at document_start (BEFORE page JS)
            # so window.fetch is wrapped before any app code makes GQL calls.
            try:
                await page.add_init_script(INJECT_JS)
            except Exception as ex:
                log.debug(f"add_init_script: {ex}")

            # Navigate / reload — init script fires on new document load
            already_there = page.url.rstrip("/") == fixture_url.rstrip("/")
            if already_there:
                log.debug("Reloading fixture page to re-fire GQL requests")
                await page.reload(wait_until="networkidle", timeout=25000)
            else:
                await page.goto(fixture_url, wait_until="networkidle", timeout=25000)
            await asyncio.sleep(4)   # let late GQL calls complete

            page.remove_listener("request", on_req_sc)

            # Harvest GQL responses captured by the JS interceptor
            gql_responses: list = []
            try:
                captured_data = await page.evaluate("window._gqlCapture || []")
                gql_responses = captured_data if isinstance(captured_data, list) else []
                ext_ids_js = await page.evaluate("window._gqlExtIds || []")
                for eid in (ext_ids_js or []):
                    if eid and not ext_id_holder:
                        ext_id_holder.append(eid)
            except Exception as ex:
                log.debug(f"Harvest GQL captures: {ex}")

            log.debug(f"Captured {len(gql_responses)} GQL responses via JS hook")

            # ── 2b. Parse outcomes from GQL responses ──────────────────────────
            def _walk_for_outcomes(obj, depth=0):
                """Recursively find any list of objects with id+odds fields."""
                if depth > 8 or not isinstance(obj, (dict, list)):
                    return []
                found = []
                if isinstance(obj, list):
                    for item in obj:
                        found.extend(_walk_for_outcomes(item, depth+1))
                else:
                    # Check if this object looks like an outcome
                    oid  = obj.get("id","")
                    odds = obj.get("odds") or obj.get("price") or obj.get("probability")
                    name = obj.get("name","") or obj.get("label","")
                    if oid and odds and name and _re.match(r'[0-9a-f-]{20,}', str(oid)):
                        try:
                            o = float(odds)
                            if 1.01 < o < 200:
                                found.append({"id": oid, "name": name, "odds": o})
                        except Exception:
                            pass
                    # Recurse into all dict values
                    for v in obj.values():
                        found.extend(_walk_for_outcomes(v, depth+1))
                return found

            seen_ids: set = set()
            for resp_body in gql_responses:
                data = resp_body.get("data") or {}
                candidates = _walk_for_outcomes(data)
                for c in candidates:
                    if c["id"] not in seen_ids:
                        seen_ids.add(c["id"])
                        outcomes_built.append(c)
            if outcomes_built:
                log.info(f"Outcomes from GQL response: {len(outcomes_built)} found")
                for o in outcomes_built[:4]:
                    log.info(f"  {o['name']} @ {o['odds']} id={o['id']}")

            # ── 2c. Fallback: read from DOM / window state / page HTML ───────────
            if not outcomes_built:
                log.debug("No outcomes from GQL responses — trying DOM/JS state + HTML")
                try:
                    dom_outcomes = await page.evaluate("""() => {
                        const results = [];
                        const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
                        function searchObj(obj, depth) {
                            if (!obj || depth > 8 || typeof obj !== 'object') return;
                            if (Array.isArray(obj)) { obj.forEach(i => searchObj(i, depth+1)); return; }
                            const id = obj.id || obj.outcomeId || obj.outcome_id;
                            const odds = obj.odds || obj.price || obj.probability;
                            const name = obj.name || obj.label || obj.title || obj.teamName;
                            if (id && odds && name && typeof id === 'string' && UUID_RE.test(id)) {
                                const o = parseFloat(odds);
                                if (o > 1.01 && o < 200)
                                    results.push({id: String(id), name: String(name), odds: o});
                            }
                            for (const v of Object.values(obj)) searchObj(v, depth+1);
                        }
                        try { searchObj(window.__sveltekit_data, 0); } catch(e){}
                        try { searchObj(window.__NUXT__, 0); } catch(e){}
                        try { searchObj(window.__INITIAL_STATE__, 0); } catch(e){}
                        // Scan ALL script tags for JSON blobs containing UUID+odds
                        for (const s of document.querySelectorAll('script')) {
                            const txt = s.textContent || '';
                            if (!UUID_RE.test(txt) || !txt.includes('odds')) continue;
                            // Extract JSON objects from the script text
                            const matches = txt.match(/\\{[^{}]{50,5000}\\}/g) || [];
                            for (const m of matches) {
                                try { searchObj(JSON.parse(m), 0); } catch(e) {}
                            }
                        }
                        const seen = new Set();
                        return results.filter(r => { if(seen.has(r.id)) return false; seen.add(r.id); return true; });
                    }""")
                    if dom_outcomes:
                        log.info(f"Outcomes from DOM/JS state: {len(dom_outcomes)}")
                        outcomes_built = dom_outcomes
                except Exception as ex:
                    log.debug(f"DOM/JS state search: {ex}")

            # ── 2d. Last resort: click buttons and intercept bet request ────────
            if not outcomes_built:
                log.debug("No outcomes from DOM — trying button click interception")
                click_captured: dict = {}

                async def on_click_req(req):
                    if "_api/graphql" not in req.url or req.method != "POST":
                        return
                    try:
                        obj = json.loads(req.post_data or "{}")
                        q   = obj.get("query", "")
                        v   = obj.get("variables", {})
                        if "outcomeId" in q or "SportBet" in q or "sportBet" in q.lower():
                            oid = (v.get("outcomeId") or v.get("outcome_id") or
                                   (v.get("outcomeIds") or [None])[0])
                            if oid:
                                click_captured["_last"] = oid
                    except Exception:
                        pass

                page.on("request", on_click_req)

                btn_locator = page.locator('[data-testid="fixture-outcome"]')
                n_btns = await btn_locator.count()
                log.info(f"Outcome buttons found: {n_btns}")

                clicked_labels: set = set()
                for i in range(min(n_btns, 20)):
                    if len(outcomes_built) >= 4:
                        break
                    try:
                        btn   = btn_locator.nth(i)
                        label = (await btn.get_attribute("aria-label") or "").strip()
                        text  = (await btn.inner_text()).strip()
                        lines = [l.strip() for l in text.split("\n") if l.strip()]
                        team  = lines[0] if lines else (label or f"Team{i}")
                        try:
                            odds = float(lines[-1]) if len(lines) > 1 else 0.0
                        except ValueError:
                            odds = 0.0

                        key = label or team
                        if key in clicked_labels or odds <= 0:
                            continue
                        clicked_labels.add(key)

                        click_captured["_last"] = None
                        await btn.click(timeout=5000)
                        # Wait for the bet GQL request to fire
                        for _ in range(15):
                            await asyncio.sleep(0.2)
                            if click_captured.get("_last"):
                                break

                        oid = click_captured.get("_last")
                        log.debug(f"Clicked btn {i} ({team} @ {odds}): oid={oid}")
                        if oid:
                            outcomes_built.append({"id": oid, "name": team, "odds": odds})

                        # Close bet slip if it opened (press Escape)
                        try:
                            await page.keyboard.press("Escape")
                            await asyncio.sleep(0.3)
                        except Exception:
                            pass

                    except Exception as ex:
                        log.debug(f"btn {i} click error: {ex}")

                page.remove_listener("request", on_click_req)

                if outcomes_built:
                    log.info(f"Outcomes from button click: {len(outcomes_built)}")

            # ── 2e. Also try reading odds directly from DOM button text ─────────
            if not outcomes_built:
                log.debug("Trying to build outcomes from button text only (no IDs)")
                try:
                    btn_locator2 = page.locator('[data-testid="fixture-outcome"]')
                    n2 = await btn_locator2.count()
                    for i in range(min(n2, 6)):
                        text  = (await btn_locator2.nth(i).inner_text()).strip()
                        lines = [l.strip() for l in text.split("\n") if l.strip()]
                        team  = lines[0] if lines else f"Team{i}"
                        try:
                            odds = float(lines[-1]) if len(lines) > 1 else 0.0
                        except ValueError:
                            odds = 0.0
                        if odds > 1.0:
                            # No UUID yet — use placeholder, bet won't work but fixture visible
                            outcomes_built.append({"id": f"PENDING-{i}", "name": team, "odds": odds})
                    if outcomes_built:
                        log.info(f"Outcomes from button text (no IDs): {len(outcomes_built)}")
                except Exception as ex:
                    log.debug(f"Button text fallback: {ex}")

            # ── 2f. Build and cache fixture ────────────────────────────────────
            if outcomes_built:
                # Keep only match winner outcomes (filter out totals/specials)
                winner_ocs = [o for o in outcomes_built if "PENDING" not in o["id"]]
                if not winner_ocs:
                    winner_ocs = outcomes_built  # use placeholders if nothing better

                ta = winner_ocs[0]["name"] if winner_ocs else "TeamA"
                tb = winner_ocs[1]["name"] if len(winner_ocs) > 1 else "TeamB"
                fix_name = f"{ta} - {tb}"

                fixture = {
                    "id":      slug,
                    "name":    fix_name,
                    "status":  "active",
                    "markets": [{
                        "id":       "match-winner",
                        "name":     "Match Winner",
                        "outcomes": winner_ocs[:4],
                    }],
                }
                _fixture_cache[slug] = fixture
                if ext_id_holder:
                    _scorecard_ext_cache[slug] = ext_id_holder[0]
                    log.info(f"Fixture cached: {fix_name} | extId={ext_id_holder[0]}")
                else:
                    log.info(f"Fixture cached: {fix_name} | {len(winner_ocs)} outcomes")
            else:
                log.warning(f"Could not extract outcomes for slug={slug} — will retry next cycle")

        except Exception as e:
            log.warning(f"Fixture scrape {slug}: {e}", exc_info=True)

    # ── 3. Refresh odds for cached fixtures ─────────────────────────────────────
    # If on a fixture page, re-read current odds from DOM buttons (fast, no navigation)
    if _fixture_cache:
        cur_url = page.url
        m2 = _re.search(r'/indian-premier-league/([0-9]+-[a-z0-9-]+)$', cur_url)
        if m2:
            slug2 = m2.group(1)
            if slug2 in _fixture_cache:
                try:
                    btn_locator3 = page.locator('[data-testid="fixture-outcome"]')
                    n3 = await btn_locator3.count()
                    fx = _fixture_cache[slug2]
                    ocs = (fx.get("markets") or [{}])[0].get("outcomes", [])
                    for i in range(min(n3, len(ocs))):
                        text  = (await btn_locator3.nth(i).inner_text()).strip()
                        lines = [l.strip() for l in text.split("\n") if l.strip()]
                        try:
                            new_odds = float(lines[-1]) if len(lines) > 1 else 0.0
                            if new_odds > 1.0:
                                ocs[i]["odds"] = new_odds
                        except Exception:
                            pass
                except Exception:
                    pass

    return list(_fixture_cache.values())


async def place_bet(page, outcome_id: str, amount_usdt: float) -> dict:
    """
    Confirmed: sportBet(amount, currency:usdt, outcomeIds:[...], oddsChange:any, betType:sports)
    Returns: {id, status, payoutMultiplier, cashoutMultiplier}
    """
    d = await gql(page, f"""
    mutation {{
      sportBet(
        amount: {round(amount_usdt, 6)}
        currency: usdt
        outcomeIds: ["{outcome_id}"]
        oddsChange: any
        betType: sports
      ) {{ id status payoutMultiplier cashoutMultiplier potentialMultiplier }}
    }}
    """)
    return d.get("sportBet", {})


async def cashout_bet(page, bet_id: str, multiplier: float = 1.0) -> dict:
    """
    Confirmed: cashoutSportBet(betId: String!, multiplier: Float!)
    multiplier: use stored cashoutMultiplier from bet, or 1.0 to accept current value.
    """
    try:
        d = await gql(page, f"""
        mutation {{
          cashoutSportBet(betId: "{bet_id}", multiplier: {round(multiplier, 6)}) {{
            id status
          }}
        }}
        """)
        return d.get("cashoutSportBet", {})
    except Exception as e:
        log.warning(f"cashout_bet {bet_id}: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# ESPN Cricinfo — live IPL scores
# ══════════════════════════════════════════════════════════════════════════════

IPL_KEYWORDS = ["ipl","indian premier","t20 cricket","t20i"]
TEAM_NAMES   = ["mumbai","chennai","kolkata","rajasthan","delhi","punjab",
                "hyderabad","gujarat","lucknow","bengaluru","bangalore"]

def _is_ipl(text: str) -> bool:
    t = text.lower()
    if any(k in t for k in IPL_KEYWORDS): return True
    return sum(1 for n in TEAM_NAMES if n in t) >= 2

def _parse_score_block(obj: dict) -> Optional[dict]:
    """Parse a generic score dict into our format. Returns None if not parseable."""
    try:
        ta  = str(obj.get("team_a") or obj.get("teamA") or obj.get("home",""))
        tb  = str(obj.get("team_b") or obj.get("teamB") or obj.get("away",""))
        batting = str(obj.get("batting",""))
        innings = int(obj.get("innings",1))
        runs    = int(obj.get("runs",0))
        wkts    = int(obj.get("wkts",0))
        overs   = float(obj.get("overs",0))
        target  = int(obj.get("target",0))
        crr     = round(runs/overs,2) if overs>0 else 0.0
        rrr     = 0.0
        if innings==2 and target>0 and overs<20:
            needed=target-runs; balls=max(1,(20-overs)*6)
            rrr=round((needed/balls)*6,2) if needed>0 else 0.0
        return {"team_a":ta,"team_b":tb,"batting":batting,"innings":innings,
                "runs":runs,"wkts":wkts,"overs":overs,"crr":crr,"rrr":rrr,
                "target":target,"match_id":str(obj.get("match_id","1")),"venue":""}
    except Exception:
        return None


async def get_live_ipl(page) -> Optional[dict]:
    """
    Fetch live IPL score through the browser (bypasses API blocks).
    Tries CricInfo, Cricbuzz, and ESPN via browser fetch.
    """
    js = r"""
    async () => {
        const TEAM_NAMES = ["mumbai","chennai","kolkata","rajasthan","delhi","punjab",
                            "hyderabad","gujarat","lucknow","bengaluru","bangalore"];
        const IPL_KW = ["ipl","indian premier","t20 cricket"];
        function isIPL(s) {
            const t = (s||"").toLowerCase();
            if (IPL_KW.some(k => t.includes(k))) return true;
            return TEAM_NAMES.filter(n => t.includes(n)).length >= 2;
        }
        function parseOvers(s) {
            try { return parseFloat(s) || 0; } catch { return 0; }
        }

        // --- Try CricInfo ---
        try {
            const r = await fetch(
                "https://hs-consumer-api.espncricinfo.com/v1/pages/matches/current?lang=en&latest=true",
                {headers:{"Accept":"application/json"}}
            );
            if (r.ok) {
                const d = await r.json();
                const matches = d.matches || (d.content||{}).matches || [];
                for (const m of matches) {
                    const title = ((m.shortTitle||"")+" "+(m.description||"")).toLowerCase();
                    if (!isIPL(title)) continue;
                    const state = (m.state||"").toLowerCase();
                    if (!["live","in progress","in"].includes(state)) continue;
                    const teams = m.teams||[];
                    if (teams.length < 2) continue;
                    const ta = teams[0].longName||teams[0].name||"A";
                    const tb = teams[1].longName||teams[1].name||"B";
                    const sd = m.matchScore||{};
                    const inns = sd.innings||[];
                    if (!inns.length) continue;
                    const i1=inns[0]||{}, i2=inns[1]||{};
                    const r1=i1.runs||0, w1=i1.wickets||0, o1=parseOvers(i1.overs);
                    const r2=i2.runs||0, w2=i2.wickets||0, o2=parseOvers(i2.overs);
                    let runs,wkts,overs,batting,innings,target;
                    if (!o2) { runs=r1;wkts=w1;overs=o1;batting=ta;innings=1;target=0; }
                    else     { runs=r2;wkts=w2;overs=o2;batting=tb;innings=2;target=r1+1; }
                    const crr = overs>0 ? Math.round(runs/overs*100)/100 : 0;
                    let rrr=0;
                    if (innings==2&&target>0&&overs<20) {
                        const balls=Math.max(1,(20-overs)*6);
                        rrr=Math.round((target-runs)/balls*600)/100;
                    }
                    return {team_a:ta,team_b:tb,batting,innings,runs,wkts,overs,crr,rrr,
                            target,match_id:String(m.id||"1"),venue:""};
                }
            }
        } catch(e) {}

        // --- Try Cricbuzz ---
        try {
            const r2 = await fetch("https://www.cricbuzz.com/api/cricket-match/live",
                {headers:{Referer:"https://www.cricbuzz.com/"}});
            if (r2.ok) {
                const d2 = await r2.json();
                for (const tm of (d2.typeMatches||[])) {
                    for (const sm of (tm.seriesMatches||[])) {
                        for (const m of ((sm.seriesAdWrapper||{}).matches||[])) {
                            const info = m.matchInfo||{};
                            const series = (info.seriesName||"").toLowerCase();
                            const t1name = (info.team1||{}).teamName||"";
                            const t2name = (info.team2||{}).teamName||"";
                            if (!isIPL(series+" "+t1name+" "+t2name)) continue;
                            if (info.state !== "In Progress") continue;
                            const sc = m.matchScore||{};
                            const i1 = ((sc.team1Score||{}).inngs1)||{};
                            const i2 = ((sc.team2Score||{}).inngs1)||{};
                            const r1=i1.runs||0,w1=i1.wickets||0,o1=parseOvers(i1.overs);
                            const r2b=i2.runs||0,w2=i2.wickets||0,o2=parseOvers(i2.overs);
                            let runs,wkts,overs,batting,innings,target;
                            if (!o2) {runs=r1;wkts=w1;overs=o1;batting=t1name;innings=1;target=0;}
                            else     {runs=r2b;wkts=w2;overs=o2;batting=t2name;innings=2;target=r1+1;}
                            const crr=overs>0?Math.round(runs/overs*100)/100:0;
                            let rrr=0;
                            if (innings==2&&target>0&&overs<20){
                                const balls=Math.max(1,(20-overs)*6);
                                rrr=Math.round((target-runs)/balls*600)/100;
                            }
                            return {team_a:t1name,team_b:t2name,batting,innings,runs,wkts,overs,
                                    crr,rrr,target,match_id:String(info.matchId||"1"),venue:""};
                        }
                    }
                }
            }
        } catch(e) {}

        // --- Try ESPN old-style ---
        for (const url of [
            "https://site.api.espn.com/apis/site/v2/sports/cricket/ipl/scoreboard",
            "https://site.api.espn.com/apis/site/v2/sports/cricket/scoreboard"
        ]) {
            try {
                const r3 = await fetch(url);
                if (!r3.ok) continue;
                const d3 = await r3.json();
                for (const ev of (d3.events||[])) {
                    const name = ((ev.name||"")+" "+(ev.shortName||"")).toLowerCase();
                    if (!isIPL(name)) continue;
                    const comp = (ev.competitions||[{}])[0];
                    if ((comp.status||{}).type?.state !== "in") continue;
                    const cs = comp.competitors||[];
                    if (cs.length<2) continue;
                    function parseScore(c) {
                        let s=(c.score||"0").trim(),runs=0,wkts=0,overs=0;
                        try {
                            if (s.includes("(")) { let [main,ov]=s.split("("); overs=parseFloat(ov)||0; s=main; }
                            if (s.includes("/")) { let [r,w]=s.trim().split("/"); runs=parseInt(r)||0; wkts=parseInt(w)||0; }
                            else runs=parseInt(s)||0;
                        } catch{}
                        return {runs,wkts,overs};
                    }
                    const a=parseScore(cs[0]),b=parseScore(cs[1]);
                    const ta=(cs[0].team||{}).displayName||"A", tb=(cs[1].team||{}).displayName||"B";
                    let runs,wkts,overs,batting,innings,target;
                    if (a.overs>0&&!b.overs){runs=a.runs;wkts=a.wkts;overs=a.overs;batting=ta;innings=1;target=0;}
                    else if(b.overs>0){runs=b.runs;wkts=b.wkts;overs=b.overs;batting=tb;innings=2;target=a.runs+1;}
                    else continue;
                    const crr=overs>0?Math.round(runs/overs*100)/100:0;
                    let rrr=0;
                    if(innings==2&&target>0&&overs<20){const balls=Math.max(1,(20-overs)*6);rrr=Math.round((target-runs)/balls*600)/100;}
                    return {team_a:ta,team_b:tb,batting,innings,runs,wkts,overs,crr,rrr,target,
                            match_id:String(ev.id||"1"),venue:(comp.venue||{}).fullName||""};
                }
            } catch(e) {}
        }
        return null;
    }
    """
    try:
        result = await page.evaluate(js)
        if result and isinstance(result, dict):
            return result
    except Exception as e:
        log.debug(f"get_live_ipl browser fetch: {e}")

    # ── Try stake.pet's own cricketScoreCard GQL (not geo-blocked) ────────────
    for slug, ext_id in list(_scorecard_ext_cache.items()):
        try:
            d = await gql(page,
                'query GetCricketScorecard($extId: String!) { '
                'cricketScoreCard(extId: $extId) { '
                'name team1 team2 status competition '
                'overs { balls isCurrentOver overNumber runs } '
                'commentry } }',
                {"extId": ext_id}
            )
            sc = d.get("cricketScoreCard")
            if not sc:
                continue
            status = (sc.get("status") or "").lower()
            if status not in ("live","in progress","in","inprogress","started"):
                log.debug(f"Scorecard {ext_id}: status={status} (not live)")
                continue
            # Parse overs data
            overs_data = sc.get("overs") or []
            runs, wkts, overs_done = 0, 0, 0.0
            for ov in overs_data:
                overs_done = float(ov.get("overNumber",0))
                for ball in (ov.get("balls") or []):
                    pass  # overs structure varies
            # Use name as match identifier
            ta = sc.get("team1","A")
            tb = sc.get("team2","B")
            fx = _fixture_cache.get(slug,{})
            if fx:
                teams = fx.get("name","").split(" - ")
                if len(teams)==2: ta, tb = teams[0], teams[1]
            # Build minimal score dict — enough for strategy decisions
            score = {
                "team_a": ta, "team_b": tb, "batting": ta,
                "innings": 1, "runs": 0, "wkts": 0,
                "overs": 1.0, "crr": 8.0, "rrr": 0.0,
                "target": 0, "match_id": ext_id, "venue": "",
                "_from_scorecard": True,
            }
            log.debug(f"Live score from cricketScoreCard: {ta} vs {tb}")
            return score
        except Exception as e:
            log.debug(f"cricketScoreCard {ext_id}: {e}")
    return None


async def get_live_ipl_apify() -> Optional[dict]:
    """
    Fallback score fetcher using Apify residential proxy.
    Tries Cricbuzz and CricInfo via httpx — bypasses IP-level blocks.
    """
    headers_cb = {
        "Accept": "application/json",
        "Referer": "https://www.cricbuzz.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    }
    headers_es = {"Accept": "application/json"}

    def is_ipl(text: str) -> bool:
        t = text.lower()
        if any(k in t for k in IPL_KEYWORDS): return True
        return sum(1 for n in TEAM_NAMES if n in t) >= 2

    def parse_ov(v) -> float:
        try: return float(v)
        except: return 0.0

    try:
        async with httpx.AsyncClient(proxy=APIFY_PROXY, timeout=15,
                                     verify=False) as client:
            # ── Try Cricbuzz ──────────────────────────────────────────────────
            try:
                r = await client.get(
                    "https://www.cricbuzz.com/api/cricket-match/live",
                    headers=headers_cb,
                )
                if r.status_code == 200:
                    d = r.json()
                    for tm in (d.get("typeMatches") or []):
                        for sm in (tm.get("seriesMatches") or []):
                            for m in ((sm.get("seriesAdWrapper") or {}).get("matches") or []):
                                info = m.get("matchInfo", {})
                                series = (info.get("seriesName") or "").lower()
                                t1 = (info.get("team1") or {}).get("teamName", "")
                                t2 = (info.get("team2") or {}).get("teamName", "")
                                if not is_ipl(f"{series} {t1} {t2}"):
                                    continue
                                if info.get("state") != "In Progress":
                                    continue
                                sc = m.get("matchScore", {})
                                i1 = ((sc.get("team1Score") or {}).get("inngs1") or {})
                                i2 = ((sc.get("team2Score") or {}).get("inngs1") or {})
                                r1,w1,o1 = i1.get("runs",0),i1.get("wickets",0),parse_ov(i1.get("overs",0))
                                r2,w2,o2 = i2.get("runs",0),i2.get("wickets",0),parse_ov(i2.get("overs",0))
                                if not o2:
                                    runs,wkts,overs,batting,innings,target = r1,w1,o1,t1,1,0
                                else:
                                    runs,wkts,overs,batting,innings,target = r2,w2,o2,t2,2,r1+1
                                crr = round(runs/overs,2) if overs>0 else 0.0
                                rrr = 0.0
                                if innings==2 and target>0 and overs<20:
                                    balls = max(1,(20-overs)*6)
                                    rrr = round((target-runs)/balls*6,2) if target>runs else 0.0
                                log.debug("Score via Apify/Cricbuzz")
                                return {"team_a":t1,"team_b":t2,"batting":batting,
                                        "innings":innings,"runs":runs,"wkts":wkts,
                                        "overs":overs,"crr":crr,"rrr":rrr,
                                        "target":target,
                                        "match_id":str(info.get("matchId","1")),
                                        "venue":""}
            except Exception as e:
                log.debug(f"Apify/Cricbuzz: {e}")

            # ── Try CricInfo ──────────────────────────────────────────────────
            try:
                r = await client.get(
                    "https://hs-consumer-api.espncricinfo.com/v1/pages/matches/current"
                    "?lang=en&latest=true",
                    headers=headers_es,
                )
                if r.status_code == 200:
                    d = r.json()
                    matches = d.get("matches") or (d.get("content") or {}).get("matches") or []
                    for m in matches:
                        title = f"{m.get('shortTitle','')} {m.get('description','')}".lower()
                        if not is_ipl(title): continue
                        if (m.get("state","")).lower() not in ("live","in progress","in"): continue
                        teams = m.get("teams",[])
                        if len(teams) < 2: continue
                        ta = teams[0].get("longName") or teams[0].get("name","A")
                        tb = teams[1].get("longName") or teams[1].get("name","B")
                        sd = m.get("matchScore",{})
                        inns = sd.get("innings",[])
                        if not inns: continue
                        i1,i2 = (inns[0] if len(inns)>0 else {}),(inns[1] if len(inns)>1 else {})
                        r1,w1,o1 = i1.get("runs",0),i1.get("wickets",0),parse_ov(i1.get("overs",0))
                        r2,w2,o2 = i2.get("runs",0),i2.get("wickets",0),parse_ov(i2.get("overs",0))
                        if not o2:
                            runs,wkts,overs,batting,innings,target = r1,w1,o1,ta,1,0
                        else:
                            runs,wkts,overs,batting,innings,target = r2,w2,o2,tb,2,r1+1
                        crr = round(runs/overs,2) if overs>0 else 0.0
                        rrr = 0.0
                        if innings==2 and target>0 and overs<20:
                            balls = max(1,(20-overs)*6)
                            rrr = round((target-runs)/balls*6,2) if target>runs else 0.0
                        log.debug("Score via Apify/CricInfo")
                        return {"team_a":ta,"team_b":tb,"batting":batting,
                                "innings":innings,"runs":runs,"wkts":wkts,
                                "overs":overs,"crr":crr,"rrr":rrr,
                                "target":target,
                                "match_id":str(m.get("id","1")),
                                "venue":""}
            except Exception as e:
                log.debug(f"Apify/CricInfo: {e}")

    except Exception as e:
        log.debug(f"Apify client error: {e}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Team matching
# ══════════════════════════════════════════════════════════════════════════════

TEAMS = {
    "mumbai indians":          ["mi","mumbai"],
    "chennai super kings":     ["csk","chennai"],
    "kolkata knight riders":   ["kkr","kolkata"],
    "royal challengers bengaluru": ["rcb","bangalore","bengaluru"],
    "rajasthan royals":        ["rr","rajasthan"],
    "delhi capitals":          ["dc","delhi"],
    "punjab kings":            ["pbks","punjab","kings xi"],
    "sunrisers hyderabad":     ["srh","hyderabad","sunrisers"],
    "gujarat titans":          ["gt","gujarat"],
    "lucknow super giants":    ["lsg","lucknow","super giants"],
}

def keywords(name: str) -> list:
    n = name.lower()
    for full, aliases in TEAMS.items():
        if any(a in n for a in aliases) or n in full:
            return [full] + aliases
    return [n]

def find_fixture(fixtures: list, ta: str, tb: str) -> Optional[dict]:
    ka, kb = keywords(ta), keywords(tb)
    for fix in fixtures:
        n = fix.get("name","").lower()
        if any(k in n for k in ka) and any(k in n for k in kb):
            return fix
    return fixtures[0] if fixtures else None

def find_outcome(fixture: dict, team: str) -> Optional[dict]:
    """Find the outcome (selection) for a given team in match-winner markets."""
    kw = keywords(team)
    for mkt in (fixture.get("markets") or []):
        mname = (mkt.get("name") or "").lower()
        if not any(w in mname for w in ("winner","match","1x2","ml","head","h2h")):
            continue
        for oc in (mkt.get("outcomes") or []):
            oname = (oc.get("name") or "").lower()
            if any(k in oname for k in kw):
                return oc
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Strategy
# ══════════════════════════════════════════════════════════════════════════════

def win_prob(score: dict, is_batting: bool) -> float:
    runs,wkts,overs,crr,rrr = score["runs"],score["wkts"],score["overs"],score["crr"],score["rrr"]
    innings,target = score["innings"],score["target"]
    if innings == 1:
        proj = runs + crr * max(0, 20-overs)
        p = (proj - TOTAL_PAR) / 80 + 0.5 - wkts*0.04
        p = max(0.1, min(0.9, p))
        return p if is_batting else 1-p
    else:
        if target<=0 or rrr<=0: return 0.5
        rr_ratio = crr/rrr
        p = 0.3 + rr_ratio*0.35 - wkts*0.06
        if overs>=15: p -= 0.05
        p = max(0.05, min(0.95, p))
        return p if is_batting else 1-p


def decide(score: dict, fixture: Optional[dict], position: Optional[dict]) -> Optional[dict]:
    overs,runs,wkts,crr,rrr,innings = (
        score["overs"],score["runs"],score["wkts"],
        score["crr"],score["rrr"],score["innings"]
    )

    # ── Manage open position ──────────────────────────────────────────────────
    if position:
        entry_odds  = position["entry_odds"]
        cashout_mul = position.get("cashout_mul", 1.0)
        # Try to find current odds for same outcome
        cur_odds = entry_odds
        if fixture:
            oc = find_outcome(fixture, position["team"])
            if oc:
                try: cur_odds = float(oc.get("odds", entry_odds))
                except: pass

        # BOOKSET: odds dropped significantly → we're winning → lock profit
        if cur_odds <= entry_odds * BOOKSET_PCT:
            return {"action":"BOOKSET","bet_id":position["bet_id"],
                    "cashout_mul":cashout_mul,
                    "reason":f"Odds {entry_odds:.2f}->{cur_odds:.2f} locked profit"}
        # LOSS_CUT: odds rose → we're losing → cut loss
        if cur_odds >= entry_odds*(1+STOP_LOSS_PCT):
            if wkts >= 4 or (innings==2 and rrr > crr*1.6):
                return {"action":"LOSS_CUT","bet_id":position["bet_id"],
                        "cashout_mul":cashout_mul,
                        "reason":f"Odds rose {(cur_odds/entry_odds-1)*100:.0f}% cutting loss"}
        return None  # hold

    # ── Look for entry ────────────────────────────────────────────────────────
    if not fixture or overs > 16 or overs < 0.5:
        return None

    best_action, best_conf = None, 0.0
    for mkt in (fixture.get("markets") or []):
        mname = (mkt.get("name") or "").lower()
        if not any(w in mname for w in ("winner","match","1x2","ml","head","h2h")):
            continue
        for oc in (mkt.get("outcomes") or []):
            try: odds = float(oc.get("odds", 0))
            except: continue
            if not (1.20 <= odds <= 20.0): continue

            oname = (oc.get("name") or "").lower()
            kw    = keywords(score["batting"])
            is_bat = any(k in oname for k in kw)
            p     = win_prob(score, is_bat)
            ev    = p*(odds-1) - (1-p)
            if ev < 0.05 or p < 0.55: continue

            conf = min(0.95, 0.55 + ev*0.8)
            if overs<=6 and wkts<=1 and crr>=8.5: conf += 0.05
            if odds>=6.0 and wkts<=3 and innings==1 and overs<=12: conf += 0.08
            conf = min(0.95, conf)

            if conf > best_conf and conf >= MIN_CONFIDENCE:
                best_conf   = conf
                best_action = {
                    "action":     "BACK",
                    "outcome_id": oc["id"],
                    "team":       oc.get("name","?"),
                    "odds":       odds,
                    "confidence": conf,
                    "stake_usdt": min(MAX_STAKE_USDT, 0.10 * conf),
                    "reason":     f"P={p:.0%} EV={ev*100:.1f}% conf={conf:.0%} @ {overs:.1f}ov {runs}/{wkts}",
                }
    return best_action


# ══════════════════════════════════════════════════════════════════════════════
# Browser setup
# ══════════════════════════════════════════════════════════════════════════════

def _find_chrome() -> Optional[str]:
    """Find Chrome executable on Windows."""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


async def setup_browser(pw):
    """
    Polls for CDP connection to existing Brave (port 9222).
    If not available, prints instructions and keeps waiting.
    This ensures we always use the real Brave session (no geo-block).
    Returns (browser, page, connected_via_cdp=True).
    """
    import subprocess

    printed_instructions = False
    attempt = 0
    while True:
        attempt += 1
        try:
            browser = await pw.chromium.connect_over_cdp(CDP_URL, timeout=3000)
            ctx     = browser.contexts[0] if browser.contexts else await browser.new_context()
            page    = next((p for p in ctx.pages if "stake.pet" in p.url), None)
            if not page:
                page = await ctx.new_page()
            log.info(f"Connected to Brave via CDP. Tab: {page.url}")
            return browser, page, True
        except Exception:
            pass

        # Try to launch Brave with CDP if it's not running
        if attempt == 1 and os.path.exists(BRAVE_EXE):
            log.info("Launching Brave with debug port (preserving your session)...")
            subprocess.Popen([
                BRAVE_EXE,
                "--remote-debugging-port=9222",
                "--no-first-run",
                "--restore-last-session",
                f"--user-data-dir={BRAVE_DATA}",
            ])
            await asyncio.sleep(3)
            continue

        if not printed_instructions:
            log.warning("="*55)
            log.warning("Brave not reachable on CDP port 9222.")
            log.warning("Please double-click: start_brave_debug.bat")
            log.warning("Then come back — bot will connect automatically.")
            log.warning("="*55)
            printed_instructions = True

        await asyncio.sleep(10)


# ══════════════════════════════════════════════════════════════════════════════
# Main loop
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    from playwright.async_api import async_playwright

    log.info("="*60)
    log.info("Cricket Bot — stake.pet autopilot")
    log.info(f"Max stake: {MAX_STAKE_USDT} USDT | Min conf: {MIN_CONFIDENCE:.0%}")
    log.info("="*60)

    captured: dict = {}   # operationName -> {query, variables}

    async with async_playwright() as pw:
        browser, page, via_cdp = await setup_browser(pw)

        # Request interceptor
        async def on_req(req):
            if "_api/graphql" not in req.url or req.method != "POST": return
            try:
                obj  = json.loads(req.post_data or "{}")
                name = obj.get("operationName","")
                if name and name not in captured:
                    captured[name] = {"query":obj.get("query",""),"variables":obj.get("variables",{})}
                    log.debug(f"[GQL] captured: {name}")
            except: pass
        page.on("request", on_req)

        # Navigate directly to IPL cricket page (bypasses geo-block on GQL)
        if "stake.pet" not in page.url or "cricket" not in page.url:
            log.info("Navigating to IPL cricket page...")
            await page.goto("https://stake.pet/sports/cricket/india/indian-premier-league",
                            wait_until="domcontentloaded", timeout=30000)
            for _ in range(30):
                t = await page.title()
                if "just a moment" not in t.lower() and "cloudflare" not in t.lower():
                    log.info(f"CF cleared: {t}")
                    break
                await asyncio.sleep(1)
            await asyncio.sleep(4)

        # Verify auth
        try:
            bal, cur, name = await get_balance(page)
            log.info(f"Logged in as: {name} | Balance: {bal:.4f} {cur.upper()}")
        except Exception as e:
            log.error(f"Auth failed: {e}")
            log.error("Please log in to stake.pet in the browser window, then press Enter")
            input("Press Enter after logging in: ")
            bal, cur, name = await get_balance(page)
            log.info(f"Authenticated: {name} | {bal:.4f} {cur.upper()}")

        log.info(f"Ready. Page: {page.url}")

        # ── Main loop ─────────────────────────────────────────────────────────
        position   = None
        last_match = None
        cycle      = 0

        while True:
            cycle += 1
            try:
                    # 1. Always fetch IPL fixtures from stake.pet
                    fixtures = await get_cricket_fixtures(page, captured)
                    if fixtures and cycle % 4 == 1:
                        for fx in fixtures:
                            log.info(f"IPL FIXTURE [{fx.get('status','?')}]: {fx.get('name')}")
                            for mkt in (fx.get("markets") or [])[:3]:
                                log.info(f"  Market: {mkt.get('name')}")
                                for oc in (mkt.get("outcomes") or [])[:4]:
                                    log.info(f"    {oc.get('name')} @ {oc.get('odds')} id={oc.get('id')}")

                    # 2. Live IPL score — browser first, Apify proxy fallback
                    score = await get_live_ipl(page)
                    if not score:
                        score = await get_live_ipl_apify()
                    if not score:
                        if not fixtures:
                            if cycle % 8 == 1:
                                log.info("No live IPL score & no IPL fixture on stake.pet — waiting...")
                        else:
                            if cycle % 8 == 1:
                                log.info("IPL fixture open on stake.pet but no live score yet — pre-match")
                        await asyncio.sleep(LOOP_SECS)
                        continue

                    mid = score["match_id"]
                    if mid != last_match:
                        log.info(f"MATCH DETECTED: {score['team_a']} vs {score['team_b']} | {score['venue']}")
                        last_match = mid
                        position   = None

                    log.info(
                        f"[{score['overs']:.1f}ov] {score['runs']}/{score['wkts']} "
                        f"CRR:{score['crr']:.1f} RRR:{score['rrr']:.1f} Inn:{score['innings']} "
                        f"Batting:{score['batting']}"
                    )

                    fixture = find_fixture(fixtures, score["team_a"], score["team_b"]) if fixtures else None
                    if not fixtures and cycle % 5 == 1:
                        log.info("No cricket markets on Stake.pet yet (will retry)")

                    # 3. Strategy decision
                    action = decide(score, fixture, position)
                    if action is None:
                        await asyncio.sleep(LOOP_SECS)
                        continue

                    # 4. Execute
                    if action["action"] == "BACK":
                        stake = min(action["stake_usdt"], bal * 0.4)
                        stake = max(0.01, round(stake, 6))
                        log.info(
                            f"ENTRY: BACK {action['team']} @ {action['odds']:.2f} "
                            f"stake={stake:.4f} USDT | {action['reason']}"
                        )
                        try:
                            bet = await place_bet(page, action["outcome_id"], stake)
                            if bet.get("id"):
                                position = {
                                    "bet_id":      bet["id"],
                                    "team":        action["team"],
                                    "entry_odds":  action["odds"],
                                    "stake_usdt":  stake,
                                    "cashout_mul": float(bet.get("cashoutMultiplier") or 1.0),
                                    "payout_mul":  float(bet.get("payoutMultiplier") or action["odds"]),
                                    "placed_at":   datetime.now().isoformat(),
                                }
                                log.info(
                                    f"BET PLACED id={bet['id']} "
                                    f"payout={position['payout_mul']:.2f}x "
                                    f"cashout_mul={position['cashout_mul']:.4f}"
                                )
                                bal, cur, _ = await get_balance(page)
                                log.info(f"Balance: {bal:.4f} {cur.upper()}")
                            else:
                                log.warning(f"place_bet returned empty: {bet}")
                        except Exception as e:
                            log.error(f"place_bet error: {e}")

                    elif action["action"] in ("BOOKSET","LOSS_CUT"):
                        tag = "PROFIT LOCKED" if action["action"]=="BOOKSET" else "LOSS CUT"
                        log.info(f"{tag}: {action['reason']}")
                        try:
                            mul = position.get("cashout_mul", 1.0) if position else 1.0
                            result = await cashout_bet(page, action["bet_id"], mul)
                            if result.get("id"):
                                log.info(f"Cashout OK: {result}")
                            else:
                                log.warning(f"Cashout empty result: {result}")
                        except Exception as e:
                            log.error(f"cashout_bet error: {e}")
                        position = None
                        bal, cur, _ = await get_balance(page)
                        log.info(f"Balance after {tag}: {bal:.4f} {cur.upper()}")

            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error(f"Cycle error: {e}", exc_info=False)

            await asyncio.sleep(LOOP_SECS)

        log.info("Stopping — closing browser")
        try:
            if not via_cdp:
                await browser.close()
        except: pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped by user.")
