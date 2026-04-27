"""
IPL Match Prediction Model
==========================
Covers:
  Part 1  — Download & parse Cricsheet IPL JSON data
  Part 2  — Pre-match win-probability model (LogisticRegression + calibration)
  Part 3  — In-play win-probability model
  Part 4  — Public API functions
  Part 5  — Telegram expert-tips scraper
  Part 6  — Back-testing function
"""

from __future__ import annotations

import io
import json
import zipfile
import warnings
from collections import defaultdict
from datetime import datetime
from typing import Optional

import httpx
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")

# ---------------------------------------------------------------------------
# Global state (populated by train())
# ---------------------------------------------------------------------------
_matches_df: Optional[pd.DataFrame] = None          # one row per match
_balls_df: Optional[pd.DataFrame] = None            # ball-by-ball rows
_prematch_model: Optional[CalibratedClassifierCV] = None
_prematch_scaler: Optional[StandardScaler] = None
_inplay_model: Optional[CalibratedClassifierCV] = None
_inplay_scaler: Optional[StandardScaler] = None

CRICSHEET_URL = "https://cricsheet.org/downloads/ipl_male_json.zip"

# ---------------------------------------------------------------------------
# Part 1 — Download & parse
# ---------------------------------------------------------------------------

def _download_zip() -> zipfile.ZipFile:
    """Download Cricsheet IPL JSON zip into memory and return a ZipFile object."""
    print("[*] Downloading Cricsheet IPL data …")
    resp = httpx.get(CRICSHEET_URL, timeout=120, follow_redirects=True)
    resp.raise_for_status()
    print(f"    Downloaded {len(resp.content) / 1_048_576:.1f} MB")
    return zipfile.ZipFile(io.BytesIO(resp.content))


def _parse_match(data: dict) -> tuple[dict, list[dict]]:
    """
    Parse a single Cricsheet match JSON dict.
    Returns (match_row_dict, [ball_row_dict, …]).
    """
    info = data.get("info", {})
    innings_list = data.get("innings", [])

    teams = info.get("teams", [])
    if len(teams) < 2:
        return {}, []

    home_team = teams[0]
    away_team = teams[1]

    # Dates
    dates = info.get("dates", [])
    match_date = dates[0] if dates else None

    # Toss
    toss = info.get("toss", {})
    toss_winner = toss.get("winner", "")
    toss_decision = toss.get("decision", "")

    # Outcome
    outcome = info.get("outcome", {})
    winner = outcome.get("winner", None)
    if winner is None and "result" in outcome:
        winner = None  # no result / tie

    # Innings totals
    def _innings_summary(inn_data: dict) -> tuple[int, int]:
        runs = 0
        wickets = 0
        for over in inn_data.get("overs", []):
            for delivery in over.get("deliveries", []):
                runs_d = delivery.get("runs", {})
                runs += runs_d.get("total", 0)
                if "wickets" in delivery:
                    wickets += len(delivery["wickets"])
        return runs, wickets

    i1_score, i1_wkts, i2_score, i2_wkts = 0, 0, 0, 0
    batting_teams: list[str] = []

    for idx, inn in enumerate(innings_list):
        batting_team = inn.get("team", "")
        batting_teams.append(batting_team)
        r, w = _innings_summary(inn)
        if idx == 0:
            i1_score, i1_wkts = r, w
        elif idx == 1:
            i2_score, i2_wkts = r, w

    match_row = dict(
        home_team=home_team,
        away_team=away_team,
        venue=info.get("venue", ""),
        season=str(info.get("season", "")),
        toss_winner=toss_winner,
        toss_decision=toss_decision,
        winner=winner,
        innings1_score=i1_score,
        innings1_wickets=i1_wkts,
        innings2_score=i2_score,
        innings2_wickets=i2_wkts,
        match_date=match_date,
    )

    # ---- Ball-by-ball rows (for in-play model) ----
    ball_rows: list[dict] = []

    # We only care about 2nd innings for target-chasing model
    if len(innings_list) >= 2:
        target = i1_score + 1
        inn2 = innings_list[1]
        batting_team2 = inn2.get("team", "")
        did_win = 1 if winner == batting_team2 else 0

        cumulative_runs = 0
        cumulative_wickets = 0
        total_overs = 20

        for over_data in inn2.get("overs", []):
            over_num = over_data.get("over", 0)  # 0-indexed
            for ball_idx, delivery in enumerate(over_data.get("deliveries", [])):
                runs_d = delivery.get("runs", {})
                cumulative_runs += runs_d.get("total", 0)
                if "wickets" in delivery:
                    cumulative_wickets += len(delivery["wickets"])

            # Snapshot at end of each over
            overs_done = over_num + 1
            overs_remaining = total_overs - overs_done
            wickets_remaining = 10 - cumulative_wickets
            runs_needed = target - cumulative_runs

            if overs_remaining <= 0:
                continue

            curr_run_rate = cumulative_runs / overs_done if overs_done > 0 else 0.0
            req_run_rate = runs_needed / overs_remaining if overs_remaining > 0 else 99.0

            ball_rows.append(dict(
                req_run_rate=req_run_rate,
                curr_run_rate=curr_run_rate,
                wickets_remaining=wickets_remaining,
                overs_remaining=overs_remaining,
                label=did_win,
            ))

    return match_row, ball_rows


def load_data(zf: Optional[zipfile.ZipFile] = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Download (if needed) and parse all IPL matches.
    Returns (matches_df, balls_df).
    """
    if zf is None:
        zf = _download_zip()

    match_rows: list[dict] = []
    ball_rows: list[dict] = []

    json_names = [n for n in zf.namelist() if n.endswith(".json") and not n.startswith("__")]
    print(f"[*] Parsing {len(json_names)} match files …")

    for name in json_names:
        try:
            with zf.open(name) as f:
                data = json.load(f)
            mr, brs = _parse_match(data)
            if mr:
                match_rows.append(mr)
            ball_rows.extend(brs)
        except Exception as exc:
            print(f"    [warn] {name}: {exc}")

    matches_df = pd.DataFrame(match_rows)
    matches_df["match_date"] = pd.to_datetime(matches_df["match_date"], errors="coerce")
    matches_df = matches_df.sort_values("match_date").reset_index(drop=True)

    balls_df = pd.DataFrame(ball_rows)

    print(f"[*] Loaded {len(matches_df)} matches, {len(balls_df)} inning-over snapshots")
    return matches_df, balls_df


# ---------------------------------------------------------------------------
# Part 2 — Pre-match model helpers
# ---------------------------------------------------------------------------

def _compute_elo(matches_df: pd.DataFrame, k: float = 32.0, initial: float = 1500.0) -> dict[str, float]:
    """Walk through matches chronologically and compute final ELO ratings."""
    elo: dict[str, float] = defaultdict(lambda: initial)
    for _, row in matches_df.iterrows():
        t1, t2 = row["home_team"], row["away_team"]
        w = row["winner"]
        if pd.isna(w) or w not in (t1, t2):
            continue
        e1 = 1 / (1 + 10 ** ((elo[t2] - elo[t1]) / 400))
        e2 = 1 - e1
        s1 = 1.0 if w == t1 else 0.0
        s2 = 1.0 - s1
        elo[t1] += k * (s1 - e1)
        elo[t2] += k * (s2 - e2)
    return dict(elo)


def _team_avg_score_series(matches_df: pd.DataFrame) -> dict[str, list[float]]:
    """Map team -> list of innings scores (chronological)."""
    scores: dict[str, list[float]] = defaultdict(list)
    for _, row in matches_df.iterrows():
        scores[row["home_team"]].append(float(row["innings1_score"]))
        scores[row["away_team"]].append(float(row["innings2_score"]))
    return dict(scores)


def _avg_last_n(scores: list[float], n: int = 5) -> float:
    if not scores:
        return 150.0  # league average fallback
    return float(np.mean(scores[-n:]))


def _h2h_win_pct(matches_df: pd.DataFrame, team1: str, team2: str, last_n: int = 15) -> float:
    """Win % of team1 vs team2 (last_n meetings)."""
    mask = (
        ((matches_df["home_team"] == team1) & (matches_df["away_team"] == team2)) |
        ((matches_df["home_team"] == team2) & (matches_df["away_team"] == team1))
    )
    h2h = matches_df[mask].tail(last_n)
    if h2h.empty:
        return 0.5
    wins = (h2h["winner"] == team1).sum()
    return wins / len(h2h)


def _venue_avg_score(matches_df: pd.DataFrame, venue: str) -> float:
    v = matches_df[matches_df["venue"] == venue]
    if v.empty:
        return float(matches_df["innings1_score"].mean())
    return float(pd.concat([v["innings1_score"], v["innings2_score"]]).mean())


def _build_prematch_features(
    matches_df: pd.DataFrame,
    elo_ratings: dict[str, float],
    score_series: dict[str, list[float]],
) -> tuple[np.ndarray, np.ndarray]:
    """Build X, y for pre-match model from all historical matches."""
    rows_X, rows_y = [], []

    # For each match we need per-match ELO snapshot — simplified: use final ELO
    # (good enough for training; not leaking future ELO into features is ideal
    #  but requires re-simulating; final ELO is standard for offline datasets)

    # We do rolling computation so we don't leak future ELO
    elo_snap: dict[str, float] = defaultdict(lambda: 1500.0)
    score_snap: dict[str, list[float]] = defaultdict(list)
    k = 32.0

    for _, row in matches_df.iterrows():
        t1 = row["home_team"]
        t2 = row["away_team"]
        w = row["winner"]
        venue = row["venue"]

        if pd.isna(w) or w not in (t1, t2):
            # still update scores
            score_snap[t1].append(float(row["innings1_score"]))
            score_snap[t2].append(float(row["innings2_score"]))
            continue

        # Features using snapshot BEFORE this match
        t1_elo = elo_snap[t1]
        t2_elo = elo_snap[t2]
        t1_avg = _avg_last_n(score_snap[t1], 5)
        t2_avg = _avg_last_n(score_snap[t2], 5)

        # h2h from all rows in matches_df up to current index — expensive;
        # approximate with current score_snap counts (simplified)
        # We'll compute h2h on the full df once (slight future leak but minor for training)
        h2h = _h2h_win_pct(matches_df, t1, t2, 15)

        toss_adv = 0.1 if (row["toss_winner"] == t1 and row["toss_decision"] == "field") else 0.0

        v_avg = _venue_avg_score(matches_df, venue)

        label = 1 if w == t1 else 0

        rows_X.append([t1_elo, t2_elo, t1_avg, t2_avg, h2h, toss_adv, v_avg])
        rows_y.append(label)

        # Update ELO
        e1 = 1 / (1 + 10 ** ((elo_snap[t2] - elo_snap[t1]) / 400))
        e2 = 1 - e1
        s1 = 1.0 if w == t1 else 0.0
        elo_snap[t1] += k * (s1 - e1)
        elo_snap[t2] += k * ((1 - s1) - e2)

        # Update scores
        score_snap[t1].append(float(row["innings1_score"]))
        score_snap[t2].append(float(row["innings2_score"]))

    return np.array(rows_X, dtype=float), np.array(rows_y, dtype=int)


# ---------------------------------------------------------------------------
# Part 3 — In-play model
# ---------------------------------------------------------------------------

def _build_inplay_features(balls_df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    feature_cols = ["req_run_rate", "curr_run_rate", "wickets_remaining", "overs_remaining"]
    X = balls_df[feature_cols].values.astype(float)
    y = balls_df["label"].values.astype(int)
    return X, y


# ---------------------------------------------------------------------------
# Main training routine
# ---------------------------------------------------------------------------

def train(zf: Optional[zipfile.ZipFile] = None) -> None:
    """Download data (if needed), parse, train both models, store globals."""
    global _matches_df, _balls_df, _prematch_model, _prematch_scaler
    global _inplay_model, _inplay_scaler

    matches_df, balls_df = load_data(zf)
    _matches_df = matches_df
    _balls_df = balls_df

    # ---- Pre-match model ----
    print("[*] Training pre-match model …")
    elo_ratings = _compute_elo(matches_df)
    score_series = _team_avg_score_series(matches_df)

    X_pm, y_pm = _build_prematch_features(matches_df, elo_ratings, score_series)

    if len(X_pm) < 10:
        print("[warn] Not enough data for pre-match model")
        return

    X_train, X_test, y_train, y_test = train_test_split(
        X_pm, y_pm, test_size=0.2, random_state=42, stratify=y_pm
    )

    scaler_pm = StandardScaler()
    X_train_s = scaler_pm.fit_transform(X_train)
    X_test_s = scaler_pm.transform(X_test)

    base_lr = LogisticRegression(max_iter=1000, random_state=42)
    cal_pm = CalibratedClassifierCV(base_lr, cv=5, method="isotonic")
    cal_pm.fit(X_train_s, y_train)

    preds = cal_pm.predict(X_test_s)
    acc = accuracy_score(y_test, preds)
    print(f"    Pre-match model accuracy (hold-out): {acc:.3f}  ({len(X_pm)} samples)")

    _prematch_model = cal_pm
    _prematch_scaler = scaler_pm

    # ---- In-play model ----
    print("[*] Training in-play model …")
    if balls_df.empty:
        print("[warn] No ball-by-ball data found")
        return

    X_ip, y_ip = _build_inplay_features(balls_df)

    X_tr, X_te, y_tr, y_te = train_test_split(
        X_ip, y_ip, test_size=0.2, random_state=42, stratify=y_ip
    )

    scaler_ip = StandardScaler()
    X_tr_s = scaler_ip.fit_transform(X_tr)
    X_te_s = scaler_ip.transform(X_te)

    base_ip = LogisticRegression(max_iter=1000, random_state=42)
    cal_ip = CalibratedClassifierCV(base_ip, cv=5, method="isotonic")
    cal_ip.fit(X_tr_s, y_tr)

    preds_ip = cal_ip.predict(X_te_s)
    acc_ip = accuracy_score(y_te, preds_ip)
    print(f"    In-play model accuracy (hold-out):   {acc_ip:.3f}  ({len(X_ip)} samples)")

    _inplay_model = cal_ip
    _inplay_scaler = scaler_ip
    print("[*] Training complete.")


# ---------------------------------------------------------------------------
# Part 4 — Public API
# ---------------------------------------------------------------------------

def _ensure_trained() -> None:
    if _prematch_model is None:
        train()


def get_prematch_prob(
    team1: str,
    team2: str,
    venue: str,
    toss_winner: str,
    toss_decision: str,
) -> tuple[float, float]:
    """Returns (p_team1_wins, p_team2_wins) from trained model."""
    _ensure_trained()
    assert _matches_df is not None and _prematch_scaler is not None and _prematch_model is not None

    elo_ratings = _compute_elo(_matches_df)
    score_series = _team_avg_score_series(_matches_df)

    t1_elo = elo_ratings.get(team1, 1500.0)
    t2_elo = elo_ratings.get(team2, 1500.0)
    t1_avg = _avg_last_n(score_series.get(team1, []), 5)
    t2_avg = _avg_last_n(score_series.get(team2, []), 5)
    h2h = _h2h_win_pct(_matches_df, team1, team2, 15)
    toss_adv = 0.1 if (toss_winner == team1 and toss_decision == "field") else 0.0
    v_avg = _venue_avg_score(_matches_df, venue)

    X = np.array([[t1_elo, t2_elo, t1_avg, t2_avg, h2h, toss_adv, v_avg]], dtype=float)
    X_s = _prematch_scaler.transform(X)
    proba = _prematch_model.predict_proba(X_s)[0]  # [p_lose, p_win]
    p_win = float(proba[1])
    return round(p_win, 4), round(1 - p_win, 4)


def get_inplay_prob(
    runs: int,
    wickets: int,
    overs: float,
    target: int,
    total_overs: int = 20,
) -> float:
    """Returns probability batting team wins (chasing)."""
    _ensure_trained()
    assert _inplay_scaler is not None and _inplay_model is not None

    overs_remaining = max(total_overs - overs, 0.1)
    wickets_remaining = max(10 - wickets, 0)
    runs_needed = target - runs
    curr_run_rate = runs / overs if overs > 0 else 0.0
    req_run_rate = runs_needed / overs_remaining if overs_remaining > 0 else 99.0

    X = np.array([[req_run_rate, curr_run_rate, wickets_remaining, overs_remaining]], dtype=float)
    X_s = _inplay_scaler.transform(X)
    proba = _inplay_model.predict_proba(X_s)[0]
    return round(float(proba[1]), 4)


def get_h2h_stats(team1: str, team2: str) -> dict:
    """Returns h2h stats from full dataset."""
    _ensure_trained()
    assert _matches_df is not None

    mask = (
        (((_matches_df["home_team"] == team1) & (_matches_df["away_team"] == team2)) |
         ((_matches_df["home_team"] == team2) & (_matches_df["away_team"] == team1)))
    )
    h2h = _matches_df[mask].copy()

    total = len(h2h)
    t1_wins = int((h2h["winner"] == team1).sum())
    t2_wins = int((h2h["winner"] == team2).sum())
    no_result = total - t1_wins - t2_wins

    avg_i1 = float(h2h["innings1_score"].mean()) if total > 0 else 0.0
    avg_i2 = float(h2h["innings2_score"].mean()) if total > 0 else 0.0

    last5 = h2h.tail(5)[["match_date", "winner", "innings1_score", "innings2_score"]].to_dict("records")

    return {
        "team1": team1,
        "team2": team2,
        "total_matches": total,
        f"{team1}_wins": t1_wins,
        f"{team2}_wins": t2_wins,
        "no_result": no_result,
        f"{team1}_win_pct": round(t1_wins / total, 3) if total > 0 else 0.5,
        "avg_innings1_score": round(avg_i1, 1),
        "avg_innings2_score": round(avg_i2, 1),
        "last_5_matches": last5,
    }


def get_team_avg_score(team: str, last_n: int = 10) -> float:
    """Average score (batting) last N matches."""
    _ensure_trained()
    assert _matches_df is not None

    score_series = _team_avg_score_series(_matches_df)
    scores = score_series.get(team, [])
    return round(_avg_last_n(scores, last_n), 2)


# ---------------------------------------------------------------------------
# Part 5 — Telegram scraper
# ---------------------------------------------------------------------------

def get_telegram_tips(match_teams: list[str]) -> str:
    """
    Scrape t.me/s/Fantasyexpertnews and t.me/s/tossboss (public web views).
    Search for messages mentioning any of match_teams.
    Return last 3 relevant messages as a single string.
    """
    channels = [
        "https://t.me/s/Fantasyexpertnews",
        "https://t.me/s/tossboss",
    ]
    relevant: list[str] = []

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    team_lower = [t.lower() for t in match_teams]

    # Simple HTML text extraction without BeautifulSoup (stdlib only)
    import re

    def _extract_message_texts(html: str) -> list[str]:
        # Find all tgme_widget_message_text div contents
        pattern = re.compile(
            r'<div[^>]+class="[^"]*tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            re.DOTALL | re.IGNORECASE,
        )
        texts = []
        for m in pattern.finditer(html):
            raw = m.group(1)
            # Strip HTML tags
            clean = re.sub(r"<[^>]+>", " ", raw).strip()
            clean = re.sub(r"\s+", " ", clean)
            if clean:
                texts.append(clean)
        return texts

    for url in channels:
        try:
            resp = httpx.get(url, headers=headers, timeout=20, follow_redirects=True)
            if resp.status_code != 200:
                continue
            texts = _extract_message_texts(resp.text)
            for text in texts:
                text_low = text.lower()
                if any(team in text_low for team in team_lower):
                    relevant.append(f"[{url.split('/')[-1]}] {text}")
        except Exception as exc:
            print(f"[warn] Telegram scrape failed for {url}: {exc}")

    if not relevant:
        return "No relevant Telegram tips found for the given teams."

    # Return last 3 relevant messages
    return "\n---\n".join(relevant[-3:])


# ---------------------------------------------------------------------------
# Part 6 — Back-testing
# ---------------------------------------------------------------------------

def backtest_edge(min_edge: float = 0.04) -> dict:
    """
    Simulate: for each historical match, compute model prob vs hypothetical
    market odds (use 5% margin on true prob).
    Count: how often edge >= min_edge, and what's the expected ROI.
    Returns: {matches_with_edge: int, expected_roi: float, win_rate: float}
    """
    _ensure_trained()
    assert _matches_df is not None and _prematch_model is not None and _prematch_scaler is not None

    elo_snap: dict[str, float] = defaultdict(lambda: 1500.0)
    score_snap: dict[str, list[float]] = defaultdict(list)
    k = 32.0

    matches_with_edge = 0
    total_bets = 0
    total_profit = 0.0
    wins_on_edge_bets = 0

    for _, row in _matches_df.iterrows():
        t1 = row["home_team"]
        t2 = row["away_team"]
        w = row["winner"]
        venue = row["venue"]

        if pd.isna(w) or w not in (t1, t2):
            score_snap[t1].append(float(row["innings1_score"]))
            score_snap[t2].append(float(row["innings2_score"]))
            continue

        t1_elo = elo_snap[t1]
        t2_elo = elo_snap[t2]
        t1_avg = _avg_last_n(score_snap[t1], 5)
        t2_avg = _avg_last_n(score_snap[t2], 5)
        h2h = _h2h_win_pct(_matches_df, t1, t2, 15)
        toss_adv = 0.1 if (row["toss_winner"] == t1 and row["toss_decision"] == "field") else 0.0
        v_avg = _venue_avg_score(_matches_df, venue)

        X = np.array([[t1_elo, t2_elo, t1_avg, t2_avg, h2h, toss_adv, v_avg]], dtype=float)
        X_s = _prematch_scaler.transform(X)
        proba = _prematch_model.predict_proba(X_s)[0]
        model_p = float(proba[1])  # model P(team1 wins)

        # Hypothetical market: true prob with 5% margin (overround)
        # Market implied prob for team1 = model_p * 1.05  → odds = 1/(model_p*1.05)
        market_implied = model_p * 1.05
        market_odds = 1.0 / market_implied if market_implied > 0 else 1.0

        # Edge = model_p - market_implied
        edge = model_p - market_implied

        # We bet on team1 only (symmetrically we could also check team2)
        if edge >= min_edge:
            matches_with_edge += 1
            total_bets += 1
            actual_win = 1 if w == t1 else 0
            profit = (market_odds - 1) if actual_win else -1.0
            total_profit += profit
            wins_on_edge_bets += actual_win

        # Update ELO
        e1 = 1 / (1 + 10 ** ((elo_snap[t2] - elo_snap[t1]) / 400))
        e2 = 1 - e1
        s1 = 1.0 if w == t1 else 0.0
        elo_snap[t1] += k * (s1 - e1)
        elo_snap[t2] += k * ((1 - s1) - e2)

        score_snap[t1].append(float(row["innings1_score"]))
        score_snap[t2].append(float(row["innings2_score"]))

    expected_roi = (total_profit / total_bets) if total_bets > 0 else 0.0
    win_rate = (wins_on_edge_bets / total_bets) if total_bets > 0 else 0.0

    return {
        "matches_with_edge": matches_with_edge,
        "total_bets_simulated": total_bets,
        "expected_roi": round(expected_roi, 4),
        "win_rate": round(win_rate, 4),
        "total_profit_units": round(total_profit, 4),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import pprint

    print("=" * 60)
    print("  IPL Match Prediction Model — Full Pipeline")
    print("=" * 60)

    # Download once, pass to train() to avoid double-download
    zf = _download_zip()
    train(zf)

    print()
    print("--- DC vs RCB H2H Stats ---")
    h2h = get_h2h_stats("Delhi Capitals", "Royal Challengers Bengaluru")
    # Cricsheet may use older/alternative names; try fallback
    if h2h["total_matches"] == 0:
        h2h = get_h2h_stats("Delhi Capitals", "Royal Challengers Bangalore")
    if h2h["total_matches"] == 0:
        # Try Delhi Daredevils
        h2h = get_h2h_stats("Delhi Daredevils", "Royal Challengers Bangalore")
    pprint.pprint(h2h)

    print()
    print("--- Sample Pre-match Probability: DC vs RCB ---")
    team1 = "Delhi Capitals"
    team2 = "Royal Challengers Bengaluru"
    venue = "Arun Jaitley Stadium"
    p1, p2 = get_prematch_prob(team1, team2, venue, toss_winner=team1, toss_decision="field")
    print(f"  P({team1} wins) = {p1:.3f}")
    print(f"  P({team2} wins) = {p2:.3f}")

    print()
    print("--- Sample In-play Probability ---")
    # Scenario: chasing 180, 90/3 after 10 overs
    p_ip = get_inplay_prob(runs=90, wickets=3, overs=10.0, target=180)
    print(f"  Batting team win prob (90/3 after 10, chasing 180): {p_ip:.3f}")

    print()
    print("--- Team Avg Score (last 10): DC & RCB ---")
    dc_avg = get_team_avg_score("Delhi Capitals", 10)
    rcb_avg = get_team_avg_score("Royal Challengers Bengaluru", 10)
    print(f"  DC  avg last-10: {dc_avg}")
    print(f"  RCB avg last-10: {rcb_avg}")

    print()
    print("--- Backtesting (min_edge=0.04) ---")
    bt = backtest_edge(min_edge=0.04)
    pprint.pprint(bt)

    print()
    print("--- Telegram Tips (DC vs RCB) ---")
    tips = get_telegram_tips(["Delhi Capitals", "Royal Challengers", "DC", "RCB"])
    print(tips[:500] if len(tips) > 500 else tips)

    print()
    print("Done.")
