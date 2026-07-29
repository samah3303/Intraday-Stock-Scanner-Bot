"""
AlphaQuant AI — Single Day Backtest (July 29, 2026)
====================================================
Connects to Angel One SmartAPI, fetches REAL 5-minute OHLC candle data
for all 100 stocks, applies strict Rule 1 VectorizedPreFilter + 6-Rule
OEL Strategy, and uses IntrabarExecutionSimulator to eliminate lookahead bias.
Dispatches full results to Telegram.

Run: python backtest_today.py
"""

import os
import sys
import time
import json
import logging
import traceback
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pyotp
import requests
from dotenv import load_dotenv
from SmartApi import SmartConnect

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger("AlphaQuant_Today_Backtest")

# ── Credentials ──
ANGEL_API_KEY = os.getenv("ANGEL_API_KEY")
ANGEL_CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE")
ANGEL_PASSWORD = os.getenv("ANGEL_MPIN") or os.getenv("ANGEL_PASSWORD")
ANGEL_TOTP_KEY = os.getenv("ANGEL_TOTP_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

from shared.constants import DEFAULT_100_STOCKS as STOCK_UNIVERSE, NIFTY_TOKEN, MIN_STOCK_PRICE, MAX_STOCK_PRICE

MIN_PRICE = MIN_STOCK_PRICE
MAX_PRICE = MAX_STOCK_PRICE
OPEN_LOW_TOLERANCE = 0.0005   # 0.05% tolerance for Open=Low
SLIPPAGE_PCT = 0.0005         # 0.05% market order slippage
CAPITAL = 100000.0
RISK_PCT = 0.01               # 1% risk per trade


# ── Telegram Helper ──
def send_telegram(msg: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials missing.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
            if resp.ok:
                return True
        except Exception as exc:
            logger.warning("Telegram attempt %d failed: %s", attempt + 1, exc)
            time.sleep(1.0)
    return False


def send_telegram_long(message: str) -> None:
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


# ── Angel One Login ──
def login_angel() -> SmartConnect:
    logger.info("Authenticating Angel One SmartAPI...")
    totp = pyotp.TOTP(ANGEL_TOTP_KEY).now()

    old_cwd = os.getcwd()
    try:
        tmp_dir = os.path.join(os.path.dirname(__file__), "__tmp_logs")
        os.makedirs(tmp_dir, exist_ok=True)
        os.chdir(tmp_dir)
        obj = SmartConnect(api_key=ANGEL_API_KEY)
    finally:
        os.chdir(old_cwd)

    data = obj.generateSession(ANGEL_CLIENT_CODE, ANGEL_PASSWORD, totp)
    if data.get("status"):
        logger.info("Angel One login SUCCESS for %s", ANGEL_CLIENT_CODE)
        return obj
    else:
        raise RuntimeError(f"Login failed: {data.get('message', 'Unknown error')}")


# ── Instrument Master ──
def fetch_nse_eq_tokens() -> dict:
    logger.info("Downloading Angel One instrument master (NSE EQ only)...")
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    resp = requests.get(url, timeout=90)
    resp.raise_for_status()
    data = resp.json()

    eq_map = {}
    for item in data:
        if item.get("exch_seg") == "NSE" and item.get("symbol", "").endswith("-EQ"):
            sym = item["symbol"].replace("-EQ", "")
            eq_map[sym] = item["token"]

    logger.info("Mapped %d strict NSE Cash Equity tokens.", len(eq_map))
    return eq_map


# ── Candle Data Fetcher ──
def fetch_candles(smart_api: SmartConnect, token: str, days_back: int = 2) -> pd.DataFrame:
    to_date = datetime.now()
    from_date = to_date - timedelta(days=days_back)

    params = {
        "exchange": "NSE",
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
        raise RuntimeError(f"No candle data for token {token}")

    df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Paise scaling fix
    if df["close"].mean() > 50000.0:
        logger.warning("Paise scaling detected for token %s, dividing by 100.", token)
        df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]] / 100.0

    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


# ── Intrabar Execution Simulator (Eliminates Lookahead Bias) ──
def simulate_intrabar(entry: float, sl: float, t1: float, t2: float, df_intraday: pd.DataFrame) -> dict:
    """
    Processes 5-minute bars chronologically from 09:20 onward.
    If SL and Target are touched in the same bar, conservatively treats as SL_HIT.
    """
    if df_intraday.empty:
        return {"status": "NO_DATA", "pnl_r": 0.0, "exit_price": entry, "exit_time": "N/A", "detail": "No intraday data"}

    t1_hit = False
    risk = max(entry - sl, 0.50)

    for _, bar in df_intraday.iterrows():
        b_high = float(bar["high"])
        b_low = float(bar["low"])
        t_str = bar["timestamp"].strftime("%H:%M")

        # CONFLICT BAR: Both SL and Target touched in same bar → Conservative SL
        if b_low <= sl and b_high >= t1:
            return {"status": "SL_HIT", "pnl_r": -1.0, "exit_price": sl, "exit_time": t_str,
                    "detail": f"SL Hit (Conflict Bar) at {t_str}"}

        # Check SL
        if b_low <= sl:
            return {"status": "SL_HIT", "pnl_r": -1.0, "exit_price": sl, "exit_time": t_str,
                    "detail": f"SL Hit at {t_str}"}

        # Check T2 (1:3 R:R)
        if b_high >= t2:
            return {"status": "T2_HIT", "pnl_r": 3.0, "exit_price": t2, "exit_time": t_str,
                    "detail": f"T2 Hit (+3.0R) at {t_str}"}

        # Check T1 (1:2 R:R) — trail SL to breakeven
        if b_high >= t1 and not t1_hit:
            t1_hit = True
            sl = entry  # Trail SL to cost

    # EOD Square-off
    eod_close = float(df_intraday.iloc[-1]["close"])
    eod_time = df_intraday.iloc[-1]["timestamp"].strftime("%H:%M")
    pnl_r = round((eod_close - entry) / risk, 2)

    if t1_hit:
        return {"status": "T1_PARTIAL_EOD", "pnl_r": max(pnl_r, 0.0), "exit_price": eod_close,
                "exit_time": eod_time, "detail": f"T1 Hit + EOD Close at {eod_time} ({pnl_r:+.2f}R)"}
    else:
        return {"status": "EOD_CLOSE", "pnl_r": pnl_r, "exit_price": eod_close,
                "exit_time": eod_time, "detail": f"EOD Close at {eod_time} ({pnl_r:+.2f}R)"}


# ══════════════════════════════════════════════════════════════════
# MAIN BACKTEST ENGINE
# ══════════════════════════════════════════════════════════════════
def run_today_backtest():
    today = datetime.now().date()
    today_str = today.strftime("%d %b %Y")
    logger.info("=" * 60)
    logger.info("ALPHAQUANT AI — SINGLE DAY BACKTEST: %s", today_str)
    logger.info("=" * 60)

    # 1. Login
    try:
        smart_api = login_angel()
    except Exception as exc:
        logger.error("Login failed: %s", exc)
        send_telegram(f"BACKTEST ABORTED: Login failed\n`{exc}`")
        return

    # 2. Fetch instrument master
    try:
        nse_tokens = fetch_nse_eq_tokens()
    except Exception as exc:
        logger.error("Instrument master failed: %s", exc)
        send_telegram(f"BACKTEST ABORTED: Instrument master error\n`{exc}`")
        return

    # 3. Fetch Nifty 50 benchmark
    try:
        nifty_df = fetch_candles(smart_api, NIFTY_TOKEN, days_back=2)
        nifty_today = nifty_df[nifty_df["timestamp"].dt.date == today]
        if nifty_today.empty:
            send_telegram(f"BACKTEST ABORTED: No Nifty data for {today_str}")
            return
        nifty_first = nifty_today.iloc[0]
        nifty_bullish = float(nifty_first["close"]) > float(nifty_first["open"])
        nifty_status = "BULLISH" if nifty_bullish else "BEARISH"
        logger.info("Nifty 50 09:15 AM: Open=%.2f Close=%.2f -> %s",
                     float(nifty_first["open"]), float(nifty_first["close"]), nifty_status)
    except Exception as exc:
        logger.error("Nifty fetch failed: %s", exc)
        send_telegram(f"BACKTEST ABORTED: Nifty error\n`{exc}`")
        return

    send_telegram(
        f"*ALPHAQUANT AI - SINGLE DAY BACKTEST*\n"
        f"Date: *{today_str}*\n"
        f"Stocks: *{len(STOCK_UNIVERSE)}*\n"
        f"Nifty 50: *{nifty_status}* (O:{nifty_first['open']:.2f} C:{nifty_first['close']:.2f})\n"
        f"Engine: *Intrabar Simulator (No Lookahead Bias)*\n"
        f"_Scanning..._"
    )

    # 4. Scan all 100 stocks
    trades = []
    skipped = 0
    errors = 0
    filtered_price = 0

    for idx, symbol in enumerate(STOCK_UNIVERSE, 1):
        token = nse_tokens.get(symbol)
        if not token:
            logger.warning("Token not found for %s, skipping.", symbol)
            skipped += 1
            continue

        try:
            df = fetch_candles(smart_api, token, days_back=2)
        except Exception as exc:
            logger.debug("Candle fetch error for %s: %s", symbol, exc)
            errors += 1
            time.sleep(0.15)
            continue

        # Today's candles
        today_candles = df[df["timestamp"].dt.date == today]
        if today_candles.empty:
            skipped += 1
            time.sleep(0.15)
            continue

        first_candle = today_candles.iloc[0]
        c_open = float(first_candle["open"])
        c_high = float(first_candle["high"])
        c_low = float(first_candle["low"])
        c_close = float(first_candle["close"])

        # ── RULE 1: Price Filter (₹300 – ₹3000) ──
        if not (MIN_PRICE <= c_open <= MAX_PRICE):
            filtered_price += 1
            time.sleep(0.15)
            continue

        # ── RULE 2: Bullish Open (Gap-Up or Open > Prev Close) ──
        prev_candles = df[df["timestamp"].dt.date < today]
        if prev_candles.empty:
            skipped += 1
            time.sleep(0.15)
            continue
        prev_close = float(prev_candles.iloc[-1]["close"])
        if c_open <= prev_close:
            time.sleep(0.15)
            continue

        # ── RULE 3: Open = Low (0.05% tolerance) ──
        if c_open > 0 and ((c_open - c_low) / c_open) > OPEN_LOW_TOLERANCE:
            time.sleep(0.15)
            continue

        # ── RULE 4: Bullish Candle Body (Close > Open) ──
        if c_close <= c_open:
            time.sleep(0.15)
            continue

        # ── RULE 5: Upper Wick <= 50% of Range ──
        candle_range = c_high - c_low
        if candle_range <= 0:
            time.sleep(0.15)
            continue
        upper_wick = c_high - c_close
        if upper_wick > 0.50 * candle_range:
            time.sleep(0.15)
            continue

        # ── RULE 6: Nifty Bullish Check ──
        if not nifty_bullish:
            time.sleep(0.15)
            continue

        # ── PASSED ALL 6 RULES ──
        # Calculate Entry, SL, T1, T2 with slippage
        entry = round(c_close * (1 + SLIPPAGE_PCT), 2)
        sl = round(c_low * 0.997, 2)
        risk = max(entry - sl, 0.50)
        t1 = round(entry + (risk * 2.0), 2)
        t2 = round(entry + (risk * 3.0), 2)

        # Position sizing (1% risk)
        shares = int(CAPITAL * RISK_PCT / risk)
        pos_value = round(shares * entry, 2)

        # Intraday bars after first candle
        intraday_bars = today_candles.iloc[1:]
        outcome = simulate_intrabar(entry, sl, t1, t2, intraday_bars)

        trades.append({
            "symbol": symbol,
            "token": token,
            "open": c_open,
            "high": c_high,
            "low": c_low,
            "close": c_close,
            "prev_close": prev_close,
            "entry": entry,
            "sl": sl,
            "t1": t1,
            "t2": t2,
            "risk": risk,
            "shares": shares,
            "pos_value": pos_value,
            "outcome": outcome
        })

        logger.info("TRADE #%d: %s | Entry:%.2f SL:%.2f T1:%.2f T2:%.2f | %s (%s)",
                     len(trades), symbol, entry, sl, t1, t2,
                     outcome["status"], outcome["detail"])

        time.sleep(0.15)

        if idx % 20 == 0:
            logger.info("Progress: %d/%d stocks scanned...", idx, len(STOCK_UNIVERSE))

    # ── Results ──
    total_trades = len(trades)
    wins = sum(1 for t in trades if t["outcome"]["pnl_r"] > 0)
    losses = sum(1 for t in trades if t["outcome"]["pnl_r"] < 0)
    breakeven = sum(1 for t in trades if t["outcome"]["pnl_r"] == 0)
    total_r = sum(t["outcome"]["pnl_r"] for t in trades)
    win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
    profit_rupees = total_r * (CAPITAL * RISK_PCT)

    logger.info("=" * 60)
    logger.info("BACKTEST COMPLETE: %d trades | Win Rate: %.1f%% | Net: %+.2f R", total_trades, win_rate, total_r)
    logger.info("=" * 60)

    # ── Telegram Trade Details ──
    if trades:
        trade_lines = []
        for i, t in enumerate(trades, 1):
            out = t["outcome"]
            status_emoji = {"SL_HIT": "X", "T1_HIT": "T1", "T1_PARTIAL_EOD": "T1+EOD",
                            "T2_HIT": "T2", "EOD_CLOSE": "EOD"}.get(out["status"], out["status"])

            line = (
                f"*{i}. {t['symbol']}*\n"
                f"   Entry: Rs.{t['entry']} (09:20 AM) | SL: Rs.{t['sl']}\n"
                f"   T1: Rs.{t['t1']} | T2: Rs.{t['t2']}\n"
                f"   Shares: {t['shares']} (Rs.{t['pos_value']:,.0f})\n"
                f"   Exit: *{out['detail']}* ({out['exit_time']})\n"
                f"   Result: *{out['pnl_r']:+.2f} R*"
            )
            trade_lines.append(line)

        trades_msg = (
            f"*ALPHAQUANT AI - BACKTEST TRADES ({today_str})*\n"
            f"{'=' * 40}\n\n"
            + "\n\n".join(trade_lines)
        )
        send_telegram_long(trades_msg)
        time.sleep(1.0)

    # ── Telegram Summary ──
    summary_msg = (
        f"*ALPHAQUANT AI - BACKTEST SUMMARY*\n"
        f"{'=' * 40}\n"
        f"Date: *{today_str}*\n"
        f"Nifty 50: *{nifty_status}*\n"
        f"{'=' * 40}\n"
        f"Stocks Scanned: *{len(STOCK_UNIVERSE)}*\n"
        f"Filtered (Price): *{filtered_price}*\n"
        f"Token Errors: *{errors}*\n"
        f"Skipped: *{skipped}*\n"
        f"{'=' * 40}\n"
        f"Trades Executed: *{total_trades}*\n"
        f"Wins: *{wins}*\n"
        f"Losses: *{losses}*\n"
        f"Breakeven/EOD: *{breakeven}*\n"
        f"Win Rate: *{win_rate:.1f}%*\n"
        f"Net R:R: *{total_r:+.2f} R*\n"
        f"{'=' * 40}\n"
        f"Capital: Rs.{CAPITAL:,.0f} (1% risk/trade)\n"
        f"Est. P/L: *Rs.{profit_rupees:+,.2f}*\n"
        f"{'=' * 40}\n"
        f"_Engine: Intrabar Simulator (No Lookahead Bias)_\n"
        f"_Slippage: {SLIPPAGE_PCT*100:.2f}% applied to entry_"
    )

    if total_trades == 0:
        summary_msg += (
            f"\n\n_No stocks passed all 6 rules today._\n"
            f"_This is normal - the strategy is highly selective._"
        )

    send_telegram(summary_msg)
    logger.info("Backtest results sent to Telegram.")


if __name__ == "__main__":
    run_today_backtest()
