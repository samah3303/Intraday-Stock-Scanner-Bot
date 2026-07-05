"""
Angel One Intraday Scanner – Flask Application
================================================
Two independent strategy scanners with Telegram push alerts:
  • Strategy A — Structural OEL (6-rule filter on curated stocks)
  • Strategy B — OEL First Candle Breakout (broad NSE scan, 1:1.43 R:R)
"""

import os
import time
import logging
import traceback
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()  # Load .env file for local development

import pandas as pd
import pyotp
import requests
from flask import Flask, jsonify, render_template_string
from SmartApi import SmartConnect
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------
app = Flask(__name__)

# ---------------------------------------------------------------------------
# Global State
# ---------------------------------------------------------------------------
BOT_STATUS = "Stopped"
smart_api = None  # SmartConnect session object
NSE_STOCKS = {}   # {symbol: token} — populated from instrument master

# ---------------------------------------------------------------------------
# Stock Universe & Constants
# ---------------------------------------------------------------------------
# Strategy A — curated stock list
STOCK_TOKENS = {
    "INFY": "1594",
    "RELIANCE": "2885",
    "SBIN": "3045",
}
NIFTY_TOKEN = "99926000"

# Strategy B — OEL Breakout constants
OEL_RR_RATIO = 1.43
OEL_MAX_SCAN = 1500  # Max stocks to scan per run (safety cap)

# ---------------------------------------------------------------------------
# Telegram Helper
# ---------------------------------------------------------------------------

def send_telegram(message: str) -> None:
    """Send a markdown-formatted message via Telegram Bot API."""
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
    """Send a long message, splitting into chunks if it exceeds Telegram's 4096 char limit."""
    if len(message) <= 4096:
        send_telegram(message)
        return

    # Split on newlines, keeping chunks under 4000 chars
    lines = message.split("\n")
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 4000:
            send_telegram(chunk)
            chunk = line + "\n"
            time.sleep(0.5)  # Brief pause between chunks
        else:
            chunk += line + "\n"
    if chunk.strip():
        send_telegram(chunk)


# ---------------------------------------------------------------------------
# Instrument Master (NSE Stock Universe for Strategy B)
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
            f"_{len(NSE_STOCKS)} NSE stocks ready for OEL Breakout scan._"
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
    password = os.getenv("ANGEL_PASSWORD")
    totp_key = os.getenv("ANGEL_TOTP_KEY")

    if not all([api_key, client_code, password, totp_key]):
        BOT_STATUS = "Authentication Error"
        err = "One or more ANGEL_* environment variables are missing."
        logger.error(err)
        send_telegram(f"🔴 *Auth Error*\n`{err}`")
        return

    try:
        totp = pyotp.TOTP(totp_key).now()
        obj = SmartConnect(api_key=api_key)
        data = obj.generateSession(client_code, password, totp)

        if data.get("status"):
            smart_api = obj
            BOT_STATUS = "Running"
            logger.info("Angel One login successful for %s", client_code)
            send_telegram("🟢 *Angel One Login Successful* – Bot is now *Running*.")

            # Load NSE stock universe for Strategy B
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
    """
    Fetch 5-minute candle data for *today* and the previous trading day
    and return a clean DataFrame.
    """
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
#  STRATEGY A — Structural OEL (6 Rules)
# ===========================================================================

def evaluate_stock(stock_name: str, token: str,
                   nifty_df: pd.DataFrame) -> str | None:
    """
    Evaluate a single stock against the six filter rules.
    Returns a formatted result string if all rules pass, else None.
    """
    try:
        df = fetch_candles(token)
    except Exception as exc:
        logger.warning("Skipping %s – %s", stock_name, exc)
        return None

    today = datetime.now().date()

    # ── Isolate today's 09:15 candle ──────────────────────────────────
    today_candles = df[df["timestamp"].dt.date == today]
    if today_candles.empty:
        logger.info("%s: No today candles found.", stock_name)
        return None

    candle = today_candles.iloc[0]  # First 5-min candle → 09:15
    c_open, c_high, c_low, c_close = (
        candle["open"], candle["high"], candle["low"], candle["close"],
    )

    # ── Previous day's last candle ────────────────────────────────────
    prev_candles = df[df["timestamp"].dt.date < today]
    if prev_candles.empty:
        logger.info("%s: No previous-day candles.", stock_name)
        return None
    prev_last = prev_candles.iloc[-1]

    # ── Nifty 09:15 candle ────────────────────────────────────────────
    nifty_today = nifty_df[nifty_df["timestamp"].dt.date == today]
    if nifty_today.empty:
        return None
    nifty_candle = nifty_today.iloc[0]

    # ── 20-EMA ────────────────────────────────────────────────────────
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    ema_row = df[df["timestamp"] == candle["timestamp"]]
    if ema_row.empty:
        return None
    ema_value = ema_row.iloc[0]["ema20"]

    # ── RULE 1: Open == Low & structural support ─────────────────────
    if c_open != c_low:
        return None
    if c_open != prev_last["low"]:
        return None

    # ── RULE 2: Bullish candle body ──────────────────────────────────
    if c_close <= c_open:
        return None

    # ── RULE 3: Upper-wick filter (≤ 50 % of range) ─────────────────
    candle_range = c_high - c_low
    if candle_range == 0:
        return None
    upper_wick = c_high - c_close
    if upper_wick > 0.50 * candle_range:
        return None

    # ── RULE 4: Price range 300 – 3000 ───────────────────────────────
    if not (300 <= c_close <= 3000):
        return None

    # ── RULE 5: Market-trend alignment (Nifty bullish) ───────────────
    if nifty_candle["close"] <= nifty_candle["open"]:
        return None

    # ── RULE 6: Close above 20-EMA ───────────────────────────────────
    if c_close <= ema_value:
        return None

    return (
        f"• *{stock_name}* | "
        f"O:{c_open} H:{c_high} L:{c_low} C:{c_close} | "
        f"EMA20:{ema_value:.2f}"
    )


def run_strategy_scan() -> None:
    """Run Strategy A — Structural OEL scan across curated stocks."""
    global BOT_STATUS

    if BOT_STATUS != "Running":
        logger.info("Strategy A scan skipped – bot status is '%s'.", BOT_STATUS)
        return

    logger.info("── Strategy A scan started ──")
    send_telegram("🔍 *Strategy A — Structural OEL scan started…*")

    try:
        nifty_df = fetch_candles(NIFTY_TOKEN, exchange="NSE", days_back=2)
    except Exception as exc:
        logger.error("Cannot fetch Nifty data: %s", exc)
        send_telegram(f"🔴 *Nifty data error (Strategy A)*\n`{exc}`")
        return

    results: list[str] = []
    for name, token in STOCK_TOKENS.items():
        hit = evaluate_stock(name, token, nifty_df)
        if hit:
            results.append(hit)

    if results:
        body = "\n".join(results)
        msg = (
            f"🔍 *Strategy A — Structural OEL*\n"
            f"📅 *{datetime.now():%d %b %Y}*\n\n"
            f"{body}\n\n"
            f"_6-Rule Filter: Open=Low+Support | Bullish | "
            f"Wick≤50% | ₹300-3K | Nifty↑ | >20EMA_"
        )
    else:
        msg = "🔍 *Strategy A* — No matching tickers met filters today."

    send_telegram(msg)
    logger.info("── Strategy A scan complete (%d hits) ──", len(results))


# ===========================================================================
#  STRATEGY B — OEL First Candle Breakout (Broad NSE Scan)
# ===========================================================================

def evaluate_oel_stock(stock_name: str, token: str,
                       nifty_candle: pd.Series) -> str | None:
    """
    Evaluate a single stock for OEL First Candle Breakout.
    Returns a formatted string with Entry/SL/TGT if criteria pass, else None.
    """
    try:
        df = fetch_candles(token, days_back=2)
    except Exception:
        return None

    today = datetime.now().date()

    # ── Today's first 5-min candle (09:15) ────────────────────────────
    today_candles = df[df["timestamp"].dt.date == today]
    if today_candles.empty:
        return None

    candle = today_candles.iloc[0]
    c_open  = candle["open"]
    c_high  = candle["high"]
    c_low   = candle["low"]
    c_close = candle["close"]

    # ── RULE 1: Open = Low (0.1% tolerance for tick rounding) ────────
    if c_low > 0 and abs(c_open - c_low) / c_low > 0.001:
        return None

    # ── RULE 2: Bullish candle (Close > Open) ────────────────────────
    if c_close <= c_open:
        return None

    # ── RULE 3: Meaningful body (≥ 0.3% move to filter noise) ────────
    if c_open > 0 and (c_close - c_open) / c_open < 0.003:
        return None

    # ── RULE 4: Upper wick filter (≤ 60% of candle range) ────────────
    candle_range = c_high - c_low
    if candle_range == 0:
        return None
    upper_wick = c_high - c_close
    if upper_wick > 0.60 * candle_range:
        return None

    # ── RULE 5: Price range ₹50 – ₹5,000 ────────────────────────────
    if not (50 <= c_close <= 5000):
        return None

    # ── RULE 6: Nifty bullish (market-trend alignment) ───────────────
    if nifty_candle["close"] <= nifty_candle["open"]:
        return None

    # ── Compute SL / Target ──────────────────────────────────────────
    entry = c_close
    sl = c_low       # = Open (since Open ≈ Low)
    risk = entry - sl
    if risk <= 0:
        return None

    target = entry + (OEL_RR_RATIO * risk)
    sl_pct = (risk / entry) * 100

    return (
        f"• *{stock_name}* | "
        f"Entry: {entry:.2f} | "
        f"SL: {sl:.2f} (−{sl_pct:.1f}%) | "
        f"TGT: {target:.2f} | "
        f"R:R 1:{OEL_RR_RATIO}"
    )


def run_oel_scan() -> None:
    """Run Strategy B — OEL Breakout scan across NSE universe."""
    global BOT_STATUS

    if BOT_STATUS != "Running":
        logger.info("OEL scan skipped – bot status is '%s'.", BOT_STATUS)
        return

    if not NSE_STOCKS:
        logger.warning("OEL scan skipped – instrument master not loaded.")
        send_telegram("⚠️ *Strategy B skipped* — NSE stock list not loaded.")
        return

    scan_count = min(len(NSE_STOCKS), OEL_MAX_SCAN)
    logger.info("── Strategy B (OEL Breakout) scan started (%d stocks) ──", scan_count)
    send_telegram(
        f"📊 *Strategy B — OEL Breakout scan started…*\n"
        f"_Scanning {scan_count} NSE stocks_"
    )

    # ── Fetch Nifty data first ────────────────────────────────────────
    try:
        nifty_df = fetch_candles(NIFTY_TOKEN, exchange="NSE", days_back=2)
    except Exception as exc:
        logger.error("Cannot fetch Nifty data for OEL scan: %s", exc)
        send_telegram(f"🔴 *Nifty data error (Strategy B)*\n`{exc}`")
        return

    today = datetime.now().date()
    nifty_today = nifty_df[nifty_df["timestamp"].dt.date == today]
    if nifty_today.empty:
        send_telegram("📊 *Strategy B* — No Nifty data for today yet.")
        return

    nifty_candle = nifty_today.iloc[0]

    # Early exit: if Nifty is bearish, no OEL setups
    if nifty_candle["close"] <= nifty_candle["open"]:
        send_telegram("📊 *Strategy B* — Nifty bearish on first candle, no OEL setups today.")
        logger.info("OEL scan skipped — Nifty bearish.")
        return

    # ── Scan all NSE stocks ───────────────────────────────────────────
    results: list[str] = []
    scanned = 0
    errors = 0

    stock_items = list(NSE_STOCKS.items())[:OEL_MAX_SCAN]

    for name, token in stock_items:
        # Skip stocks already covered by Strategy A
        if name in STOCK_TOKENS:
            scanned += 1
            continue

        hit = evaluate_oel_stock(name, token, nifty_candle)
        if hit:
            results.append(hit)

        scanned += 1
        if scanned % 100 == 0:
            logger.info(
                "OEL scan progress: %d/%d scanned, %d hits so far",
                scanned, scan_count, len(results),
            )

        # Rate limiting — ~7 calls/sec to stay within API limits
        time.sleep(0.15)

    # ── Send results ──────────────────────────────────────────────────
    if results:
        body = "\n".join(results)
        msg = (
            f"📊 *Strategy B — OEL Breakout*\n"
            f"📅 *{datetime.now():%d %b %Y}*\n"
            f"_Open=Low First Candle Breakout | R:R 1:{OEL_RR_RATIO}_\n\n"
            f"{body}\n\n"
            f"✅ *{len(results)} stocks* passed filters "
            f"(scanned {scanned})"
        )
    else:
        msg = (
            f"📊 *Strategy B — OEL Breakout*\n"
            f"No setups found today (scanned {scanned} stocks)."
        )

    send_telegram_long(msg)
    logger.info(
        "── Strategy B scan complete (%d hits, %d scanned, %d errors) ──",
        len(results), scanned, errors,
    )


# ---------------------------------------------------------------------------
# APScheduler Setup
# ---------------------------------------------------------------------------
scheduler = BackgroundScheduler(timezone="Asia/Kolkata")

# Login every weekday at 08:45 IST
scheduler.add_job(
    automate_angel_login,
    CronTrigger(day_of_week="mon-fri", hour=8, minute=45,
                timezone="Asia/Kolkata"),
    id="daily_login",
    replace_existing=True,
    misfire_grace_time=300,
)

# Strategy A — Structural OEL scan every weekday at 09:18 IST
scheduler.add_job(
    run_strategy_scan,
    CronTrigger(day_of_week="mon-fri", hour=9, minute=18,
                timezone="Asia/Kolkata"),
    id="strategy_a_scan",
    replace_existing=True,
    misfire_grace_time=300,
)

# Strategy B — OEL Breakout scan every weekday at 09:22 IST
scheduler.add_job(
    run_oel_scan,
    CronTrigger(day_of_week="mon-fri", hour=9, minute=22,
                timezone="Asia/Kolkata"),
    id="strategy_b_oel_scan",
    replace_existing=True,
    misfire_grace_time=300,
)

scheduler.start()
logger.info("APScheduler started – jobs: %s",
            [j.id for j in scheduler.get_jobs()])

# ---------------------------------------------------------------------------
# Control-Panel HTML (dark theme, embedded)
# ---------------------------------------------------------------------------
CONTROL_PANEL_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta name="description" content="Angel One Intraday Scanner – dual-strategy bot control panel." />
  <title>Intraday Scanner – Control Panel</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet" />
  <style>
    /* ── Reset & Base ───────────────────────────────────────────────── */
    *, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }
    html { font-size: 16px; }
    body {
      font-family: 'Inter', system-ui, -apple-system, sans-serif;
      background: #0f172a;
      color: #e2e8f0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      overflow: hidden;
    }

    /* ── Animated gradient background ───────────────────────────────── */
    body::before {
      content: '';
      position: fixed;
      inset: 0;
      background:
        radial-gradient(ellipse 80% 60% at 20% 30%, rgba(56,189,248,.08) 0%, transparent 60%),
        radial-gradient(ellipse 60% 50% at 80% 70%, rgba(139,92,246,.08) 0%, transparent 60%);
      animation: pulse 8s ease-in-out infinite alternate;
      z-index: 0;
    }
    @keyframes pulse {
      0%   { opacity: .6; transform: scale(1); }
      100% { opacity: 1;  transform: scale(1.05); }
    }

    /* ── Card ───────────────────────────────────────────────────────── */
    .card {
      position: relative;
      z-index: 1;
      width: 100%;
      max-width: 560px;
      margin: 1.5rem;
      padding: 2.5rem 2rem 2rem;
      background: rgba(30,41,59,.72);
      backdrop-filter: blur(20px) saturate(1.4);
      -webkit-backdrop-filter: blur(20px) saturate(1.4);
      border: 1px solid rgba(148,163,184,.12);
      border-radius: 1.25rem;
      box-shadow:
        0 0 0 1px rgba(255,255,255,.03),
        0 24px 48px -12px rgba(0,0,0,.55);
    }

    /* ── Header ─────────────────────────────────────────────────────── */
    .header { text-align: center; margin-bottom: 2rem; }
    .header .icon {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 56px; height: 56px;
      border-radius: 16px;
      background: linear-gradient(135deg, #38bdf8 0%, #818cf8 100%);
      margin-bottom: 1rem;
      font-size: 1.6rem;
      box-shadow: 0 8px 24px -4px rgba(56,189,248,.35);
    }
    .header h1 {
      font-size: 1.55rem;
      font-weight: 800;
      letter-spacing: -.02em;
      background: linear-gradient(135deg, #f1f5f9 30%, #94a3b8 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }
    .header p {
      margin-top: .4rem;
      font-size: .85rem;
      color: #94a3b8;
    }

    /* ── Status Badge ───────────────────────────────────────────────── */
    .status-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 1rem 1.25rem;
      background: rgba(15,23,42,.55);
      border: 1px solid rgba(148,163,184,.08);
      border-radius: .75rem;
      margin-bottom: 1.75rem;
    }
    .status-label {
      font-size: .78rem;
      text-transform: uppercase;
      letter-spacing: .08em;
      font-weight: 600;
      color: #64748b;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: .45rem;
      padding: .35rem .9rem;
      border-radius: 9999px;
      font-size: .78rem;
      font-weight: 600;
      letter-spacing: .02em;
    }
    .badge .dot {
      width: 8px; height: 8px;
      border-radius: 50%;
      display: inline-block;
    }
    /* Conditional badge colors */
    .badge--running  { background: rgba(34,197,94,.12); color: #4ade80; }
    .badge--running .dot  { background: #22c55e; box-shadow: 0 0 6px #22c55e; }
    .badge--stopped  { background: rgba(239,68,68,.12); color: #f87171; }
    .badge--stopped .dot  { background: #ef4444; box-shadow: 0 0 6px #ef4444; }
    .badge--error    { background: rgba(251,146,60,.12); color: #fb923c; }
    .badge--error .dot    { background: #f97316; box-shadow: 0 0 6px #f97316; }

    /* ── Strategy Tags ─────────────────────────────────────────────── */
    .strategies {
      display: flex;
      gap: .6rem;
      margin-bottom: 1.75rem;
    }
    .strat-tag {
      flex: 1;
      padding: .75rem .9rem;
      background: rgba(15,23,42,.45);
      border: 1px solid rgba(148,163,184,.07);
      border-radius: .6rem;
      text-align: center;
    }
    .strat-tag .tag-label {
      font-size: .62rem;
      text-transform: uppercase;
      letter-spacing: .08em;
      font-weight: 600;
      color: #64748b;
      margin-bottom: .3rem;
    }
    .strat-tag .tag-name {
      font-size: .78rem;
      font-weight: 700;
      color: #cbd5e1;
    }
    .strat-tag .tag-detail {
      font-size: .65rem;
      color: #64748b;
      margin-top: .15rem;
    }
    .strat-tag--a { border-left: 3px solid #38bdf8; }
    .strat-tag--b { border-left: 3px solid #a78bfa; }

    /* ── Buttons ────────────────────────────────────────────────────── */
    .actions { display: flex; gap: .75rem; }
    .btn {
      flex: 1;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: .5rem;
      padding: .8rem 1rem;
      border: none;
      border-radius: .65rem;
      font-family: inherit;
      font-size: .85rem;
      font-weight: 600;
      cursor: pointer;
      transition: transform .15s, box-shadow .25s, filter .25s;
      text-decoration: none;
      color: #fff;
    }
    .btn:active { transform: scale(.97); }
    .btn--start {
      background: linear-gradient(135deg, #22c55e 0%, #16a34a 100%);
      box-shadow: 0 4px 14px -3px rgba(34,197,94,.45);
    }
    .btn--start:hover { filter: brightness(1.1); box-shadow: 0 6px 20px -3px rgba(34,197,94,.55); }
    .btn--stop {
      background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
      box-shadow: 0 4px 14px -3px rgba(239,68,68,.40);
    }
    .btn--stop:hover { filter: brightness(1.1); box-shadow: 0 6px 20px -3px rgba(239,68,68,.50); }

    /* ── Info Grid ──────────────────────────────────────────────────── */
    .info-grid {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: .6rem;
      margin-top: 1.75rem;
    }
    .info-card {
      padding: .85rem 1rem;
      background: rgba(15,23,42,.45);
      border: 1px solid rgba(148,163,184,.07);
      border-radius: .6rem;
    }
    .info-card .label {
      font-size: .62rem;
      text-transform: uppercase;
      letter-spacing: .07em;
      color: #64748b;
      margin-bottom: .25rem;
      font-weight: 600;
    }
    .info-card .value {
      font-size: .82rem;
      font-weight: 600;
      color: #cbd5e1;
    }

    /* ── Footer ─────────────────────────────────────────────────────── */
    .footer {
      text-align: center;
      margin-top: 1.75rem;
      font-size: .72rem;
      color: #475569;
    }
    .footer a { color: #64748b; text-decoration: none; }
    .footer a:hover { color: #94a3b8; }

    /* ── Responsive ─────────────────────────────────────────────────── */
    @media (max-width: 480px) {
      .card { padding: 2rem 1.25rem 1.5rem; }
      .actions { flex-direction: column; }
      .strategies { flex-direction: column; }
      .info-grid { grid-template-columns: 1fr 1fr; }
    }
  </style>
</head>
<body>
  <div class="card">
    <!-- Header -->
    <div class="header">
      <div class="icon">📡</div>
      <h1>Intraday Scanner</h1>
      <p>Angel One &middot; Dual-Strategy Bot</p>
    </div>

    <!-- Status -->
    <div class="status-row">
      <span class="status-label">Bot Engine</span>
      {% if status == "Running" %}
        <span class="badge badge--running"><span class="dot"></span>Running</span>
      {% elif "Error" in status %}
        <span class="badge badge--error"><span class="dot"></span>{{ status }}</span>
      {% else %}
        <span class="badge badge--stopped"><span class="dot"></span>Stopped</span>
      {% endif %}
    </div>

    <!-- Strategy Tags -->
    <div class="strategies">
      <div class="strat-tag strat-tag--a">
        <div class="tag-label">Strategy A</div>
        <div class="tag-name">Structural OEL</div>
        <div class="tag-detail">6-Rule Filter &middot; 09:18 IST</div>
      </div>
      <div class="strat-tag strat-tag--b">
        <div class="tag-label">Strategy B</div>
        <div class="tag-name">OEL Breakout</div>
        <div class="tag-detail">1:1.43 R:R &middot; 09:22 IST</div>
      </div>
    </div>

    <!-- Actions -->
    <div class="actions">
      <a href="/start" class="btn btn--start" id="btn-start">⚡ Force Run &amp; Login</a>
      <a href="/stop"  class="btn btn--stop"  id="btn-stop">■ Stop Bot Motor</a>
    </div>

    <!-- Info Grid -->
    <div class="info-grid">
      <div class="info-card">
        <div class="label">Login</div>
        <div class="value">08:45 IST</div>
      </div>
      <div class="info-card">
        <div class="label">Scan A</div>
        <div class="value">09:18 IST</div>
      </div>
      <div class="info-card">
        <div class="label">Scan B</div>
        <div class="value">09:22 IST</div>
      </div>
      <div class="info-card">
        <div class="label">Stocks (A)</div>
        <div class="value">{{ stock_count_a }}</div>
      </div>
      <div class="info-card">
        <div class="label">NSE Universe</div>
        <div class="value">{{ nse_count }}</div>
      </div>
      <div class="info-card">
        <div class="label">Server Time</div>
        <div class="value">{{ server_time }}</div>
      </div>
    </div>

    <!-- Footer -->
    <div class="footer">
      <a href="/healthz">Health Check</a> &middot; Powered by SmartAPI + Flask
    </div>
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Render the control-panel dashboard."""
    return render_template_string(
        CONTROL_PANEL_HTML,
        status=BOT_STATUS,
        stock_count_a=len(STOCK_TOKENS),
        nse_count=len(NSE_STOCKS) if NSE_STOCKS else "—",
        server_time=datetime.now().strftime("%H:%M:%S IST"),
    )


@app.route("/start")
def start_bot():
    """On-demand login trigger + redirect to dashboard."""
    automate_angel_login()
    return index()


@app.route("/stop")
def stop_bot():
    """Flip bot status to Stopped and alert Telegram."""
    global BOT_STATUS
    BOT_STATUS = "Stopped"
    send_telegram("🛑 *Bot Motor stopped* by user via control panel.")
    logger.info("Bot stopped via /stop endpoint.")
    return index()


@app.route("/healthz")
def healthz():
    """Lightweight health-check for external ping services."""
    return jsonify({"status": "healthy"})
