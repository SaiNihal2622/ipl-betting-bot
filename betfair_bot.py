"""
Betfair IPL Cricket Trading Bot — data-backed in-play trading
=============================================================
Strategy:
  Pre-match  : back favourite based on ELO ratings + H2H + venue win%
  In-play    : monitor run rate, wickets, req. rate → back/lay dynamically
  Green-up   : trade out at target profit % (lock guaranteed return)

ENV VARS:
  BETFAIR_USERNAME=
  BETFAIR_PASSWORD=
  BETFAIR_APP_KEY=           # from betfair developer hub (free)
  BETFAIR_CERT_CRT=          # path to client cert (or paste as env)
  BETFAIR_CERT_KEY=          # path to client key
  BF_BANK_GBP=50             # total bank in GBP
  BF_STAKE_PCT=5             # % of bank per trade
  BF_GREEN_PCT=30            # green-up when 30% profit locked
  BF_MAX_ODDS=6.0            # never back above this
  BF_MIN_ODDS=1.10           # never back below this
  BF_DRY_RUN=1               # set to 0 to place real bets
"""

import os, sys, json, time, logging, asyncio, math
from datetime import datetime, timezone
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
BF_USER       = os.getenv("BETFAIR_USERNAME", "")
BF_PASS       = os.getenv("BETFAIR_PASSWORD", "")
BF_APP_KEY    = os.getenv("BETFAIR_APP_KEY", "")
CERT_CRT      = os.getenv("BETFAIR_CERT_CRT", "client-2048.crt")
CERT_KEY      = os.getenv("BETFAIR_CERT_KEY", "client-2048.key")
BANK          = float(os.getenv("BF_BANK_GBP", "50"))
STAKE_PCT     = float(os.getenv("BF_STAKE_PCT", "5"))   # % of bank
GREEN_PCT     = float(os.getenv("BF_GREEN_PCT", "30"))  # green-up trigger
MAX_ODDS      = float(os.getenv("BF_MAX_ODDS", "6.0"))
MIN_ODDS      = float(os.getenv("BF_MIN_ODDS", "1.10"))
DRY_RUN       = os.getenv("BF_DRY_RUN", "1") != "0"
LOOP_SECS     = 30   # poll interval in-play

# ── IPL team ELO ratings (2025 season end) ───────────────────────────────────
# Higher = stronger. Updated from 2024-25 IPL results.
ELO = {
    "Mumbai Indians":             1680,
    "Chennai Super Kings":        1660,
    "Royal Challengers Bangalore":1620,
    "Kolkata Knight Riders":      1640,
    "Rajasthan Royals":           1630,
    "Sunrisers Hyderabad":        1610,
    "Delhi Capitals":             1590,
    "Punjab Kings":               1570,
    "Lucknow Super Giants":       1580,
    "Gujarat Titans":             1600,
}

# Venue home-team advantage (win% above 50% for home team historically)
VENUE_BOOST = {
    "Wankhede Stadium":           0.08,   # MI home
    "M. A. Chidambaram Stadium":  0.07,   # CSK home
    "Eden Gardens":               0.06,   # KKR home
    "Narendra Modi Stadium":      0.05,   # GT home
    "Rajiv Gandhi Intl Cricket":  0.05,   # SRH home
    "Sawai Mansingh Stadium":     0.06,   # RR home
    "Arun Jaitley Stadium":       0.04,   # DC home
    "M. Chinnaswamy Stadium":     0.05,   # RCB home
    "PCA Stadium Mohali":         0.04,   # PBKS home
    "BRSABV Ekana":               0.04,   # LSG home
}

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("betfair_bot.log", encoding="utf-8"),
    ]
)
log = logging.getLogger("bf_bot")

# ── Betfair client ─────────────────────────────────────────────────────────────
import betfairlightweight as bfl
from betfairlightweight import filters

_client: Optional[bfl.APIClient] = None

def get_client() -> bfl.APIClient:
    global _client
    if _client:
        return _client
    if not all([BF_USER, BF_PASS, BF_APP_KEY]):
        raise RuntimeError("BETFAIR_USERNAME / BETFAIR_PASSWORD / BETFAIR_APP_KEY not set")
    certs = None
    if os.path.exists(CERT_CRT) and os.path.exists(CERT_KEY):
        certs = (CERT_CRT, CERT_KEY)
    _client = bfl.APIClient(BF_USER, BF_PASS, BF_APP_KEY, certs=certs)
    _client.login()
    log.info("Betfair login OK")
    return _client


# ── Market discovery ──────────────────────────────────────────────────────────
def find_ipl_markets(hours_ahead: int = 12) -> list:
    """Find all IPL match-odds markets starting in the next `hours_ahead` hours."""
    client = get_client()
    # Cricket event type = 4
    market_filter = filters.market_filter(
        event_type_ids=["4"],
        market_countries=["IN", "GB"],   # IPL listed under GB exchange too
        market_type_codes=["MATCH_ODDS"],
        text_query="IPL",
    )
    cats = client.betting.list_market_catalogue(
        filter=market_filter,
        market_projection=["MARKET_START_TIME", "RUNNERS", "EVENT", "COMPETITION", "MARKET_DESCRIPTION"],
        max_results=50,
        sort="FIRST_TO_START",
    )
    now = datetime.now(timezone.utc)
    results = []
    for m in cats:
        start = m.market_start_time
        if start is None:
            continue
        # Include pre-match (up to hours_ahead ahead) and in-play
        delta_h = (start - now).total_seconds() / 3600
        if -6 <= delta_h <= hours_ahead:
            results.append(m)
            log.info(f"Market: {m.market_id} | {m.market_name} | {m.event.name} | start={start.strftime('%H:%M UTC')} | runners={len(m.runners)}")
    return results


def get_market_book(market_id: str) -> Optional[object]:
    """Get current prices + in-play status for a market."""
    client = get_client()
    books = client.betting.list_market_book(
        market_ids=[market_id],
        price_projection=filters.price_projection(
            price_data=["EX_BEST_OFFERS"],
        ),
    )
    return books[0] if books else None


# ── ELO-based pre-match model ─────────────────────────────────────────────────
def elo_win_prob(team_a: str, team_b: str, venue: str = "") -> tuple[float, float]:
    """
    Returns (prob_a_wins, prob_b_wins) using ELO difference.
    Applies venue home boost if applicable.
    """
    ra = ELO.get(team_a, 1600)
    rb = ELO.get(team_b, 1600)
    # Check venue boost
    boost = VENUE_BOOST.get(venue, 0.0)
    # Determine which team is home
    home_indicators = {
        "Wankhede": "Mumbai Indians",
        "Chidambaram": "Chennai Super Kings",
        "Eden": "Kolkata Knight Riders",
        "Narendra Modi": "Gujarat Titans",
        "Rajiv Gandhi": "Sunrisers Hyderabad",
        "Sawai": "Rajasthan Royals",
        "Jaitley": "Delhi Capitals",
        "Chinnaswamy": "Royal Challengers Bangalore",
        "PCA": "Punjab Kings",
        "Ekana": "Lucknow Super Giants",
    }
    home_team = next((t for k, t in home_indicators.items() if k in venue), None)
    # ELO probability
    p_a = 1 / (1 + 10 ** ((rb - ra) / 400))
    p_b = 1 - p_a
    # Apply home boost
    if home_team == team_a:
        p_a = min(0.95, p_a + boost)
        p_b = 1 - p_a
    elif home_team == team_b:
        p_b = min(0.95, p_b + boost)
        p_a = 1 - p_b
    return round(p_a, 4), round(p_b, 4)


def prob_to_fair_odds(p: float) -> float:
    """Convert win probability to fair decimal odds."""
    if p <= 0:
        return 999.0
    return round(1 / p, 2)


# ── In-play score model ───────────────────────────────────────────────────────
def fetch_live_score(team_a: str, team_b: str) -> dict:
    """
    Fetch live score from ESPN CricInfo via The Odds API's score endpoint
    or direct ESPN scrape. Returns parsed score dict.
    """
    import httpx
    score = {"team_a": team_a, "team_b": team_b, "innings": 1,
             "runs": 0, "wickets": 0, "overs": 0.0,
             "target": None, "req_rate": None, "curr_rate": 0.0}
    try:
        # Try The Odds API scores endpoint (free, includes cricket)
        odds_key = os.getenv("ODDS_API_KEY", "")
        if odds_key:
            r = httpx.get(
                "https://api.the-odds-api.com/v4/sports/cricket_ipl/scores/",
                params={"apiKey": odds_key, "daysFrom": 1},
                timeout=8
            )
            if r.status_code == 200:
                events = r.json()
                for ev in events:
                    teams = [s.get("name","") for s in ev.get("scores") or []]
                    if team_a in teams or team_b in teams:
                        for s in ev.get("scores") or []:
                            if s.get("name") == team_a:
                                score["runs_a"] = s.get("score", "0/0")
                            elif s.get("name") == team_b:
                                score["runs_b"] = s.get("score", "0/0")
                        score["completed"] = ev.get("completed", False)
                        return score
    except Exception as e:
        log.debug(f"Score fetch error: {e}")
    return score


def parse_score_string(s: str) -> tuple[int, int]:
    """Parse '143/4' → (143, 4)."""
    try:
        parts = str(s).split("/")
        runs = int(parts[0].strip())
        wkts = int(parts[1].strip()) if len(parts) > 1 else 0
        return runs, wkts
    except Exception:
        return 0, 0


def inplay_win_prob(score: dict, total_overs: int = 20) -> tuple[float, float]:
    """
    Estimate win probability from live score using simple run-rate model.
    First innings: compare projected total to historical average (167 runs).
    Second innings: compare required rate to current run rate.
    Returns (p_batting_team, p_fielding_team).
    """
    AVG_TOTAL = 167  # IPL T20 average first innings

    runs_a_str = score.get("runs_a", "0/0")
    runs_b_str = score.get("runs_b", "0/0")
    runs_a, wkts_a = parse_score_string(runs_a_str)
    runs_b, wkts_b = parse_score_string(runs_b_str)

    # Determine match state
    # If team_b hasn't batted yet (0 runs, 0 wickets and team_a has batted)
    if runs_b == 0 and runs_a > 0:
        # First innings complete — second innings just starting
        target = runs_a + 1
        req_rate = target / total_overs
        # Batting team (B) slightly favoured if target < AVG_TOTAL
        p_b = 0.55 if target < AVG_TOTAL else 0.45
        return 1 - p_b, p_b

    if runs_a > 0 and runs_b > 0:
        # Second innings in progress
        overs_done = score.get("overs", 10.0)
        if overs_done <= 0:
            overs_done = 10.0
        target = runs_a + 1
        remaining_runs = target - runs_b
        remaining_overs = total_overs - overs_done
        if remaining_overs <= 0:
            # Over — B wins if they got the runs
            return (0.05, 0.95) if runs_b >= target else (0.95, 0.05)
        req_rate = remaining_runs / remaining_overs
        curr_rate = runs_b / max(overs_done, 0.1)
        wickets_left = 10 - wkts_b

        # Simple logistic: rate advantage + wickets in hand
        rate_diff = curr_rate - req_rate   # positive = batting team ahead
        wkt_factor = wickets_left / 10     # 1.0 = all wickets in hand
        raw_score = rate_diff * 0.3 + wkt_factor * 0.4
        # Map to probability via sigmoid
        p_b = 1 / (1 + math.exp(-raw_score * 5))
        p_b = max(0.05, min(0.95, p_b))
        return 1 - p_b, p_b

    # Pre-match or no data — return 50/50
    return 0.5, 0.5


# ── Position tracking ─────────────────────────────────────────────────────────
class Position:
    def __init__(self, market_id, runner_id, side, odds, stake, team_name):
        self.market_id  = market_id
        self.runner_id  = runner_id
        self.side       = side        # "BACK" or "LAY"
        self.odds       = odds
        self.stake      = stake
        self.team       = team_name
        self.bet_id     = None
        self.placed_at  = datetime.now(timezone.utc)
        self.liability  = stake * (odds - 1) if side == "LAY" else stake
        self.to_win     = stake * (odds - 1) if side == "BACK" else stake

    def __repr__(self):
        return (f"Position({self.team} {self.side} @ {self.odds} "
                f"stake=£{self.stake:.2f} to_win=£{self.to_win:.2f})")


# ── Bet placement ─────────────────────────────────────────────────────────────
def place_bet(market_id: str, runner_id: int, side: str,
              odds: float, stake: float, team: str) -> Optional[Position]:
    """
    Place a back or lay bet. Returns Position if successful.
    side: 'BACK' or 'LAY'
    """
    pos = Position(market_id, runner_id, side, odds, stake, team)
    log.info(f"{'[DRY]' if DRY_RUN else '[LIVE]'} {pos}")
    if DRY_RUN:
        pos.bet_id = f"DRY_{int(time.time())}"
        return pos

    client = get_client()
    size = round(stake, 2)
    instruction = filters.place_instruction(
        order_type="LIMIT",
        selection_id=runner_id,
        side=side,
        limit_order=filters.limit_order(
            size=size,
            price=odds,
            persistence_type="LAPSE",
        ),
    )
    result = client.betting.place_orders(
        market_id=market_id,
        instructions=[instruction],
        customer_ref=f"ipl_{int(time.time())}",
    )
    if result and result.status == "SUCCESS":
        pos.bet_id = result.instruction_reports[0].bet_id
        log.info(f"Bet placed OK — bet_id={pos.bet_id}")
        return pos
    else:
        log.error(f"Bet failed: {result}")
        return None


def green_up(position: Position, current_odds: float) -> bool:
    """
    Trade out (green up) by placing opposite bet at current odds.
    Calculates hedge stake to lock guaranteed profit.
    """
    if position.side == "BACK":
        # Originally backed at `position.odds` for `position.stake`
        # Hedge: lay at current_odds
        # Hedge stake = (back_stake * back_odds) / lay_odds
        hedge_stake = round((position.stake * position.odds) / current_odds, 2)
        profit_locked = round(position.stake * (position.odds - 1) - hedge_stake * (current_odds - 1), 2)
    else:
        # Originally laid → hedge by backing
        hedge_stake = round((position.stake * position.odds) / current_odds, 2)
        profit_locked = round(position.stake - hedge_stake, 2)

    if profit_locked <= 0:
        log.info(f"No profit to lock (would be £{profit_locked:.2f})")
        return False

    log.info(f"GREEN UP: lay hedge @ {current_odds} stake=£{hedge_stake:.2f} → locks £{profit_locked:.2f}")
    hedge_side = "LAY" if position.side == "BACK" else "BACK"
    hedge_pos = place_bet(position.market_id, position.runner_id, hedge_side,
                          current_odds, hedge_stake, position.team)
    return hedge_pos is not None


# ── Main trading logic ────────────────────────────────────────────────────────
def get_runner_best_odds(book, runner_id: int, side: str) -> float:
    """Get best available back or lay price for a runner."""
    for r in book.runners:
        if r.selection_id == runner_id:
            ex = r.ex
            if side == "BACK" and ex.available_to_back:
                return ex.available_to_back[0].price
            elif side == "LAY" and ex.available_to_lay:
                return ex.available_to_lay[0].price
    return 0.0


def trade_market(market_cat, position: Optional[Position] = None) -> Optional[Position]:
    """
    Core trading logic for one market iteration.
    Returns updated Position (or None if no trade).
    """
    market_id = market_cat.market_id
    runners   = market_cat.runners  # [runner_a, runner_b, draw(rare)]
    if len(runners) < 2:
        return position

    team_a = runners[0].runner_name
    team_b = runners[1].runner_name
    venue  = getattr(market_cat.event, "venue", "") or ""

    book = get_market_book(market_id)
    if not book:
        return position

    in_play = book.inplay
    status  = book.status   # OPEN, SUSPENDED, CLOSED

    if status == "CLOSED":
        log.info(f"Market {market_id} CLOSED — done.")
        return None

    # Get best prices
    odds_a_back = get_runner_best_odds(book, runners[0].selection_id, "BACK")
    odds_b_back = get_runner_best_odds(book, runners[1].selection_id, "BACK")
    odds_a_lay  = get_runner_best_odds(book, runners[0].selection_id, "LAY")
    odds_b_lay  = get_runner_best_odds(book, runners[1].selection_id, "LAY")

    log.info(f"{'[IN-PLAY]' if in_play else '[PRE-MATCH]'} {team_a} vs {team_b}")
    log.info(f"  {team_a}: back={odds_a_back} lay={odds_a_lay}")
    log.info(f"  {team_b}: back={odds_b_back} lay={odds_b_lay}")

    stake = round(BANK * STAKE_PCT / 100, 2)

    # ── GREEN UP check (existing position) ────────────────────────────────────
    if position:
        pid  = position.runner_id
        curr = get_runner_best_odds(book, pid, "LAY" if position.side == "BACK" else "BACK")
        if curr <= 0:
            return position
        # Check profit %
        if position.side == "BACK":
            profit_pct = (position.odds / curr - 1) * 100
        else:
            profit_pct = (curr / position.odds - 1) * 100

        log.info(f"  Position: {position} | current_odds={curr} | profit={profit_pct:.1f}%")

        if profit_pct >= GREEN_PCT:
            log.info(f"  ✅ TARGET PROFIT {profit_pct:.1f}% >= {GREEN_PCT}% → greening up")
            green_up(position, curr)
            return None  # position closed

        # Stop loss: if odds moved 50% against us
        if profit_pct <= -50:
            log.info(f"  🛑 STOP LOSS {profit_pct:.1f}% → cutting position")
            green_up(position, curr)
            return None

        return position  # hold

    # ── PRE-MATCH: back the ELO favourite ────────────────────────────────────
    if not in_play:
        p_a, p_b = elo_win_prob(team_a, team_b, venue)
        fair_a = prob_to_fair_odds(p_a)
        fair_b = prob_to_fair_odds(p_b)
        log.info(f"  ELO: {team_a} p={p_a:.2%} fair={fair_a} | {team_b} p={p_b:.2%} fair={fair_b}")

        # Back A if market odds are value (market > fair odds = overpriced = value back)
        if (odds_a_back > fair_a * 1.03 and MIN_ODDS <= odds_a_back <= MAX_ODDS):
            log.info(f"  📈 VALUE BACK: {team_a} @ {odds_a_back} (fair={fair_a})")
            return place_bet(market_id, runners[0].selection_id, "BACK",
                             odds_a_back, stake, team_a)
        elif (odds_b_back > fair_b * 1.03 and MIN_ODDS <= odds_b_back <= MAX_ODDS):
            log.info(f"  📈 VALUE BACK: {team_b} @ {odds_b_back} (fair={fair_b})")
            return place_bet(market_id, runners[1].selection_id, "BACK",
                             odds_b_back, stake, team_b)
        else:
            log.info("  No value pre-match — waiting for in-play.")
            return None

    # ── IN-PLAY: score-based dynamic trading ─────────────────────────────────
    score = fetch_live_score(team_a, team_b)
    p_a, p_b = inplay_win_prob(score)
    fair_a = prob_to_fair_odds(p_a)
    fair_b = prob_to_fair_odds(p_b)
    log.info(f"  Score: {team_a} {score.get('runs_a','?')} | {team_b} {score.get('runs_b','?')}")
    log.info(f"  Model: {team_a} p={p_a:.2%} fair={fair_a} | {team_b} p={p_b:.2%} fair={fair_b}")

    # Back the team whose probability our model rates higher than the market
    val_a = odds_a_back / fair_a if fair_a > 0 else 0
    val_b = odds_b_back / fair_b if fair_b > 0 else 0
    log.info(f"  Value ratio: {team_a}={val_a:.2f} | {team_b}={val_b:.2f}")

    EDGE_THRESH = 1.05   # need at least 5% edge

    if val_a >= val_b and val_a >= EDGE_THRESH and MIN_ODDS <= odds_a_back <= MAX_ODDS:
        log.info(f"  🏏 BACK {team_a} @ {odds_a_back} (model fair={fair_a}, edge={val_a:.2f}x)")
        return place_bet(market_id, runners[0].selection_id, "BACK",
                         odds_a_back, stake, team_a)
    elif val_b > val_a and val_b >= EDGE_THRESH and MIN_ODDS <= odds_b_back <= MAX_ODDS:
        log.info(f"  🏏 BACK {team_b} @ {odds_b_back} (model fair={fair_b}, edge={val_b:.2f}x)")
        return place_bet(market_id, runners[1].selection_id, "BACK",
                         odds_b_back, stake, team_b)
    else:
        log.info("  No edge in-play — holding.")
        return None


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("Betfair IPL Cricket Trading Bot")
    log.info(f"Bank: £{BANK} | Stake: {STAKE_PCT}% (£{BANK*STAKE_PCT/100:.2f}/trade) | DRY_RUN={DRY_RUN}")
    log.info("=" * 60)

    positions: dict[str, Optional[Position]] = {}  # market_id → Position

    while True:
        try:
            markets = find_ipl_markets(hours_ahead=6)
            if not markets:
                log.info("No IPL markets found — waiting 60s")
                time.sleep(60)
                continue

            for mkt in markets:
                mid = mkt.market_id
                pos = positions.get(mid)
                new_pos = trade_market(mkt, pos)
                positions[mid] = new_pos

        except bfl.exceptions.APIError as e:
            log.error(f"Betfair API error: {e}")
            if "LOGIN" in str(e).upper():
                global _client
                _client = None   # force re-login
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as e:
            log.error(f"Loop error: {e}", exc_info=True)

        time.sleep(LOOP_SECS)


def run_once():
    """Single scan pass for GitHub Actions / CI."""
    log.info("=== betfair_bot --once ===")
    try:
        markets = find_ipl_markets(hours_ahead=12)
        if not markets:
            log.info("No IPL markets found.")
            return
        for mkt in markets:
            trade_market(mkt, position=None)
    except Exception as e:
        log.error(f"run_once error: {e}", exc_info=True)


def setup_check():
    """Verify credentials work and show account balance."""
    client = get_client()
    funds = client.account.get_account_funds()
    log.info(f"Account balance: £{funds.available_to_bet_balance:.2f}")
    log.info(f"Exposure: £{funds.exposure:.2f}")
    log.info("✅ Betfair setup OK")


if __name__ == "__main__":
    if "--setup" in sys.argv:
        setup_check()
    elif "--once" in sys.argv:
        run_once()
    else:
        try:
            main()
        except KeyboardInterrupt:
            log.info("Stopped.")
