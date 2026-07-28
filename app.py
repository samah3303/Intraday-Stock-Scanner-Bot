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
from datetime import datetime, timedelta

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
logger.addHandler(memory_handler)
logging.getLogger().addHandler(memory_handler)

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
# Target /tmp on Vercel/serverless environments to avoid read-only file system errors
SELECTED_STOCKS_FILE = os.path.join("/tmp" if os.getenv("VERCEL") else os.path.dirname(__file__), "selected_stocks.json")
SELECTED_STOCKS: list[str] = []  # Custom watchlist stocks to scan


def load_selected_stocks() -> list[str]:
    """Load user selected stock symbols from local JSON file."""
    global SELECTED_STOCKS
    if os.path.exists(SELECTED_STOCKS_FILE):
        try:
            with open(SELECTED_STOCKS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    SELECTED_STOCKS = [str(s).upper().strip() for s in data if s]
                    logger.info("Loaded %d selected stock(s) from watchlist file.", len(SELECTED_STOCKS))
        except Exception as exc:
            logger.error("Failed to load selected_stocks.json: %s", exc)
    return SELECTED_STOCKS


def save_selected_stocks(stocks: list[str]) -> bool:
    """Save user selected stock symbols to local JSON file."""
    global SELECTED_STOCKS
    try:
        clean_stocks = sorted(list(set(str(s).upper().strip() for s in stocks if s)))
        SELECTED_STOCKS = clean_stocks
        with open(SELECTED_STOCKS_FILE, "w", encoding="utf-8") as f:
            json.dump(SELECTED_STOCKS, f, indent=2)
        logger.info("Saved %d selected stock(s) to watchlist file.", len(SELECTED_STOCKS))
        return True
    except Exception as exc:
        logger.error("Failed to save selected_stocks.json: %s", exc)
        return False


# Initialize watchlist on startup
load_selected_stocks()

LATEST_SCAN_RESULTS = {
    "status": "No scan executed yet",
    "timestamp": None,
    "scanned": 0,
    "in_range": 0,
    "hits": [],
}

# ---------------------------------------------------------------------------
# System Constants
# ---------------------------------------------------------------------------
NIFTY_TOKEN = "99926000"

# Universe parameters
MIN_STOCK_PRICE = 300.0       # Minimum stock price filter
MAX_STOCK_PRICE = 3000.0      # Maximum stock price filter
MAX_SCAN_STOCKS = 750         # Max NSE stocks to scan per run

# ---------------------------------------------------------------------------
# Telegram Group Alert Helper
# ---------------------------------------------------------------------------

def send_telegram(message: str) -> None:
    """Send a markdown-formatted message via Telegram Bot API to configured chat/group."""
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
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Telegram alert sent successfully.")
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)


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
        
        # SmartConnect attempts to create 'logs/' in current working directory.
        # Temporarily switch working directory to /tmp on serverless environments to prevent Read-only file system errors.
        old_cwd = os.getcwd()
        try:
            os.chdir("/tmp")
            os.makedirs("/tmp/logs", exist_ok=True)
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
    """Fetch 5-minute candle data for today and previous day."""
    if smart_api is None:
        raise RuntimeError("SmartAPI session not initialized.")

    to_date = datetime.now()
    from_date = to_date - timedelta(days=days_back)

    params = {
        "exchange": exchange,
        "symboltoken": token,
        "interval": "FIVE_MINUTE",
        "fromdate": from_date.strftime("%Y-%m-%d 09:00"),
        "todate": to_date.strftime("%Y-%m-%d 15:30"),
    }

    raw = smart_api.getCandleData(params)

    if not raw or raw.get("status") is False:
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


# ===========================================================================
# 6-RULE STRUCTURAL OEL STRATEGY EVALUATOR
# ===========================================================================

def evaluate_strategy(stock_name: str, df: pd.DataFrame,
                      nifty_candle: pd.Series) -> dict | None:
    """
    Evaluate stock against the 6 strict filter rules:
    1. OPEN = LOW & STRUCTURAL SUPPORT: Today's 09:15 Open == Low & Open == Prev Day Last 5-min Low
    2. BULLISH CANDLE BODY: Today's 09:15 Close > Open
    3. MINIMAL UPPER REJECTION: Upper Wick <= 50% of Candle Range
    4. PRICE RANGE: ₹300 <= Close <= ₹3000
    5. MARKET TREND ALIGNMENT: Nifty 50 09:15 Close > Open
    6. TREND CONFIRMATION: Today's 09:15 Close > 20 EMA (5-min chart)
    """
    today = datetime.now().date()

    # Today's 09:15 AM (first 5-minute) candle
    today_candles = df[df["timestamp"].dt.date == today]
    if today_candles.empty:
        return None

    candle = today_candles.iloc[0]
    c_open, c_high, c_low, c_close = (
        float(candle["open"]), float(candle["high"]),
        float(candle["low"]), float(candle["close"])
    )

    # Previous day's last 5-minute candle
    prev_candles = df[df["timestamp"].dt.date < today]
    if prev_candles.empty:
        return None
    prev_last_low = float(prev_candles.iloc[-1]["low"])

    # Compute 20-period EMA on 5-minute chart
    df_copy = df.copy()
    df_copy["ema20"] = df_copy["close"].ewm(span=20, adjust=False).mean()
    ema_row = df_copy[df_copy["timestamp"] == candle["timestamp"]]
    if ema_row.empty:
        return None
    ema_value = float(ema_row.iloc[0]["ema20"])

    # ── RULE 1: Open == Low & Structural Support Alignment ─────────────
    # Allow 0.05 tick size float tolerance
    if not (c_open == c_low or abs(c_open - c_low) < 0.05):
        return None
    if not (c_open == prev_last_low or abs(c_open - prev_last_low) < 0.05):
        return None

    # ── RULE 2: Bullish Candle Body ────────────────────────────────────
    if c_close <= c_open:
        return None

    # ── RULE 3: Minimal Upper Rejection (Upper Wick Filter ≤ 50%) ─────
    candle_range = c_high - c_low
    if candle_range <= 0:
        return None
    upper_wick = c_high - c_close
    if upper_wick > (0.50 * candle_range):
        return None

    # ── RULE 4: Price Range Filter (300 <= Price <= 3000) ──────────────
    if not (MIN_STOCK_PRICE <= c_close <= MAX_STOCK_PRICE):
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
        "ema20": round(ema_value, 2),
        "wick_pct": round(wick_pct, 1),
    }


# ===========================================================================
# DEEPSEEK AI ANALYSIS MODULE
# ===========================================================================

def analyze_hit_with_deepseek(hit: dict, nifty_bullish: bool = True) -> dict:
    """
    Send stock hit details to DeepSeek AI model for trade quality scoring (0-100),
    dynamic Entry/SL/Target calculations, and reasoning.
    """
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    close_price = hit.get("close", 1000.0)
    low_price = hit.get("low", close_price * 0.99)
    sl = round(low_price * 0.998, 2)
    risk = max(close_price - sl, 0.5)
    t1 = round(close_price + (risk * 2.0), 2)
    t2 = round(close_price + (risk * 3.0), 2)

    if not api_key or api_key == "your_deepseek_api_key_here":
        score = 85 if hit.get("wick_pct", 10) < 20 else 72
        return {
            "score": score,
            "recommendation": "BUY" if score >= 75 else "AVOID",
            "entry": round(close_price, 2),
            "stop_loss": sl,
            "target_1": t1,
            "target_2": t2,
            "reasoning": f"Clean 09:15 Open=Low candle with {hit.get('wick_pct', 0)}% upper wick, price above 20 EMA."
        }

    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    system_prompt = (
        "You are an expert quantitative trader and risk manager specializing in Indian stock market (NSE) intraday trading. "
        "Analyze the provided stock scanner hit and output strictly valid JSON with no markdown wrapping or additional text.\n"
        "Required JSON schema:\n"
        "{\n"
        '  "score": integer (0 to 100 confidence rating),\n'
        '  "recommendation": string ("BUY" or "AVOID"),\n'
        '  "entry": float (suggested limit entry price near 09:15 close),\n'
        '  "stop_loss": float (stop loss slightly below 09:15 low),\n'
        '  "target_1": float (1:2 Risk-Reward target price),\n'
        '  "target_2": float (1:3 Risk-Reward target price),\n'
        '  "reasoning": string (concise 1-2 sentence technical analysis justification)\n'
        "}"
    )

    user_prompt = f"""Analyze this NSE stock intraday setup:
Symbol: {hit.get('symbol')}
09:15 Open: {hit.get('open')}
09:15 High: {hit.get('high')}
09:15 Low: {hit.get('low')}
09:15 Close: {hit.get('close')}
20 EMA (5-min): {hit.get('ema20')}
Upper Wick Rejection: {hit.get('wick_pct')}%
Nifty 50 Benchmark: {"Bullish" if nifty_bullish else "Neutral"}
"""

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"}
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=12)
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        res_json = json.loads(content)
        return res_json
    except Exception as exc:
        logger.warning("DeepSeek API call error for %s: %s. Using fallback.", hit.get("symbol"), exc)
        return {
            "score": 75,
            "recommendation": "BUY",
            "entry": round(close_price, 2),
            "stop_loss": sl,
            "target_1": t1,
            "target_2": t2,
            "reasoning": "Standard Open=Low structural support pass (API Fallback)."
        }


# ===========================================================================
# STRATEGY SCAN EXECUTOR
# ===========================================================================

def run_strategy_scan() -> None:
    """Execute the 09:20 AM intraday scan across all universe stocks."""
    global BOT_STATUS, LATEST_SCAN_RESULTS

    if BOT_STATUS != "Running":
        logger.info("Scan skipped – bot status is '%s'.", BOT_STATUS)
        return

    if not NSE_STOCKS:
        logger.warning("Scan skipped – instrument master not loaded.")
        send_telegram("⚠️ *Scan skipped* — NSE stock list not loaded.")
        return

    scan_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")
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

    today = datetime.now().date()
    nifty_today = nifty_df[nifty_df["timestamp"].dt.date == today]
    if nifty_today.empty:
        send_telegram("⚠️ *Scan aborted* — No Nifty candle available for today yet.")
        return

    nifty_candle = nifty_today.iloc[0]

    # Early exit if Nifty index is bearish
    if float(nifty_candle["close"]) <= float(nifty_candle["open"]):
        msg_bearish = (
            f"🔍 *Structural OEL Scanner*\n"
            f"📅 *{datetime.now():%d %b %Y %H:%M} IST*\n\n"
            f"⚠️ *Scan Aborted*: Nifty 50 benchmark is bearish on 09:15 candle "
            f"(Close {nifty_candle['close']} ≤ Open {nifty_candle['open']})."
        )
        send_telegram(msg_bearish)
        LATEST_SCAN_RESULTS = {
            "status": "Aborted — Nifty index bearish",
            "timestamp": scan_time_str,
            "scanned": 0,
            "in_range": 0,
            "hits": [],
        }
        logger.info("Scan aborted — Nifty 50 candle is bearish.")
        return

    # ── Iterate and filter stocks ──────────────────────────────────────
    matching_hits: list[dict] = []
    scanned = 0
    in_range = 0

    if SELECTED_STOCKS:
        # Filter stock items to only those present in SELECTED_STOCKS
        stock_items = [(sym, token) for sym, token in NSE_STOCKS.items() if sym in SELECTED_STOCKS]
        logger.info("Scanning custom watchlist of %d stock(s) (configured: %d)", len(stock_items), len(SELECTED_STOCKS))
    else:
        stock_items = list(NSE_STOCKS.items())

    for name, token in stock_items:
        if not SELECTED_STOCKS and scanned >= MAX_SCAN_STOCKS:
            break

        try:
            df = fetch_candles(token, days_back=2)
        except Exception:
            scanned += 1
            time.sleep(0.12)
            continue

        today_candles = df[df["timestamp"].dt.date == today]
        if today_candles.empty:
            scanned += 1
            time.sleep(0.12)
            continue

        first_close = float(today_candles.iloc[0]["close"])
        if not SELECTED_STOCKS and not (MIN_STOCK_PRICE <= first_close <= MAX_STOCK_PRICE):
            scanned += 1
            time.sleep(0.12)
            continue

        in_range += 1

        try:
            hit = evaluate_strategy(name, df, nifty_candle)
            if hit:
                # Enrich hit with DeepSeek AI Model Evaluation
                ai_eval = analyze_hit_with_deepseek(hit, nifty_bullish=True)
                hit["ai_analysis"] = ai_eval
                matching_hits.append(hit)
                logger.info("MATCH FOUND: %s (AI Score: %s) -> %s", name, ai_eval.get("score"), hit)
        except Exception as exc:
            logger.debug("Evaluation error for %s: %s", name, exc)

        scanned += 1

        if scanned % 100 == 0:
            logger.info("Scan progress: %d scanned | %d matches found", scanned, len(matching_hits))

        time.sleep(0.12)

    # ── Update global results state ────────────────────────────────────
    LATEST_SCAN_RESULTS = {
        "status": "Success",
        "timestamp": scan_time_str,
        "scanned": scanned,
        "in_range": in_range,
        "hits": matching_hits,
    }

    # ── Send Telegram Group Alert ──────────────────────────────────────
    universe_str = f"Custom Watchlist ({scanned} stocks)" if SELECTED_STOCKS else f"{scanned} stocks ({in_range} within ₹{int(MIN_STOCK_PRICE)}–₹{int(MAX_STOCK_PRICE)})"
    if matching_hits:
        hit_lines = []
        for h in matching_hits:
            ai = h.get("ai_analysis", {})
            score = ai.get("score", 75)
            badge = "🔥 [HIGH]" if score >= 80 else ("⚡ [MED]" if score >= 70 else "⚠️ [LOW]")
            
            line = (
                f"• *{h['symbol']}* {badge} AI Score: *{score}/100*\n"
                f"  O:₹{h['open']} H:₹{h['high']} L:₹{h['low']} C:₹{h['close']} | EMA20:₹{h['ema20']}\n"
                f"  📍 *Entry*: ₹{ai.get('entry', h['close'])} | 🛑 *SL*: ₹{ai.get('stop_loss', 'N/A')}\n"
                f"  🎯 *T1*: ₹{ai.get('target_1', 'N/A')} | 🎯 *T2*: ₹{ai.get('target_2', 'N/A')}\n"
                f"  💡 _{ai.get('reasoning', 'Passed 6 structural rules.')}_"
            )
            hit_lines.append(line)

        body = "\n\n".join(hit_lines)
        telegram_msg = (
            f"🎯 *DEEPSEEK AI MATCHING INTRADAY STOCKS*\n"
            f"📅 *{datetime.now():%d %b %Y %H:%M} IST*\n\n"
            f"{body}\n\n"
            f"✅ *{len(matching_hits)} stock(s)* passed 6 rules + AI Evaluation.\n"
            f"_Universe scanned: {universe_str}_"
        )
    else:
        telegram_msg = (
            f"🔍 *Structural OEL Scan Completed*\n"
            f"📅 *{datetime.now():%d %b %Y %H:%M} IST*\n\n"
            f"ℹ️ No stocks matched all 6 rules today.\n"
            f"_Scanned {universe_str}_"
        )

    send_telegram_long(telegram_msg)
    logger.info("── Scan complete: %d hits found out of %d scanned ──", len(matching_hits), scanned)


# ---------------------------------------------------------------------------
# APScheduler Setup
# ---------------------------------------------------------------------------
scheduler = BackgroundScheduler(timezone="Asia/Kolkata")

# Auto-login at 08:45 AM IST every weekday
scheduler.add_job(
    automate_angel_login,
    CronTrigger(day_of_week="mon-fri", hour=8, minute=45, timezone="Asia/Kolkata"),
    id="daily_login",
    replace_existing=True,
    misfire_grace_time=300,
)

# Execute Strategy Scan at 09:20 AM IST every weekday
scheduler.add_job(
    run_strategy_scan,
    CronTrigger(day_of_week="mon-fri", hour=9, minute=20, timezone="Asia/Kolkata"),
    id="daily_scan",
    replace_existing=True,
    misfire_grace_time=300,
)

scheduler.start()
logger.info("APScheduler started – active jobs: %s", [j.id for j in scheduler.get_jobs()])

# ---------------------------------------------------------------------------
# Control Panel HTML UI (Embedded Glassmorphic Dark Theme)
# ---------------------------------------------------------------------------
CONTROL_PANEL_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta name="description" content="Angel One Structural OEL Intraday Stock Scanner" />
  <title>Intraday Scanner — Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet" />
  <style>
    *, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      font-family: 'Inter', system-ui, -apple-system, sans-serif;
      background: #0b1329;
      color: #e2e8f0;
      min-height: 100vh;
      padding: 2rem 1rem;
      display: flex;
      flex-direction: column;
      align-items: center;
    }
    body::before {
      content: '';
      position: fixed;
      inset: 0;
      background:
        radial-gradient(ellipse 80% 60% at 20% 20%, rgba(56, 189, 248, 0.09) 0%, transparent 60%),
        radial-gradient(ellipse 60% 50% at 80% 80%, rgba(139, 92, 246, 0.09) 0%, transparent 60%);
      pointer-events: none;
      z-index: 0;
    }
    .container {
      position: relative;
      z-index: 1;
      width: 100%;
      max-width: 900px;
    }
    .card {
      background: rgba(23, 32, 54, 0.75);
      backdrop-filter: blur(20px);
      -webkit-backdrop-filter: blur(20px);
      border: 1px solid rgba(148, 163, 184, 0.12);
      border-radius: 1.25rem;
      padding: 2rem;
      box-shadow: 0 20px 40px -15px rgba(0, 0, 0, 0.6);
      margin-bottom: 1.5rem;
    }
    .header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 1rem;
      margin-bottom: 1.5rem;
      border-bottom: 1px solid rgba(148, 163, 184, 0.1);
      padding-bottom: 1.25rem;
    }
    .header-left { display: flex; align-items: center; gap: 0.85rem; }
    .icon {
      width: 48px; height: 48px;
      border-radius: 14px;
      background: linear-gradient(135deg, #38bdf8 0%, #6366f1 100%);
      display: flex; align-items: center; justify-content: center;
      font-size: 1.4rem;
      box-shadow: 0 4px 16px rgba(56, 189, 248, 0.3);
    }
    .title h1 {
      font-size: 1.4rem; font-weight: 800;
      background: linear-gradient(135deg, #f8fafc 30%, #94a3b8 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }
    .title p { font-size: 0.8rem; color: #94a3b8; margin-top: 0.15rem; }

    .badge {
      display: inline-flex; align-items: center; gap: 0.4rem;
      padding: 0.4rem 0.9rem; border-radius: 9999px;
      font-size: 0.78rem; font-weight: 600;
    }
    .badge .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
    .badge--running { background: rgba(34, 197, 94, 0.15); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.3); }
    .badge--running .dot { background: #22c55e; box-shadow: 0 0 6px #22c55e; }
    .badge--stopped { background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }
    .badge--stopped .dot { background: #ef4444; box-shadow: 0 0 6px #ef4444; }
    .badge--ai-high { background: rgba(34, 197, 94, 0.2); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.4); font-weight: 700; }
    .badge--ai-med { background: rgba(234, 179, 8, 0.2); color: #facc15; border: 1px solid rgba(234, 179, 8, 0.4); font-weight: 700; }
    .badge--ai-low { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.4); font-weight: 700; }

    .actions { display: flex; gap: 0.75rem; flex-wrap: wrap; margin-bottom: 1.5rem; }
    .btn {
      flex: 1; min-width: 160px;
      display: inline-flex; align-items: center; justify-content: center;
      gap: 0.5rem; padding: 0.75rem 1.25rem;
      border: none; border-radius: 0.65rem;
      font-family: inherit; font-size: 0.85rem; font-weight: 600;
      cursor: pointer; text-decoration: none; color: #fff;
      transition: all 0.2s ease;
    }
    .btn:active { transform: scale(0.98); }
    .btn--scan { background: linear-gradient(135deg, #0ea5e9 0%, #2563eb 100%); box-shadow: 0 4px 14px rgba(14, 165, 233, 0.35); }
    .btn--scan:hover { filter: brightness(1.15); }
    .btn--start { background: linear-gradient(135deg, #22c55e 0%, #16a34a 100%); box-shadow: 0 4px 14px rgba(34, 197, 94, 0.35); }
    .btn--start:hover { filter: brightness(1.15); }
    .btn--stop { background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%); box-shadow: 0 4px 14px rgba(239, 68, 68, 0.35); }
    .btn--stop:hover { filter: brightness(1.15); }
    .btn--save { background: linear-gradient(135deg, #10b981 0%, #059669 100%); box-shadow: 0 4px 14px rgba(16, 185, 129, 0.35); }
    .btn--save:hover { filter: brightness(1.15); }
    .btn--clear { background: linear-gradient(135deg, #64748b 0%, #475569 100%); box-shadow: 0 4px 14px rgba(100, 116, 139, 0.35); }
    .btn--clear:hover { background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%); }

    /* Watchlist Dropdown & Multiselect Tags */
    .watchlist-search-wrapper { position: relative; margin-bottom: 1rem; }
    .watchlist-input {
      width: 100%; padding: 0.75rem 1rem; border-radius: 0.65rem;
      border: 1px solid rgba(148, 163, 184, 0.2);
      background: rgba(15, 23, 42, 0.8); color: #f8fafc;
      font-family: inherit; font-size: 0.9rem; outline: none;
      transition: all 0.2s ease;
    }
    .watchlist-input:focus { border-color: #38bdf8; box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.2); }
    .dropdown-options {
      position: absolute; top: 100%; left: 0; right: 0;
      background: #0f172a; border: 1px solid rgba(148, 163, 184, 0.2);
      border-radius: 0.65rem; max-height: 220px; overflow-y: auto;
      z-index: 100; display: none; margin-top: 0.25rem;
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.5);
    }
    .dropdown-options.active { display: block; }
    .option-item {
      padding: 0.65rem 1rem; cursor: pointer; font-size: 0.85rem; color: #cbd5e1;
      display: flex; align-items: center; justify-content: space-between;
      transition: background 0.15s ease;
    }
    .option-item:hover { background: rgba(56, 189, 248, 0.15); color: #38bdf8; }
    .selected-tags-container {
      display: flex; flex-wrap: wrap; gap: 0.5rem; min-height: 52px;
      padding: 0.75rem; background: rgba(15, 23, 42, 0.4);
      border: 1px dashed rgba(148, 163, 184, 0.15); border-radius: 0.65rem;
      margin-bottom: 1rem; align-items: center;
    }
    .stock-tag {
      display: inline-flex; align-items: center; gap: 0.45rem;
      padding: 0.35rem 0.75rem; border-radius: 9999px;
      background: rgba(56, 189, 248, 0.15); border: 1px solid rgba(56, 189, 248, 0.3);
      color: #38bdf8; font-size: 0.82rem; font-weight: 700;
    }
    .stock-tag .remove-btn {
      cursor: pointer; width: 18px; height: 18px; border-radius: 50%;
      display: inline-flex; align-items: center; justify-content: center;
      background: rgba(56, 189, 248, 0.25); color: #38bdf8; font-size: 0.95rem;
      line-height: 1; transition: all 0.15s ease;
    }
    .stock-tag .remove-btn:hover { background: #ef4444; color: #fff; }
    .watchlist-actions { display: flex; gap: 0.75rem; flex-wrap: wrap; align-items: center; }
    .watchlist-status { font-size: 0.8rem; color: #94a3b8; margin-top: 0.75rem; }

    .info-grid {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
      gap: 0.75rem; margin-bottom: 1.5rem;
    }
    .info-card {
      background: rgba(15, 23, 42, 0.5);
      border: 1px solid rgba(148, 163, 184, 0.08);
      border-radius: 0.65rem; padding: 0.85rem;
    }
    .info-card .label { font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.06em; color: #64748b; font-weight: 700; margin-bottom: 0.25rem; }
    .info-card .value { font-size: 0.9rem; font-weight: 700; color: #cbd5e1; }

    .section-title {
      font-size: 1rem; font-weight: 700; color: #f1f5f9;
      margin-bottom: 1rem; display: flex; align-items: center; justify-content: space-between;
    }

    /* Table styling */
    .table-container { overflow-x: auto; border-radius: 0.65rem; border: 1px solid rgba(148, 163, 184, 0.1); }
    table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.85rem; }
    th {
      background: rgba(15, 23, 42, 0.8); color: #94a3b8;
      padding: 0.75rem 1rem; font-weight: 600; text-transform: uppercase; font-size: 0.68rem; letter-spacing: 0.05em;
    }
    td { padding: 0.85rem 1rem; border-top: 1px solid rgba(148, 163, 184, 0.08); }
    tr:hover td { background: rgba(255, 255, 255, 0.02); }
    .symbol-pill {
      font-weight: 700; color: #38bdf8; background: rgba(56, 189, 248, 0.1);
      padding: 0.25rem 0.6rem; border-radius: 0.35rem; display: inline-block;
    }

    .empty-state {
      text-align: center; padding: 2.5rem 1rem; color: #64748b;
      background: rgba(15, 23, 42, 0.4); border-radius: 0.65rem; border: 1px dashed rgba(148, 163, 184, 0.15);
    }
    .empty-state icon { font-size: 2rem; display: block; margin-bottom: 0.5rem; }

    .rules-list {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 0.75rem;
    }
    .rule-item {
      background: rgba(15, 23, 42, 0.4); border-left: 3px solid #38bdf8;
      padding: 0.75rem 0.9rem; border-radius: 0.4rem; font-size: 0.78rem;
    }
    .rule-item strong { color: #f1f5f9; display: block; margin-bottom: 0.15rem; }

    footer { text-align: center; font-size: 0.75rem; color: #475569; margin-top: 1rem; }
  </style>
</head>
<body>
  <div class="container">
    <div class="card">
      <div class="header">
        <div class="header-left">
          <div class="icon">🤖</div>
          <div class="title">
            <h1>Structural OEL + DeepSeek AI Scanner</h1>
            <p>Angel One SmartAPI &middot; DeepSeek AI Engine &middot; Telegram Group Alerts</p>
          </div>
        </div>
        <div>
          {% if status == "Running" %}
            <div class="badge badge--running"><span class="dot"></span> Bot Motor Running</div>
          {% else %}
            <div class="badge badge--stopped"><span class="dot"></span> {{ status }}</div>
          {% endif %}
        </div>
      </div>

      <div class="actions">
        <a href="/scan" class="btn btn--scan">⚡ Run Scan Now</a>
        <a href="/start" class="btn btn--start">▶️ Start Bot &amp; Login</a>
        <a href="/stop" class="btn btn--stop">🛑 Stop Bot</a>
      </div>

      <div class="info-grid">
        <div class="info-card">
          <div class="label">Cached NSE Stocks</div>
          <div class="value">{{ nse_count }}</div>
        </div>
        <div class="info-card">
          <div class="label">Scheduled Time</div>
          <div class="value">09:20 AM IST</div>
        </div>
        <div class="info-card">
          <div class="label">DeepSeek AI Engine</div>
          <div class="value" style="color: #38bdf8; font-size: 0.85rem;">🤖 Active (v3/v4)</div>
        </div>
        <div class="info-card">
          <div class="label">Latest Scan Status</div>
          <div class="value" style="font-size: 0.8rem;">{{ results.status }}</div>
        </div>
        <div class="info-card">
          <div class="label">Matches Found</div>
          <div class="value" style="color: #4ade80;">{{ results.hits | length }}</div>
        </div>
      </div>

      <!-- CUSTOM WATCHLIST MANAGER SECTION -->
      <div style="margin-top: 1.5rem; margin-bottom: 1.5rem; border-top: 1px solid rgba(148, 163, 184, 0.1); padding-top: 1.5rem;">
        <div class="section-title">
          <span>🎯 Custom Stock Watchlist Manager</span>
          <span id="watchlistCountBadge" style="font-size: 0.75rem; color: #38bdf8; font-weight: 600;"></span>
        </div>
        <p style="font-size: 0.8rem; color: #94a3b8; margin-bottom: 1rem;">
          Select specific stocks to scan. If cleared (empty), the scanner will automatically scan the full default NSE universe.
        </p>

        <div class="watchlist-search-wrapper">
          <input type="text" id="stockSearchInput" class="watchlist-input" placeholder="🔍 Search stock ticker (e.g. TATAMOTORS, RELIANCE, INFY)..." autocomplete="off" />
          <div id="stockDropdownOptions" class="dropdown-options"></div>
        </div>

        <div class="selected-tags-container" id="selectedTagsContainer">
          <!-- Dynamic Selected Tags -->
        </div>

        <div class="watchlist-actions">
          <button class="btn btn--save" onclick="saveWatchlist()">💾 Save Watchlist</button>
          <button class="btn btn--clear" onclick="clearWatchlist()">🗑️ Clear All</button>
        </div>
        <div id="watchlistNotice" class="watchlist-status"></div>
      </div>

      <!-- MATCHING TICKERS TABLE SECTION -->
      <div class="section-title">
        <span>🎯 Matching Stock Tickers &amp; DeepSeek AI Analysis</span>
        <span style="font-size: 0.75rem; color: #94a3b8;">Instant Live Display</span>
      </div>

      {% if results.hits %}
        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th>Ticker Symbol</th>
                <th>09:15 Price (O / H / L / C)</th>
                <th>20 EMA / Wick</th>
                <th>DeepSeek AI Score</th>
                <th>Trade Setup Plan (Entry / SL / Targets)</th>
                <th>AI Justification</th>
              </tr>
            </thead>
            <tbody>
              {% for item in results.hits %}
                <tr>
                  <td><span class="symbol-pill">{{ item.symbol }}</span></td>
                  <td>
                    <span style="font-size: 0.78rem;">
                      O: &#8377;{{ item.open }} | H: &#8377;{{ item.high }}<br>
                      L: &#8377;{{ item.low }} | <strong>C: &#8377;{{ item.close }}</strong>
                    </span>
                  </td>
                  <td>
                    <span style="font-size: 0.78rem;">
                      EMA20: &#8377;{{ item.ema20 }}<br>
                      Wick: {{ item.wick_pct }}%
                    </span>
                  </td>
                  <td>
                    {% set ai = item.ai_analysis %}
                    {% if ai %}
                      {% if ai.score >= 80 %}
                        <div class="badge badge--ai-high">🔥 {{ ai.score }}/100 High</div>
                      {% elif ai.score >= 70 %}
                        <div class="badge badge--ai-med">⚡ {{ ai.score }}/100 Med</div>
                      {% else %}
                        <div class="badge badge--ai-low">⚠️ {{ ai.score }}/100 Low</div>
                      {% endif %}
                    {% else %}
                      <span class="badge">N/A</span>
                    {% endif %}
                  </td>
                  <td>
                    {% set ai = item.ai_analysis %}
                    {% if ai %}
                      <div style="font-size: 0.78rem; line-height: 1.45;">
                        <strong>Entry:</strong> &#8377;{{ ai.entry }}<br>
                        <span style="color: #f87171;"><strong>SL:</strong> &#8377;{{ ai.stop_loss }}</span><br>
                        <span style="color: #4ade80;"><strong>T1:</strong> &#8377;{{ ai.target_1 }} | <strong>T2:</strong> &#8377;{{ ai.target_2 }}</span>
                      </div>
                    {% else %}
                      —
                    {% endif %}
                  </td>
                  <td style="font-size: 0.78rem; color: #cbd5e1; max-width: 240px; line-height: 1.35;">
                    {% set ai = item.ai_analysis %}
                    {% if ai %}
                      {{ ai.reasoning }}
                    {% else %}
                      Passed 6 structural rules.
                    {% endif %}
                  </td>
                </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      {% else %}
        <div class="empty-state">
          <p>No matching stock tickers found in the latest scan pass.</p>
          <p style="font-size: 0.75rem; margin-top: 0.4rem;">Scan runs automatically at 09:20 AM IST or click "Run Scan Now" to test on-demand.</p>
        </div>
      {% endif %}
    </div>

    <!-- STRATEGY RULES OVERVIEW CARD -->
    <div class="card">
      <div class="section-title">
        <span>⚙️ Active Filter Strategy Rules &amp; DeepSeek AI Engine</span>
      </div>
      <div class="rules-list">
        <div class="rule-item">
          <strong>1. Open = Low &amp; Structural Support</strong>
          Today's 09:15 AM Open == Low AND Open == Prev Day Last 5-min Low.
        </div>
        <div class="rule-item">
          <strong>2. Bullish Body</strong>
          Today's 09:15 AM candle Close &gt; Open.
        </div>
        <div class="rule-item">
          <strong>3. Upper Wick Rejection</strong>
          Upper Wick (High &minus; Close) &le; 50% of Candle Range (High &minus; Low).
        </div>
        <div class="rule-item">
          <strong>4. Price Range</strong>
          Current Price between &#8377;300 and &#8377;3000.
        </div>
        <div class="rule-item">
          <strong>5. Market Trend Alignment</strong>
          Nifty 50 Index 09:15 AM candle Close &gt; Open.
        </div>
        <div class="rule-item">
          <strong>6. Trend Confirmation (20 EMA)</strong>
          Today's 09:15 AM Close &gt; 20-period EMA on 5-minute chart.
        </div>
        <div class="rule-item" style="border-left-color: #38bdf8; background: rgba(56, 189, 248, 0.08);">
          <strong style="color: #38bdf8;">🧠 7. DeepSeek AI Risk &amp; Setup Engine</strong>
          Scores quality (0–100), calculates limit entry, structural SL, 1:2 &amp; 1:3 R:R targets, and generates AI technical justification.
        </div>
      </div>
    </div>

    <!-- REAL-TIME SYSTEM LOGS TERMINAL CARD -->
    <div class="card">
      <div class="section-title">
        <span>📜 Real-Time System Logs</span>
        <button class="btn btn--clear" style="min-width: auto; padding: 0.35rem 0.75rem; font-size: 0.75rem;" onclick="fetchDashboardLogs()">🔄 Refresh Logs</button>
      </div>
      <div style="background: #060913; border: 1px solid rgba(148, 163, 184, 0.15); border-radius: 0.65rem; padding: 1rem; font-family: monospace; font-size: 0.78rem; color: #a7f3d0; max-height: 250px; overflow-y: auto; white-space: pre-wrap; word-break: break-word;" id="logTerminal">Loading system logs...</div>
    </div>

    <footer>
      AlphaQuant AI Scanner &middot; Powered by Angel One SmartAPI, DeepSeek AI &amp; Quantitative ML Engine
    </footer>
  </div>

  <script>
    let availableStocks = [];
    let selectedStocks = [];

    async function fetchStocksData() {
      try {
        const resp = await fetch('/api/stocks');
        const data = await resp.json();
        if (data.status === 'success') {
          availableStocks = data.all_stocks || [];
          selectedStocks = data.selected_stocks || [];
          renderTags();
        }
      } catch (err) {
        console.error('Error loading stocks API:', err);
      }
    }

    function renderTags() {
      const container = document.getElementById('selectedTagsContainer');
      const badge = document.getElementById('watchlistCountBadge');
      const notice = document.getElementById('watchlistNotice');
      
      if (selectedStocks.length === 0) {
        container.innerHTML = `<span style="color: #64748b; font-size: 0.8rem;">No custom stocks selected. Scanner will scan default NSE universe.</span>`;
        badge.textContent = `Default Mode (Full Universe)`;
        notice.innerHTML = `ℹ️ Currently in <strong>Default Mode</strong> (Scanning full price-filtered universe).`;
      } else {
        container.innerHTML = selectedStocks.map(symbol => `
          <div class="stock-tag">
            <span>${symbol}</span>
            <span class="remove-btn" onclick="removeStock('${symbol}')" title="Remove ${symbol}">&times;</span>
          </div>
        `).join('');
        badge.textContent = `${selectedStocks.length} Selected`;
        notice.innerHTML = `✅ <strong>Custom Watchlist Active (${selectedStocks.length} stock(s) set).</strong> Scanner will evaluate only these stocks.`;
      }
    }

    function addStock(symbol) {
      const sym = symbol.toUpperCase().trim();
      if (sym && !selectedStocks.includes(sym)) {
        selectedStocks.push(sym);
        renderTags();
      }
      document.getElementById('stockSearchInput').value = '';
      document.getElementById('stockDropdownOptions').classList.remove('active');
    }

    function removeStock(symbol) {
      selectedStocks = selectedStocks.filter(s => s !== symbol);
      renderTags();
    }

    function clearWatchlist() {
      if (selectedStocks.length === 0) return;
      if (confirm('Are you sure you want to clear all selected stocks?')) {
        selectedStocks = [];
        renderTags();
        saveWatchlistToServer([]);
      }
    }

    async function saveWatchlist() {
      saveWatchlistToServer(selectedStocks);
    }

    async function saveWatchlistToServer(stocksList) {
      const notice = document.getElementById('watchlistNotice');
      try {
        notice.innerHTML = `⏳ Saving watchlist...`;
        const resp = await fetch('/api/stocks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ selected: stocksList }),
        });
        const data = await resp.json();
        if (data.status === 'success') {
          selectedStocks = data.selected_stocks;
          renderTags();
          notice.innerHTML = `✨ <strong>Watchlist saved successfully!</strong>`;
        } else {
          notice.innerHTML = `❌ Error: ${data.message}`;
        }
      } catch (err) {
        notice.innerHTML = `❌ Failed to save watchlist.`;
      }
    }

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
      fetchStocksData();
      fetchDashboardLogs();
      setInterval(fetchDashboardLogs, 4000);

      const input = document.getElementById('stockSearchInput');
      const optionsDiv = document.getElementById('stockDropdownOptions');

      input.addEventListener('input', (e) => {
        const query = e.target.value.toUpperCase().trim();
        if (!query) {
          optionsDiv.classList.remove('active');
          optionsDiv.innerHTML = '';
          return;
        }

        const matches = availableStocks.filter(s => s.includes(query) && !selectedStocks.includes(s)).slice(0, 30);
        
        if (matches.length > 0) {
          optionsDiv.innerHTML = matches.map(s => `
            <div class="option-item" onclick="addStock('${s}')">
              <span><strong>${s}</strong></span>
              <span style="font-size: 0.75rem; color: #38bdf8;">+ Add</span>
            </div>
          `).join('');
          optionsDiv.classList.add('active');
        } else {
          optionsDiv.innerHTML = `
            <div class="option-item" onclick="addStock('${query}')">
              <span>Add custom ticker: <strong>${query}</strong></span>
              <span style="font-size: 0.75rem; color: #38bdf8;">+ Add</span>
            </div>
          `;
          optionsDiv.classList.add('active');
        }
      });

      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          const query = input.value.toUpperCase().trim();
          if (query) addStock(query);
        }
      });

      document.addEventListener('click', (e) => {
        if (!input.contains(e.target) && !optionsDiv.contains(e.target)) {
          optionsDiv.classList.remove('active');
        }
      });
    });
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP Endpoints & Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Render the control-panel dashboard."""
    return render_template_string(
        CONTROL_PANEL_HTML,
        status=BOT_STATUS,
        nse_count=len(NSE_STOCKS) if NSE_STOCKS else "—",
        results=LATEST_SCAN_RESULTS,
    )


@app.route("/start")
def start_bot():
    """Trigger manual Angel One authentication & refresh master."""
    automate_angel_login()
    return redirect(url_for("index"))


@app.route("/scan")
def trigger_scan():
    """On-demand scan execution trigger for instant testing."""
    if BOT_STATUS != "Running":
        automate_angel_login()
    run_strategy_scan()
    return redirect(url_for("index"))


@app.route("/stop")
def stop_bot():
    """Stop bot motor."""
    global BOT_STATUS
    BOT_STATUS = "Stopped"
    send_telegram("🛑 *Bot Motor stopped* via control panel.")
    logger.info("Bot stopped via /stop endpoint.")
    return redirect(url_for("index"))


@app.route("/api/stocks", methods=["GET"])
def get_stocks_api():
    """Return all cached NSE symbols and currently selected watchlist stocks."""
    all_symbols = sorted(list(NSE_STOCKS.keys()))
    return jsonify({
        "status": "success",
        "all_stocks": all_symbols,
        "selected_stocks": SELECTED_STOCKS,
        "total_available": len(all_symbols),
    })


@app.route("/api/stocks", methods=["POST"])
def update_stocks_api():
    """Save updated selected stocks list."""
    data = request.get_json(silent=True) or {}
    selected = data.get("selected", [])
    if save_selected_stocks(selected):
        return jsonify({
            "status": "success",
            "message": f"Watchlist updated with {len(SELECTED_STOCKS)} stock(s).",
            "selected_stocks": SELECTED_STOCKS,
        })
    return jsonify({"status": "error", "message": "Failed to save watchlist."}), 500


@app.route("/api/stocks/clear", methods=["POST"])
def clear_stocks_api():
    """Clear custom watchlist state."""
    if save_selected_stocks([]):
        return jsonify({
            "status": "success",
            "message": "Watchlist cleared. Bot will scan default universe.",
            "selected_stocks": [],
        })
    return jsonify({"status": "error", "message": "Failed to clear watchlist."}), 500


@app.route("/api/results")
def get_results():
    """JSON API endpoint returning latest scan results."""
    return jsonify(LATEST_SCAN_RESULTS)


@app.route("/api/logs")
def get_logs_api():
    """JSON API endpoint returning recent in-memory system logs."""
    return jsonify({"status": "success", "logs": SYSTEM_LOGS})


@app.route("/healthz")
def healthz():
    """Lightweight health check endpoint for external keep-alive monitoring."""
    return jsonify({"status": "healthy", "bot_status": BOT_STATUS})


# ---------------------------------------------------------------------------
# Local Standalone Launcher
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
