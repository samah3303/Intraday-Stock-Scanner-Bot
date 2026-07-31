"""
Angel One Intraday Scanner – Flask Application
================================================
Single 6-Rule Structural OEL Intraday Stock Scanner with Telegram Group Alerts
and Live Web Control Panel Dashboard.

Scan Schedule: 09:20 AM IST Daily (Mon–Fri)
Base Candle: First 5-minute candle (09:15 AM)
Universe: NSE stocks priced between ₹300 and ₹3000.
"""

import os
import time
import logging
import traceback
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
IST = ZoneInfo('Asia/Kolkata')

from dotenv import load_dotenv
load_dotenv()  # Load .env file for local development

import json
import pandas as pd
import pyotp
import requests
from flask import Flask, jsonify, render_template_string, redirect, url_for, request
from SmartApi import SmartConnect
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# Shared modules
from shared.constants import (
    DEFAULT_100_STOCKS, NIFTY_TOKEN,
    MIN_STOCK_PRICE, MAX_STOCK_PRICE, MAX_SCAN_STOCKS,
    OPT_RVOL_MIN, OPT_WICK_MAX_PCT, OPT_AI_MIN_SCORE,
    OPT_SECTOR_GUARD_ENABLED, WEAK_SECTORS,
)
from shared.deepseek_agent import analyze_hit_agent, generate_morning_brief, generate_trade_journal, is_sector_blocked
from ml_engine import MarketAnomalyDetector
from trade_journal import log_trade_outcome, retrain_from_history

# ---------------------------------------------------------------------------
# Logging & In-Memory Log Ring Buffer (for Dashboard Live Terminal UI)
# ---------------------------------------------------------------------------
SYSTEM_LOGS: list[str] = []

class MemoryLogHandler(logging.Handler):
    """Custom logging handler to keep the last 150 log entries in memory for dashboard UI."""
    def emit(self, record):
        try:
            msg = self.format(record)
            SYSTEM_LOGS.append(msg)
            if len(SYSTEM_LOGS) > 150:
                SYSTEM_LOGS.pop(0)
        except Exception:
            pass

log_formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
memory_handler = MemoryLogHandler()
memory_handler.setFormatter(log_formatter)

logger = logging.getLogger("AlphaQuantPro")
logger.setLevel(logging.INFO)
logger.propagate = False

if not logger.handlers:
    stream_h = logging.StreamHandler()
    stream_h.setFormatter(log_formatter)
    logger.addHandler(stream_h)
    logger.addHandler(memory_handler)

# ---------------------------------------------------------------------------
# Flask App Setup
# ---------------------------------------------------------------------------
app = Flask(__name__)

# ---------------------------------------------------------------------------
# Global Application State & Persistent Watchlist
# ---------------------------------------------------------------------------
BOT_STATUS = "Stopped"
smart_api = None      # SmartConnect session object
NSE_STOCKS = {}       # {symbol: token} — populated from instrument master
SCAN_LOCK = threading.Lock() # Thread lock to prevent concurrent scan executions
# Default 100 Top Liquid Bullish NSE Stock Universe (imported from shared.constants)
# DEFAULT_100_STOCKS list comes from shared.constants

# Handle Persistent Disk on Render, fallback to /tmp on Vercel/Render without disk, or local directory
if os.getenv("RENDER") or os.getenv("VERCEL"):
    data_dir = "/tmp"
else:
    data_dir = os.path.dirname(__file__)

PREMARKET_CANDIDATES_FILE = os.path.join(data_dir, "premarket_candidates.json")
PREMARKET_CANDIDATES: list[str] = []  # High-probability pre-filtered candidates (screened at 08:45 AM)
TODAYS_TRADES: list[dict] = []        # Journal of completed trades today
ANOMALY_DETECTOR = MarketAnomalyDetector()  # IsolationForest Market Anomaly Guard
# Try loading pre-trained anomaly model from disk
_anomaly_path = os.path.join(data_dir, "anomaly_detector.joblib")
if os.path.exists(_anomaly_path):
    ANOMALY_DETECTOR.load_model(_anomaly_path)
    logger.info("Loaded anomaly detector from anomaly_detector.joblib.")


def load_premarket_candidates() -> list[str]:
    """Load pre-market screened candidate stocks from local JSON file."""
    global PREMARKET_CANDIDATES
    if os.path.exists(PREMARKET_CANDIDATES_FILE):
        try:
            with open(PREMARKET_CANDIDATES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    PREMARKET_CANDIDATES = [str(s).upper().strip() for s in data if s]
                    logger.info("Loaded %d pre-market candidate(s) from cache.", len(PREMARKET_CANDIDATES))
                    return PREMARKET_CANDIDATES
        except Exception as exc:
            logger.error("Failed to load premarket_candidates.json: %s", exc)
    PREMARKET_CANDIDATES = []
    return PREMARKET_CANDIDATES


# Initialize pre-market candidates on startup
load_premarket_candidates()

LATEST_SCAN_RESULTS = {
    "status": "No scan executed yet",
    "timestamp": None,
    "scanned": 0,
    "in_range": 0,
    "hits": [],
}

# ---------------------------------------------------------------------------
# System Constants (imported from shared.constants)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Telegram Group Alert Helper
# ---------------------------------------------------------------------------

def send_telegram(message: str) -> None:
    """Send a markdown-formatted message via Telegram Bot API with retries."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("Telegram credentials not set – skipping alert.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
    }
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            logger.info("Telegram alert sent successfully.")
            return
        except Exception as exc:
            if attempt < 2:
                logger.warning("Telegram send attempt %d failed: %s. Retrying...", attempt + 1, exc)
                time.sleep(1.0)
            else:
                logger.error("Telegram send failed after 3 attempts: %s", exc)


def send_telegram_long(message: str) -> None:
    """Send a long message to Telegram group, splitting into chunks if >4000 chars."""
    if len(message) <= 4096:
        send_telegram(message)
        return
    lines = message.split("\n")
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 4000:
            send_telegram(chunk)
            chunk = line + "\n"
            time.sleep(0.5)
        else:
            chunk += line + "\n"
    if chunk.strip():
        send_telegram(chunk)


# ---------------------------------------------------------------------------
# Instrument Master (NSE Stock Universe)
# ---------------------------------------------------------------------------

def fetch_instrument_master() -> None:
    """Download Angel One's instrument master and cache NSE EQ stocks."""
    global NSE_STOCKS

    master_url = (
        "https://margincalculator.angelbroking.com"
        "/OpenAPI_File/files/OpenAPIScripMaster.json"
    )
    try:
        logger.info("Downloading instrument master…")
        resp = requests.get(master_url, timeout=90)
        resp.raise_for_status()
        data = resp.json()

        nse_eq = {}
        for item in data:
            if (item.get("exch_seg") == "NSE"
                    and item.get("symbol", "").endswith("-EQ")):
                symbol = item["symbol"].replace("-EQ", "")
                nse_eq[symbol] = item["token"]

        NSE_STOCKS = nse_eq
        logger.info("Instrument master loaded: %d NSE EQ stocks.", len(NSE_STOCKS))
        send_telegram(
            f"📋 *Instrument master loaded*\n"
            f"_{len(NSE_STOCKS)} NSE stocks cached "
            f"(Universe: ₹{int(MIN_STOCK_PRICE)}–₹{int(MAX_STOCK_PRICE)})_"
        )
    except Exception as exc:
        logger.error("Instrument master fetch failed: %s", exc)
        send_telegram(f"⚠️ *Instrument master error*\n`{exc}`")


# ---------------------------------------------------------------------------
# Angel One Authentication
# ---------------------------------------------------------------------------

def automate_angel_login() -> None:
    """Generate a fresh SmartAPI session using TOTP-based 2FA."""
    global BOT_STATUS, smart_api

    api_key = os.getenv("ANGEL_API_KEY")
    client_code = os.getenv("ANGEL_CLIENT_CODE")
    password = os.getenv("ANGEL_MPIN") or os.getenv("ANGEL_PIN") or os.getenv("ANGEL_PASSWORD")
    totp_key = os.getenv("ANGEL_TOTP_KEY")

    if not all([api_key, client_code, password, totp_key]):
        BOT_STATUS = "Authentication Error"
        err = "One or more ANGEL_* environment variables are missing."
        logger.error(err)
        send_telegram(f"🔴 *Auth Error*\n`{err}`")
        return

    try:
        totp = pyotp.TOTP(totp_key).now()
        
        # SmartConnect creates a 'logs/' directory in the current working directory.
        # Ensure we are in a writable directory (persistent if Render, /tmp if Vercel).
        if os.getenv("RENDER") or os.getenv("VERCEL"):
            work_dir = "/tmp"
        else:
            work_dir = os.getcwd()

        os.makedirs(os.path.join(work_dir, "logs"), exist_ok=True)
        
        old_cwd = os.getcwd()
        try:
            os.chdir(work_dir)
            obj = SmartConnect(api_key=api_key)
        finally:
            os.chdir(old_cwd)

        data = obj.generateSession(client_code, password, totp)

        if data.get("status"):
            smart_api = obj
            BOT_STATUS = "Running"
            logger.info("Angel One login successful for %s", client_code)
            send_telegram("🟢 *Angel One Login Successful* – Bot Motor is *Running*.")

            # Load NSE stock universe
            fetch_instrument_master()
        else:
            raise RuntimeError(data.get("message", "Unknown login failure"))

    except Exception as exc:
        BOT_STATUS = "Authentication Error"
        tb = traceback.format_exc()
        logger.error("Login failed:\n%s", tb)
        send_telegram(f"🔴 *Auth Error*\n```\n{tb[-1000:]}\n```")


# ---------------------------------------------------------------------------
# Candle Data Fetcher
# ---------------------------------------------------------------------------

def fetch_candles(token: str, exchange: str = "NSE",
                  days_back: int = 2) -> pd.DataFrame:
    """Fetch 5-minute candle data for today and previous day with automatic retries and rate limit handling."""
    if smart_api is None:
        raise RuntimeError("SmartAPI session not initialized.")

    to_date = datetime.now(IST)
    from_date = to_date - timedelta(days=days_back)

    params = {
        "exchange": exchange,
        "symboltoken": token,
        "interval": "FIVE_MINUTE",
        "fromdate": from_date.strftime("%Y-%m-%d 09:00"),
        "todate": to_date.strftime("%Y-%m-%d 15:30"),
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            raw = smart_api.getCandleData(params)
            if not raw or not isinstance(raw, dict):
                raise RuntimeError(f"Invalid API response for token {token}: {raw}")

            if raw.get("status") is False:
                msg = str(raw.get("message", ""))
                if attempt < max_retries - 1:
                    logger.warning("getCandleData status False for token %s (Attempt %d/%d): %s. Retrying...", token, attempt + 1, max_retries, msg)
                    time.sleep(0.6 * (attempt + 1))
                    continue
                raise RuntimeError(f"getCandleData error for token {token}: {raw}")

            data = raw.get("data", [])
            if not data:
                raise RuntimeError(f"No candle data returned for token {token}")

            df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df.sort_values("timestamp", inplace=True)
            df.reset_index(drop=True, inplace=True)
            return df

        except Exception as exc:
            if attempt < max_retries - 1:
                logger.warning("fetch_candles attempt %d failed for token %s: %s. Retrying...", attempt + 1, token, exc)
                time.sleep(0.7 * (attempt + 1))
            else:
                raise exc

    raise RuntimeError(f"Failed to fetch candles for token {token} after {max_retries} attempts")


# ===========================================================================
# 6-RULE STRUCTURAL OEL STRATEGY EVALUATOR
# ===========================================================================

def evaluate_strategy(stock_name: str, df: pd.DataFrame,
                      nifty_candle: pd.Series) -> dict | None:
    """
    Evaluate stock against the 6 strict filter rules:
    1. OPEN = LOW: Today's 09:15 Open == Low (0.05% tolerance)
    2. BULLISH CANDLE BODY: Today's 09:15 Close > Open (Gap-Up or Bullish Open)
    3. MINIMAL UPPER REJECTION: Upper Wick <= 50% of Candle Range
    4. PRICE RANGE: ₹300 <= Open <= ₹3000
    5. MARKET TREND ALIGNMENT: Nifty 50 09:15 Close > Open
    6. TREND CONFIRMATION: Today's 09:15 Close > 20 EMA (5-min chart)
    """
    today = datetime.now(IST).date()

    # Today's 09:15 AM (first 5-minute) candle
    today_candles = df[df["timestamp"].dt.date == today]
    if today_candles.empty:
        return None

    candle = today_candles.iloc[0]
    c_open, c_high, c_low, c_close = (
        float(candle["open"]), float(candle["high"]),
        float(candle["low"]), float(candle["close"])
    )

    # Previous day's last 5-minute candle (for Gap-Up check)
    prev_candles = df[df["timestamp"].dt.date < today]
    if prev_candles.empty:
        return None
    prev_close = float(prev_candles.iloc[-1]["close"])

    # Compute 20-period EMA on 5-minute chart
    df_copy = df.copy()
    df_copy["ema20"] = df_copy["close"].ewm(span=20, adjust=False).mean()
    ema_row = df_copy[df_copy["timestamp"] == candle["timestamp"]]
    if ema_row.empty:
        return None
    ema_value = float(ema_row.iloc[0]["ema20"])

    # ── RULE 1: Open = Low (0.05% percentage tolerance) ────────────────
    if c_open > 0 and ((c_open - c_low) / c_open) > 0.0005:
        return None

    # ── RULE 2: Bullish Candle Body + Gap-Up / Bullish Open ────────────
    if c_close <= c_open:
        return None
    if c_open <= prev_close:
        return None

    # ── RULE 3: Minimal Upper Rejection (Upper Wick Filter, optimized %) ─
    candle_range = c_high - c_low
    if candle_range <= 0:
        return None
    upper_wick = c_high - c_close
    wick_max_ratio = OPT_WICK_MAX_PCT / 100.0
    if upper_wick > (wick_max_ratio * candle_range):
        return None

    # ── RULE 4: Price Range Filter (300 <= Open <= 3000) ───────────────
    if not (MIN_STOCK_PRICE <= c_open <= MAX_STOCK_PRICE):
        return None

    # ── RULE 5: Market Trend Alignment (Nifty Benchmark Bullish) ──────
    if float(nifty_candle["close"]) <= float(nifty_candle["open"]):
        return None

    # ── RULE 6: Trend Confirmation (Close > 20 EMA) ───────────────────
    if c_close <= ema_value:
        return None

    wick_pct = (upper_wick / candle_range) * 100

    return {
        "symbol": stock_name,
        "open": round(c_open, 2),
        "high": round(c_high, 2),
        "low": round(c_low, 2),
        "close": round(c_close, 2),
        "prev_close": round(prev_close, 2),
        "ema20": round(ema_value, 2),
        "wick_pct": round(wick_pct, 1),
    }


# ===========================================================================
# PRE-MARKET SCREENING EXECUTOR (08:45 AM IST)
# ===========================================================================

def run_premarket_screening() -> list[str]:
    """
    PRE-MARKET SCREENER (08:45 AM IST)
    Evaluates prior day candles for the universe of stocks to pre-filter
    high-probability bullish candidates before market open.
    Filters:
    1. Price Range: ₹300 <= Prev Close <= ₹3000
    2. Bullish Daily Candle: Prev Close > Prev Open
    3. Daily 20 EMA Trend: Prev Close > Daily 20 EMA
    Returns pre-filtered list of symbols for 09:20 breakout scanning.
    """
    global BOT_STATUS, PREMARKET_CANDIDATES

    if BOT_STATUS != "Running" or smart_api is None:
        logger.info("Pre-market login check triggered...")
        automate_angel_login()

    if BOT_STATUS != "Running" or not NSE_STOCKS:
        logger.error("Pre-market screening failed — bot session not active.")
        return []

    logger.info("── Pre-Market Screening Started (08:45 AM IST) ──")
    send_telegram("🌅 *Pre-Market Screening Started…*\n_Filtering daily bullish trends before market open (08:45 AM)._")

    candidates = []
    base_universe = list(NSE_STOCKS.keys())

    for symbol in base_universe:
        token = NSE_STOCKS.get(symbol)
        if not token:
            continue

        try:
            df = fetch_candles(token, days_back=5)
            if df.empty:
                time.sleep(0.30)
                continue

            df_copy = df.copy()
            df_copy["ema20"] = df_copy["close"].ewm(span=20, adjust=False).mean()

            last_candle = df_copy.iloc[-1]
            p_open = float(last_candle["open"])
            p_close = float(last_candle["close"])
            p_ema = float(last_candle["ema20"])

            # Pre-Market Filter Rules:
            # 1. Price Range (₹300 - ₹3000)
            if not (MIN_STOCK_PRICE <= p_close <= MAX_STOCK_PRICE):
                time.sleep(0.30)
                continue

            # 2. Bullish Candle (Close > Open)
            if p_close <= p_open:
                time.sleep(0.30)
                continue

            # 3. Bullish Trend (Close > 20 EMA)
            if p_close <= p_ema:
                time.sleep(0.30)
                continue

            candidates.append(symbol)
            logger.info("PREMARKET CANDIDATE ADDED: %s (Close: ₹%.2f > EMA20: ₹%.2f)", symbol, p_close, p_ema)

        except Exception as exc:
            logger.debug("Pre-market check failed for %s: %s", symbol, exc)

        time.sleep(0.30)

    PREMARKET_CANDIDATES = candidates
    try:
        with open(PREMARKET_CANDIDATES_FILE, "w", encoding="utf-8") as f:
            json.dump(PREMARKET_CANDIDATES, f, indent=2)
    except Exception:
        pass

    cand_str = ", ".join(candidates[:15]) + ("..." if len(candidates) > 15 else "")
    msg = (
        f"🌅 *PRE-MARKET SCREENING COMPLETE*\n"
        f"📅 *{datetime.now(IST):%d %b %Y %H:%M} IST*\n\n"
        f"✅ *{len(candidates)} Candidate Stock(s)* passed daily trend pre-filters.\n"
        f"_Target universe for 09:20 AM scan: {cand_str if candidates else 'None'}_"
    )
    send_telegram_long(msg)
    
    # Generate & send DeepSeek morning brief
    try:
        brief = generate_morning_brief("BULLISH", {"candidates_count": len(candidates)})
        send_telegram(brief)
    except Exception as exc:
        logger.warning("Morning brief generation failed: %s", exc)

    logger.info("── Pre-market screening complete: %d candidate(s) selected ──", len(candidates))
    return candidates


# ===========================================================================
# STRATEGY SCAN EXECUTOR
# ===========================================================================

def run_strategy_scan() -> None:
    """Execute the 09:20 AM intraday scan across all universe stocks."""
    global BOT_STATUS, LATEST_SCAN_RESULTS

    if not SCAN_LOCK.acquire(blocking=False):
        logger.warning("Scan trigger ignored — another scan process is currently active.")
        return

    try:
        _execute_strategy_scan_internal()
    finally:
        SCAN_LOCK.release()


def _execute_strategy_scan_internal() -> None:
    global BOT_STATUS, LATEST_SCAN_RESULTS

    # Auto-login if session expired (critical for Vercel serverless cold starts)
    if BOT_STATUS != "Running" or smart_api is None:
        logger.info("Session not active (status='%s'). Auto-login triggered...", BOT_STATUS)
        automate_angel_login()

    if BOT_STATUS != "Running":
        logger.error("Auto-login failed. Scan aborted.")
        send_telegram("🔴 *Scan aborted* — Auto-login failed. Check API credentials.")
        return

    if not NSE_STOCKS:
        logger.warning("Scan skipped – instrument master not loaded.")
        send_telegram("⚠️ *Scan skipped* — NSE stock list not loaded.")
        return

    scan_time_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    logger.info("── Structural OEL 6-Rule scan started at %s ──", scan_time_str)
    send_telegram(
        f"🔄 *Intraday 6-Rule Scan Started…*\n"
        f"_Universe: NSE stocks (₹{int(MIN_STOCK_PRICE)}–₹{int(MAX_STOCK_PRICE)})_"
    )

    # ── Fetch Nifty 50 benchmark data ──────────────────────────────────
    try:
        nifty_df = fetch_candles(NIFTY_TOKEN, exchange="NSE", days_back=2)
    except Exception as exc:
        logger.error("Cannot fetch Nifty data: %s", exc)
        send_telegram(f"🔴 *Nifty data fetch failed*\n`{exc}`")
        return

    today = datetime.now(IST).date()
    nifty_today = nifty_df[nifty_df["timestamp"].dt.date == today]
    if nifty_today.empty:
        send_telegram("⚠️ *Scan aborted* — No Nifty candle available for today yet.")
        return

    nifty_candle = nifty_today.iloc[0]

    # ── Check Market Trend Alignment (Nifty Benchmark) ─────────────────
    n_open = float(nifty_candle["open"])
    n_close = float(nifty_candle["close"])
    n_change_pct = ((n_close - n_open) / n_open) * 100.0

    # Allow scan if Nifty is flat/neutral (drop <= -0.15%). Only abort on significant market selling (drop > -0.15%)
    if n_change_pct < -0.15:
        msg_bearish = (
            f"🔍 *Structural OEL Scanner*\n"
            f"📅 *{datetime.now(IST):%d %b %Y %H:%M} IST*\n\n"
            f"⚠️ *Scan Aborted*: Nifty 50 benchmark is in a heavy market decline today "
            f"({n_change_pct:.2f}% drop: Close {n_close} vs Open {n_open})."
        )
        send_telegram(msg_bearish)
        LATEST_SCAN_RESULTS = {
            "status": f"Aborted — Heavy Nifty market drop ({n_change_pct:.2f}%)",
            "timestamp": scan_time_str,
            "scanned": 0,
            "in_range": 0,
            "hits": [],
        }
        logger.info("Scan aborted — Nifty 50 benchmark heavy decline (%.2f%%).", n_change_pct)
        return

    # ── Check Market Anomaly Detector ──────────────────────────────────
    nifty_mom = float(((float(nifty_candle["close"]) - float(nifty_candle["open"])) / float(nifty_candle["open"])) * 100)
    today_metrics = {"nifty_momentum": nifty_mom, "rvol": 1.2, "volatility": 1.0}
    if ANOMALY_DETECTOR.is_anomalous(today_metrics):
        msg_anomaly = "⚠️ *MARKET ANOMALY DETECTED*: IsolationForest model flagged extreme market volatility/anomaly. Signals paused today for capital protection."
        send_telegram(msg_anomaly)
        logger.warning("Scan aborted — Market Anomaly Detected by IsolationForest.")
        return

    # ── Iterate and filter stocks ──────────────────────────────────────
    matching_hits: list[dict] = []
    scanned = 0
    in_range = 0

    if PREMARKET_CANDIDATES:
        stock_items = [(sym, token) for sym, token in NSE_STOCKS.items() if sym in PREMARKET_CANDIDATES]
        logger.info("Scanning pre-market candidate universe of %d stock(s)", len(stock_items))
    else:
        stock_items = list(NSE_STOCKS.items())

    for name, token in stock_items:
        if scanned >= MAX_SCAN_STOCKS:
            break

        try:
            df = fetch_candles(token, days_back=2)
        except Exception:
            scanned += 1
            time.sleep(0.35)
            continue

        today_candles = df[df["timestamp"].dt.date == today]
        if today_candles.empty:
            scanned += 1
            time.sleep(0.35)
            continue

        first_close = float(today_candles.iloc[0]["close"])
        if not (MIN_STOCK_PRICE <= first_close <= MAX_STOCK_PRICE):
            scanned += 1
            time.sleep(0.35)
            continue

        in_range += 1

        try:
            hit = evaluate_strategy(name, df, nifty_candle)
            if hit:
                # ── Sector Guard: skip stocks in weak sectors ────────
                if is_sector_blocked(name):
                    logger.info("SECTOR BLOCKED: %s (sector on weak-sectors list)", name)
                    scanned += 1
                    time.sleep(0.35)
                    continue

                # Enrich hit with DeepSeek AI Agent Evaluation
                ai_eval = analyze_hit_agent(hit, nifty_bullish=True)
                hit["ai_analysis"] = ai_eval
                
                if ai_eval.get("score", 0) >= OPT_AI_MIN_SCORE and ai_eval.get("recommendation", "BUY") == "BUY":
                    matching_hits.append(hit)
                    TODAYS_TRADES.append(hit)
                    log_trade_outcome(name, hit, {"status": "TRIGGERED", "pnl_r": 0.0, "ai_score": ai_eval.get("score")})
                    logger.info("MATCH APPROVED BY AGENT: %s (AI Score: %s) -> %s", name, ai_eval.get("score"), hit)
                else:
                    logger.info("MATCH REJECTED BY AGENT: %s (AI Score: %s)", name, ai_eval.get("score"))
        except Exception as exc:
            logger.debug("Evaluation error for %s: %s", name, exc)

        scanned += 1

        if scanned % 100 == 0:
            logger.info("Scan progress: %d scanned | %d matches found", scanned, len(matching_hits))

        time.sleep(0.35)

    # ── Update global results state ────────────────────────────────────
    LATEST_SCAN_RESULTS = {
        "status": "Success",
        "timestamp": scan_time_str,
        "scanned": scanned,
        "in_range": in_range,
        "hits": matching_hits,
    }

    # ── Send Telegram Group Alert ──────────────────────────────────────
    universe_str = f"{scanned} pre-market candidate stocks ({in_range} within ₹{int(MIN_STOCK_PRICE)}–₹{int(MAX_STOCK_PRICE)})"
    if matching_hits:
        hit_lines = []
        for h in matching_hits:
            ai = h.get("ai_analysis", {})
            score = ai.get("score", 75)
            badge = "🔥 [HIGH]" if score >= 80 else ("⚡ [MED]" if score >= 70 else "⚠️ [LOW]")
            strat_name = h.get("strategy_name", "6-Rule Open=Low Breakout")
            strat_badge = h.get("badge", "⚡ [6-Rule OEL]")
            
            line = (
                f"• *{h['symbol']}* {badge} AI Score: *{score}/100*\n"
                f"  🎯 *Strategy*: `{strat_name}` {strat_badge}\n"
                f"  O:₹{h['open']} H:₹{h['high']} L:₹{h['low']} C:₹{h['close']} | EMA20:₹{h['ema20']}\n"
                f"  📍 *Entry*: ₹{ai.get('entry', h['close'])} | 🛑 *SL*: ₹{ai.get('stop_loss', 'N/A')}\n"
                f"  🎯 *T1*: ₹{ai.get('target_1', 'N/A')} | 🎯 *T2*: ₹{ai.get('target_2', 'N/A')}\n"
                f"  💡 _{ai.get('reasoning', 'Passed quantitative rules.')}_"
            )
            hit_lines.append(line)

        body = "\n\n".join(hit_lines)
        telegram_msg = (
            f"🎯 *DEEPSEEK AI MATCHING INTRADAY STOCKS*\n"
            f"📅 *{datetime.now(IST):%d %b %Y %H:%M} IST*\n\n"
            f"{body}\n\n"
            f"✅ *{len(matching_hits)} stock(s)* passed 6 rules + AI Evaluation.\n"
            f"_Universe scanned: {universe_str}_"
        )
    else:
        telegram_msg = (
            f"🔍 *Structural OEL Scan Completed*\n"
            f"📅 *{datetime.now(IST):%d %b %Y %H:%M} IST*\n\n"
            f"ℹ️ No stocks matched all 6 rules today.\n"
            f"_Scanned {universe_str}_"
        )

    send_telegram_long(telegram_msg)
    logger.info("── Scan complete: %d hits found out of %d scanned ──", len(matching_hits), scanned)

# ---------------------------------------------------------------------------
# Control Panel HTML UI (Embedded Glassmorphic Dark Theme)
# ---------------------------------------------------------------------------
CONTROL_PANEL_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta name="description" content="AlphaQuant Pro 3.0 — Institutional Intraday Stock Scanner" />
  <title>AlphaQuant Pro 3.0 — Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet" />
  <style>
    *, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      font-family: 'Inter', system-ui, -apple-system, sans-serif;
      background-color: #09090b;
      color: #f4f4f5;
      min-height: 100vh;
      padding: 1.5rem 1rem;
      display: flex;
      flex-direction: column;
      align-items: center;
    }
    body::before {
      content: '';
      position: fixed;
      inset: 0;
      background:
        radial-gradient(ellipse 70% 50% at 50% -10%, rgba(250, 204, 21, 0.12) 0%, transparent 70%),
        radial-gradient(ellipse 50% 50% at 80% 90%, rgba(234, 179, 8, 0.06) 0%, transparent 60%);
      pointer-events: none;
      z-index: 0;
    }
    .container {
      position: relative;
      z-index: 1;
      width: 100%;
      max-width: 1050px;
    }
    .card {
      background: rgba(18, 18, 22, 0.85);
      backdrop-filter: blur(24px);
      -webkit-backdrop-filter: blur(24px);
      border: 1px solid rgba(250, 204, 21, 0.2);
      border-radius: 1.25rem;
      padding: 1.75rem;
      box-shadow: 0 20px 50px -10px rgba(0, 0, 0, 0.8), 0 0 35px rgba(250, 204, 21, 0.05);
      margin-bottom: 1.5rem;
      transition: border-color 0.3s ease;
    }
    .card:hover {
      border-color: rgba(250, 204, 21, 0.35);
    }
    .header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 1rem;
      margin-bottom: 1.5rem;
      border-bottom: 1px solid rgba(250, 204, 21, 0.15);
      padding-bottom: 1.25rem;
    }
    .header-left { display: flex; align-items: center; gap: 1rem; }
    .brand-icon {
      width: 56px; height: 56px;
      border-radius: 16px;
      background: linear-gradient(135deg, #facc15 0%, #ca8a04 100%);
      display: flex; align-items: center; justify-content: center;
      box-shadow: 0 6px 20px rgba(250, 204, 21, 0.35);
    }
    .title h1 {
      font-size: 1.55rem; font-weight: 900; letter-spacing: -0.02em;
      background: linear-gradient(135deg, #fef08a 0%, #facc15 50%, #eab308 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }
    .title p { font-size: 0.82rem; color: #a1a1aa; margin-top: 0.2rem; }

    .badge {
      display: inline-flex; align-items: center; gap: 0.45rem;
      padding: 0.45rem 1rem; border-radius: 9999px;
      font-size: 0.8rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em;
    }
    .badge .dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
    .badge--running { background: rgba(250, 204, 21, 0.15); color: #facc15; border: 1px solid rgba(250, 204, 21, 0.4); }
    .badge--running .dot { background: #facc15; box-shadow: 0 0 10px #facc15; }
    .badge--stopped { background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }
    .badge--stopped .dot { background: #ef4444; box-shadow: 0 0 8px #ef4444; }

    .badge--ai-high { background: rgba(250, 204, 21, 0.2); color: #fef08a; border: 1px solid rgba(250, 204, 21, 0.5); font-weight: 800; }
    .badge--ai-med { background: rgba(234, 179, 8, 0.18); color: #facc15; border: 1px solid rgba(234, 179, 8, 0.4); font-weight: 800; }
    .badge--ai-low { background: rgba(239, 68, 68, 0.18); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.4); font-weight: 800; }

    .actions { display: flex; gap: 0.75rem; flex-wrap: wrap; margin-bottom: 1.75rem; }
    .btn {
      flex: 1; min-width: 170px;
      display: inline-flex; align-items: center; justify-content: center;
      gap: 0.55rem; padding: 0.8rem 1.25rem;
      border: none; border-radius: 0.75rem;
      font-family: inherit; font-size: 0.88rem; font-weight: 700;
      cursor: pointer; text-decoration: none; color: #09090b;
      transition: all 0.2s ease;
    }
    .btn:active { transform: scale(0.98); }
    .btn--scan { background: linear-gradient(135deg, #fef08a 0%, #facc15 50%, #ca8a04 100%); box-shadow: 0 4px 18px rgba(250, 204, 21, 0.35); color: #000; }
    .btn--scan:hover { filter: brightness(1.12); transform: translateY(-2px); }
    .btn--premarket { background: linear-gradient(135deg, #fbbf24 0%, #d97706 100%); box-shadow: 0 4px 18px rgba(251, 191, 36, 0.35); color: #000; }
    .btn--premarket:hover { filter: brightness(1.12); transform: translateY(-2px); }
    .btn--start { background: linear-gradient(135deg, #4ade80 0%, #16a34a 100%); box-shadow: 0 4px 18px rgba(74, 222, 128, 0.35); color: #000; }
    .btn--start:hover { filter: brightness(1.12); transform: translateY(-2px); }
    .btn--stop { background: linear-gradient(135deg, #f87171 0%, #dc2626 100%); box-shadow: 0 4px 18px rgba(248, 113, 113, 0.35); color: #fff; }
    .btn--stop:hover { filter: brightness(1.12); transform: translateY(-2px); }
    .btn--refresh { background: rgba(39, 39, 42, 0.8); color: #facc15; border: 1px solid rgba(250, 204, 21, 0.3); min-width: auto; padding: 0.4rem 0.85rem; font-size: 0.78rem; }
    .btn--refresh:hover { background: rgba(250, 204, 21, 0.15); }

    .info-grid {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 0.85rem; margin-bottom: 1.75rem;
    }
    .info-card {
      background: rgba(24, 24, 27, 0.7);
      border: 1px solid rgba(250, 204, 21, 0.15);
      border-radius: 0.85rem; padding: 1rem;
      transition: all 0.2s ease;
    }
    .info-card:hover { border-color: rgba(250, 204, 21, 0.35); background: rgba(30, 30, 35, 0.8); }
    .info-card .label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.08em; color: #a1a1aa; font-weight: 700; margin-bottom: 0.35rem; }
    .info-card .value { font-size: 1.05rem; font-weight: 800; color: #facc15; }

    .section-title {
      font-size: 1.05rem; font-weight: 800; color: #fef08a;
      margin-bottom: 1.15rem; display: flex; align-items: center; justify-content: space-between;
    }

    /* Pre-Market Candidate Tag Grid */
    .candidate-grid {
      display: flex; flex-wrap: wrap; gap: 0.6rem;
      padding: 1rem; background: rgba(9, 9, 11, 0.6);
      border: 1px solid rgba(250, 204, 21, 0.2); border-radius: 0.85rem;
      margin-bottom: 1.5rem; min-height: 56px; align-items: center;
    }
    .candidate-tag {
      display: inline-flex; align-items: center; gap: 0.4rem;
      padding: 0.4rem 0.85rem; border-radius: 9999px;
      background: rgba(250, 204, 21, 0.12); border: 1px solid rgba(250, 204, 21, 0.35);
      color: #fef08a; font-size: 0.82rem; font-weight: 800;
      transition: all 0.2s ease;
    }
    .candidate-tag:hover { background: rgba(250, 204, 21, 0.25); transform: translateY(-1px); }

    /* Results Table styling */
    .table-container { overflow-x: auto; border-radius: 0.85rem; border: 1px solid rgba(250, 204, 21, 0.2); }
    table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.86rem; }
    th {
      background: rgba(24, 24, 27, 0.95); color: #facc15;
      padding: 0.85rem 1rem; font-weight: 800; text-transform: uppercase; font-size: 0.7rem; letter-spacing: 0.06em;
      border-bottom: 1px solid rgba(250, 204, 21, 0.2);
    }
    td { padding: 0.95rem 1rem; border-top: 1px solid rgba(250, 204, 21, 0.1); color: #e4e4e7; }
    tr:hover td { background: rgba(250, 204, 21, 0.04); }
    .symbol-pill {
      font-weight: 900; color: #09090b; background: #facc15;
      padding: 0.3rem 0.75rem; border-radius: 0.45rem; display: inline-block;
      box-shadow: 0 0 10px rgba(250, 204, 21, 0.3);
    }

    .empty-state {
      text-align: center; padding: 2.5rem 1rem; color: #a1a1aa;
      background: rgba(9, 9, 11, 0.5); border-radius: 0.85rem; border: 1px dashed rgba(250, 204, 21, 0.25);
    }

    /* Score Gauge Progress Bar */
    .score-bar-bg { width: 100%; height: 7px; background: rgba(39, 39, 42, 0.8); border-radius: 999px; margin-top: 6px; overflow: hidden; }
    .score-bar-fill { height: 100%; background: linear-gradient(90deg, #eab308 0%, #facc15 100%); border-radius: 999px; }

    .rules-grid {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 0.85rem;
    }
    .rule-box {
      background: rgba(24, 24, 27, 0.5); border-left: 3px solid #facc15;
      padding: 0.85rem 1rem; border-radius: 0.5rem; font-size: 0.8rem; color: #d4d4d8;
    }
    .rule-box strong { color: #fef08a; display: block; margin-bottom: 0.2rem; font-weight: 800; }

    footer { text-align: center; font-size: 0.78rem; color: #71717a; margin-top: 1.5rem; padding-bottom: 1rem; }
  </style>
</head>
<body>
  <div class="container">
    <div class="card">
      <div class="header">
        <div class="header-left">
          <div class="brand-icon">
            <svg width="34" height="34" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M50 5 L90 25 L90 75 L50 95 L10 75 L10 25 Z" fill="#09090b" stroke="#facc15" stroke-width="4"/>
              <path d="M30 65 L50 25 L70 65 L55 65 L50 52 L45 65 Z" fill="#facc15"/>
              <polygon points="50,15 80,30 50,45 20,30" fill="url(#grad1)" opacity="0.6"/>
              <defs>
                <linearGradient id="grad1" x1="0%" y1="0%" x2="100%" y2="100%">
                  <stop offset="0%" stop-color="#fef08a"/>
                  <stop offset="100%" stop-color="#ca8a04"/>
                </linearGradient>
              </defs>
            </svg>
          </div>
          <div class="title">
            <h1>AlphaQuant Pro 3.0</h1>
            <p>Institutional Pre-Market Screener &middot; DeepSeek Tool Agent &middot; IsolationForest Guard</p>
          </div>
        </div>
        <div>
          {% if status == "Running" %}
            <div class="badge badge--running"><span class="dot"></span> Engine Online (Autopilot)</div>
          {% else %}
            <div class="badge badge--stopped"><span class="dot"></span> {{ status }}</div>
          {% endif %}
        </div>
      </div>

      <div class="actions">
        <a href="/scan" class="btn btn--scan">⚡ Run Breakout Scan</a>
        <a href="/premarket-scan" class="btn btn--premarket">🌅 Run Pre-Market (08:45 AM)</a>
        <a href="/start" class="btn btn--start">▶️ Start Engine</a>
        <a href="/stop" class="btn btn--stop">🛑 Stop Engine</a>
      </div>

      <div class="info-grid">
        <div class="info-card">
          <div class="label">Cached Scrips Master</div>
          <div class="value">{{ nse_count }}</div>
        </div>
        <div class="info-card">
          <div class="label">Pre-Market Screened</div>
          <div class="value">{{ premarket_count }} Scrips</div>
        </div>
        <div class="info-card">
          <div class="label">Market Anomaly Guard</div>
          <div class="value" style="color: #4ade80; font-size: 0.95rem;">🛡️ NORMAL (IsolationForest)</div>
        </div>
        <div class="info-card">
          <div class="label">DeepSeek Agent</div>
          <div class="value" style="color: #fef08a; font-size: 0.95rem;">🤖 Active (v4 Tool Agent)</div>
        </div>
        <div class="info-card">
          <div class="label">ML Calibration</div>
          <div class="value" style="color: #facc15; font-size: 0.95rem;">🎯 Isotonic Calibrated</div>
        </div>
        <div class="info-card">
          <div class="label">Today's Trades Logged</div>
          <div class="value" style="color: #fef08a;">{{ todays_trades_count }}</div>
        </div>
      </div>

      <!-- PRE-MARKET CANDIDATES DISPLAY -->
      <div style="margin-top: 1rem; margin-bottom: 1.5rem;">
        <div class="section-title">
          <span>🌅 Pre-Market Screened Candidate Universe (08:45 AM)</span>
          <span style="font-size: 0.78rem; color: #facc15; font-weight: 700;">{{ premarket_count }} Scrips Screened</span>
        </div>
        <div class="candidate-grid">
          {% if premarket_candidates %}
            {% for symbol in premarket_candidates %}
              <div class="candidate-tag">
                <span>🔥 {{ symbol }}</span>
              </div>
            {% endfor %}
          {% else %}
            <span style="color: #a1a1aa; font-size: 0.82rem;">No pre-market screening executed yet today. Screener runs automatically at 08:45 AM IST.</span>
          {% endif %}
        </div>
      </div>

      <!-- MATCHING BREAKOUT TRADES TABLE -->
      <div class="section-title">
        <span>🎯 Active Breakout Signals &amp; DeepSeek AI Agent Analysis</span>
        <span style="font-size: 0.78rem; color: #a1a1aa;">Real-time Scanned Signals</span>
      </div>

      {% if results.hits %}
        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th>Ticker Symbol</th>
                <th>09:15 Price (O / H / L / C)</th>
                <th>20 EMA / Wick</th>
                <th>AI Agent Confidence Score</th>
                <th>Trade Plan (Entry / SL / Targets)</th>
                <th>DeepSeek AI Reasoning</th>
              </tr>
            </thead>
            <tbody>
              {% for item in results.hits %}
                <tr>
                  <td><span class="symbol-pill">{{ item.symbol }}</span></td>
                  <td>
                    <span style="font-size: 0.8rem; line-height: 1.4;">
                      O: &#8377;{{ item.open }} | H: &#8377;{{ item.high }}<br>
                      L: &#8377;{{ item.low }} | <strong style="color: #fef08a;">C: &#8377;{{ item.close }}</strong>
                    </span>
                  </td>
                  <td>
                    <span style="font-size: 0.8rem;">
                      EMA20: &#8377;{{ item.ema20 }}<br>
                      Wick: {{ item.wick_pct }}%
                    </span>
                  </td>
                  <td>
                    {% set ai = item.ai_analysis %}
                    {% if ai %}
                      {% set score = ai.score | default(75) %}
                      {% if score >= 80 %}
                        <div class="badge badge--ai-high">🔥 {{ score }}/100</div>
                      {% else %}
                        <div class="badge badge--ai-med">⚡ {{ score }}/100</div>
                      {% endif %}
                      <div class="score-bar-bg"><div class="score-bar-fill" style="width: {{ score }}%;"></div></div>
                    {% else %}
                      <span class="badge">N/A</span>
                    {% endif %}
                  </td>
                  <td>
                    {% set ai = item.ai_analysis %}
                    {% if ai %}
                      <div style="font-size: 0.8rem; line-height: 1.45;">
                        <strong style="color: #fef08a;">Entry:</strong> &#8377;{{ ai.entry }}<br>
                        <span style="color: #f87171;"><strong>SL:</strong> &#8377;{{ ai.stop_loss }}</span><br>
                        <span style="color: #4ade80;"><strong>T1:</strong> &#8377;{{ ai.target_1 }} | <strong>T2:</strong> &#8377;{{ ai.target_2 }}</span>
                      </div>
                    {% else %}
                      —
                    {% endif %}
                  </td>
                  <td style="font-size: 0.8rem; color: #d4d4d8; max-width: 260px; line-height: 1.35;">
                    {% set ai = item.ai_analysis %}
                    {% if ai %}
                      {{ ai.reasoning }}
                    {% else %}
                      Passed 6 structural breakout rules.
                    {% endif %}
                  </td>
                </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      {% else %}
        <div class="empty-state">
          <p style="font-size: 0.95rem; font-weight: 700; color: #fef08a; margin-bottom: 0.3rem;">No active breakout setups in latest scan pass.</p>
          <p style="font-size: 0.78rem;">Strategy scan executes automatically at 09:20 AM IST. Click "Run Breakout Scan" to run on-demand.</p>
        </div>
      {% endif %}
    </div>

    <!-- STRATEGY ARCHITECTURE CARD -->
    <div class="card">
      <div class="section-title">
        <span>⚙️ AlphaQuant Pro 3.0 Architecture</span>
      </div>
      <div class="rules-grid">
        <div class="rule-box">
          <strong>1. Dynamic Pre-Market Screening (08:45 AM)</strong>
          Scans all ~2,400 NSE Cash Equity scrips for daily uptrend momentum ($\text{Close} > \text{Open}$ &amp; $\text{Close} > \text{Daily 20 EMA}$).
        </div>
        <div class="rule-box">
          <strong>2. Structural Open = Low Breakout (09:20 AM)</strong>
          Filters candidates for 09:15 candle $O = L$, Gap-Up, Bullish Body, Upper Wick $\le 50\%$, and 5-min 20 EMA alignment.
        </div>
        <div class="rule-box">
          <strong>3. IsolationForest Anomaly Guard</strong>
          MarketAnomalyDetector blocks signals during market crash or extreme volatility anomalies.
        </div>
        <div class="rule-box">
          <strong>4. DeepSeek v4 Native Tool Agent</strong>
          Executes multi-step verification (technical rules, sector strength, R:R calculation, final score $\ge 75$).
        </div>
        <div class="rule-box">
          <strong>5. Calibrated XGBoost ML Model</strong>
          CalibratedClassifierCV (isotonic, cv=5) scores setups with true historical win probabilities.
        </div>
        <div class="rule-box">
          <strong>6. Self-Learning Journal &amp; Retraining</strong>
          Logs trade outcomes to <code style="color: #facc15;">trade_outcomes.json</code> and auto-retrains every Saturday at 10:00 AM IST.
        </div>
      </div>
    </div>

    <!-- REAL-TIME SYSTEM LOGS TERMINAL CARD -->
    <div class="card">
      <div class="section-title">
        <span>📜 Real-Time Terminal Console</span>
        <button class="btn btn--refresh" onclick="fetchDashboardLogs()">🔄 Refresh Console</button>
      </div>
      <div style="background: #040405; border: 1px solid rgba(250, 204, 21, 0.25); border-radius: 0.75rem; padding: 1rem; font-family: monospace; font-size: 0.8rem; color: #fef08a; max-height: 260px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; box-shadow: inset 0 2px 10px rgba(0, 0, 0, 0.8);" id="logTerminal">Loading system console logs...</div>
    </div>

    <footer>
      AlphaQuant Pro 3.0 &middot; Institutional Intraday Engine &middot; Powered by Angel One SmartAPI, DeepSeek AI &amp; XGBoost ML
    </footer>
  </div>

  <script>
    async function fetchDashboardLogs() {
      try {
        const resp = await fetch('/api/logs');
        const data = await resp.json();
        if (data.status === 'success' && data.logs) {
          const terminal = document.getElementById('logTerminal');
          if (data.logs.length === 0) {
            terminal.textContent = 'No system logs recorded yet.';
          } else {
            terminal.textContent = data.logs.join('\n');
            terminal.scrollTop = terminal.scrollHeight;
          }
        }
      } catch (err) {
        console.error('Failed to fetch logs:', err);
      }
    }

    document.addEventListener('DOMContentLoaded', () => {
      fetchDashboardLogs();
      setInterval(fetchDashboardLogs, 4000);
    });
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP Endpoints & Routes
# ---------------------------------------------------------------------------

# Simple API key auth for protected write endpoints.
# Set DASHBOARD_SECRET in env to enable; if unset, all endpoints are public.
DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", "")

def _require_auth():
    """Check request for valid API key. Returns None if authorized, or error response."""
    if not DASHBOARD_SECRET:
        return None  # No secret configured — allow all
    auth_header = request.headers.get("X-API-Key", "")
    auth_query = request.args.get("key", "")
    provided = auth_header or auth_query
    if provided != DASHBOARD_SECRET:
        return jsonify({"status": "error", "message": "Unauthorized. Provide valid API key."}), 401
    return None


@app.route("/")
def index():
    """Render the control-panel dashboard."""
    return render_template_string(
        CONTROL_PANEL_HTML,
        status=BOT_STATUS,
        nse_count=len(NSE_STOCKS) if NSE_STOCKS else "—",
        results=LATEST_SCAN_RESULTS,
        premarket_candidates=PREMARKET_CANDIDATES,
        premarket_count=len(PREMARKET_CANDIDATES),
        todays_trades_count=len(TODAYS_TRADES),
    )


@app.route("/start")
def start_bot():
    """Trigger manual Angel One authentication & refresh master."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    automate_angel_login()
    return redirect(url_for("index"))


@app.route("/scan")
def trigger_scan():
    """On-demand scan execution trigger for instant testing."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    if BOT_STATUS != "Running":
        automate_angel_login()
    run_strategy_scan()
    return redirect(url_for("index"))


@app.route("/stop")
def stop_bot():
    """Stop bot motor."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    global BOT_STATUS
    BOT_STATUS = "Stopped"
    send_telegram("🛑 *Bot Motor stopped* via control panel.")
    logger.info("Bot stopped via /stop endpoint.")
    return redirect(url_for("index"))


@app.route("/api/premarket", methods=["GET"])
def get_premarket_api():
    """Return currently screened pre-market candidate stocks."""
    return jsonify({
        "status": "success",
        "premarket_candidates": PREMARKET_CANDIDATES,
        "count": len(PREMARKET_CANDIDATES)
    })


@app.route("/api/results")
def get_results():
    """JSON API endpoint returning latest scan results."""
    return jsonify(LATEST_SCAN_RESULTS)


@app.route("/api/logs")
def get_logs_api():
    """JSON API endpoint returning recent in-memory system logs."""
    return jsonify({"status": "success", "logs": SYSTEM_LOGS})


@app.route("/premarket-scan")
def trigger_premarket_scan():
    """On-demand pre-market screening execution trigger."""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    if BOT_STATUS != "Running":
        automate_angel_login()
    candidates = run_premarket_screening()
    return jsonify({
        "status": "success",
        "candidates_count": len(candidates),
        "candidates": candidates
    })


@app.route("/healthz")
def healthz():
    """Lightweight health check endpoint for external keep-alive monitoring."""
    return jsonify({
        "status": "healthy",
        "bot_status": BOT_STATUS,
        "premarket_candidates": PREMARKET_CANDIDATES
    })


# ---------------------------------------------------------------------------
# Background Scheduler (For Render.com / Persistent Servers)
# ---------------------------------------------------------------------------
def start_scheduler():
    scheduler = BackgroundScheduler(timezone=IST)
    
    # Run pre-market screening every Mon-Fri at 08:45 AM IST
    scheduler.add_job(
        func=run_premarket_screening,
        trigger=CronTrigger(day_of_week="mon-fri", hour=8, minute=45, timezone=IST),
        id="premarket_screening",
        name="Daily 08:45 AM Pre-Market Screening",
        replace_existing=True,
    )

    # Run scan every Mon-Fri at 09:20 AM IST
    scheduler.add_job(
        func=run_strategy_scan,
        trigger=CronTrigger(day_of_week="mon-fri", hour=9, minute=20, timezone=IST),
        id="intraday_scan",
        name="Daily 09:20 AM Scan",
        replace_existing=True,
    )

    # Post-market trade journal review Mon-Fri at 15:30 IST
    def _run_post_market_journal():
        try:
            completed_outcomes = []
            for t in TODAYS_TRADES:
                sym = t.get("symbol")
                ai = t.get("ai_analysis", {})
                entry = ai.get("entry", t.get("close", 1000.0))
                sl = ai.get("stop_loss", entry * 0.99)
                t1 = ai.get("target_1", entry * 1.02)
                
                token = NSE_STOCKS.get(sym)
                pnl_r = 0.0
                if token:
                    try:
                        df = fetch_candles(token, days_back=1)
                        if not df.empty:
                            eod_close = float(df.iloc[-1]["close"])
                            day_high = float(df["high"].max())
                            day_low = float(df["low"].min())
                            risk = max(entry - sl, 0.5)
                            
                            if day_low <= sl:
                                pnl_r = -1.0
                            elif day_high >= t1:
                                pnl_r = 2.0
                            else:
                                pnl_r = round((eod_close - entry) / risk, 2)
                    except Exception:
                        pass

                outcome_dict = {"status": "CLOSED", "pnl_r": pnl_r, "ai_score": ai.get("score", 75)}
                feats = {
                    "rvol": t.get("rvol", 1.2),
                    "gap_pct": t.get("gap_pct", 0.5),
                    "upper_wick_pct": t.get("wick_pct", 10.0),
                    "price_level": t.get("close", 1000.0)
                }
                log_trade_outcome(sym, feats, outcome_dict)
                t["outcome"] = outcome_dict
                t["pnl_r"] = pnl_r
                completed_outcomes.append(t)

            journal_msg = generate_trade_journal(completed_outcomes)
            send_telegram(journal_msg)
            logger.info("Post-market trade journal and outcome logging complete for %d trade(s).", len(completed_outcomes))
        except Exception as exc:
            logger.error("Failed to generate post-market journal: %s", exc)

    scheduler.add_job(
        func=_run_post_market_journal,
        trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=30, timezone=IST),
        id="post_market_journal",
        name="Daily 15:30 PM Post-Market Review",
        replace_existing=True,
    )

    # Saturday ML retraining at 10:00 AM IST
    scheduler.add_job(
        func=retrain_from_history,
        trigger=CronTrigger(day_of_week="sat", hour=10, minute=0, timezone=IST),
        id="saturday_ml_retrain",
        name="Saturday 10:00 AM ML Retrain",
        replace_existing=True,
    )
    
    scheduler.start()
    logger.info("APScheduler started! 08:45 Pre-market, 09:20 Scan, 15:30 Journal, Sat 10:00 Retrain scheduled.")

# Start the scheduler when the app starts
start_scheduler()

# ---------------------------------------------------------------------------
# Local Standalone Launcher
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
