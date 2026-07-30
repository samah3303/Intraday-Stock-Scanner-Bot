"""
AlphaQuant AI — 10-Day Institutional Backtest Engine
=====================================================
Evaluates the complete Pre-Market Screening + 6-Rule Breakout strategy 
with Intrabar Execution Simulator over the last 10 trading days.

Features:
1. Pre-Market Screening (08:45 AM): Daily Trend & 20 EMA Pre-Filter
2. 6-Rule Intraday Breakout at 09:20 AM
3. 0.05% Market Order Slippage & 0.05% Open=Low Tolerance
4. Intrabar Execution Simulator (No Lookahead Bias, lock T1 profit)
5. Telegram Report Dispatch

Run: python backtest_10days.py
"""

import os
import sys
import time
import json
import logging
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
IST = ZoneInfo('Asia/Kolkata')

import numpy as np
import pandas as pd
import pyotp
import requests
from dotenv import load_dotenv
from SmartApi import SmartConnect

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger("AlphaQuant_10Day_Backtest")

# ── Credentials & Config ──
ANGEL_API_KEY = os.getenv("ANGEL_API_KEY")
ANGEL_CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE")
ANGEL_PASSWORD = os.getenv("ANGEL_MPIN") or os.getenv("ANGEL_PASSWORD")
ANGEL_TOTP_KEY = os.getenv("ANGEL_TOTP_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

from shared.constants import DEFAULT_100_STOCKS, NIFTY_TOKEN, MIN_STOCK_PRICE, MAX_STOCK_PRICE
from shared.deepseek_analyzer import analyze_hit_with_deepseek

MIN_PRICE = MIN_STOCK_PRICE
MAX_PRICE = MAX_STOCK_PRICE
OPEN_LOW_TOLERANCE = 0.0005   # 0.05% tolerance for Open=Low
SLIPPAGE_PCT = 0.0005         # 0.05% market order slippage
CAPITAL = 100000.0
RISK_PCT = 0.01               # 1% risk per trade (Rs 1,000)
AI_MIN_SCORE = 75             # AI Quality Score threshold (>= 75 required)

# Load selected stocks watchlist
if os.path.exists("selected_stocks.json"):
    with open("selected_stocks.json", "r", encoding="utf-8") as f:
        STOCK_UNIVERSE = json.load(f)
else:
    STOCK_UNIVERSE = list(DEFAULT_100_STOCKS)


# ── Telegram Helper ──
def send_telegram(msg: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
            if resp.ok:
                return True
        except Exception:
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
        logger.info("Angel One login SUCCESS.")
        return obj
    else:
        raise RuntimeError(f"Login failed: {data.get('message', 'Unknown error')}")


# ── Instrument Master ──
def fetch_nse_eq_tokens() -> dict:
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    eq_map = {}
    for item in data:
        if item.get("exch_seg") == "NSE" and item.get("symbol", "").endswith("-EQ"):
            sym = item["symbol"].replace("-EQ", "")
            eq_map[sym] = item["token"]
    return eq_map


# ── Candle Data Fetcher ──
def fetch_candles(smart_api: SmartConnect, token: str, days_back: int = 20) -> pd.DataFrame:
    to_date = datetime.now()
    from_date = to_date - timedelta(days=days_back)

    params = {
        "exchange": "NSE",
        "symboltoken": token,
        "interval": "FIVE_MINUTE",
        "fromdate": from_date.strftime("%Y-%m-%d 09:00"),
        "todate": to_date.strftime("%Y-%m-%d 15:30"),
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            raw = smart_api.getCandleData(params)
            if not raw or not isinstance(raw, dict) or raw.get("status") is False:
                if attempt < max_retries - 1:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                return pd.DataFrame()

            data = raw.get("data", [])
            if not data:
                return pd.DataFrame()

            df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            if df["close"].mean() > 50000.0:
                df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]] / 100.0

            df.sort_values("timestamp", inplace=True)
            df.reset_index(drop=True, inplace=True)
            return df
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))
            else:
                return pd.DataFrame()
    return pd.DataFrame()


# ── Intrabar Execution Simulator ──
def simulate_intrabar(entry: float, sl: float, t1: float, t2: float, df_intraday: pd.DataFrame) -> dict:
    if df_intraday.empty:
        return {"status": "NO_DATA", "pnl_r": 0.0, "exit_price": entry, "exit_time": "N/A", "detail": "No intraday data"}

    t1_hit = False
    original_sl = sl
    sl_dist = max(entry - original_sl, 0.50)

    for _, bar in df_intraday.iterrows():
        b_high = float(bar["high"])
        b_low = float(bar["low"])
        t_str = bar["timestamp"].strftime("%H:%M")

        # Conflict Bar: Both SL and T1 touched in exact same bar -> Conservative SL
        if b_low <= sl and b_high >= t1:
            if t1_hit:
                return {"status": "T1_PARTIAL_SL", "pnl_r": 1.0, "exit_price": entry, "exit_time": t_str, "detail": f"Breakeven SL after T1 at {t_str}"}
            return {"status": "SL_HIT", "pnl_r": -1.0, "exit_price": sl, "exit_time": t_str, "detail": f"SL Hit (Conflict Bar) at {t_str}"}

        # Check Stop Loss
        if b_low <= sl:
            if t1_hit:
                return {"status": "T1_PARTIAL_SL", "pnl_r": 1.0, "exit_price": entry, "exit_time": t_str, "detail": f"Breakeven SL after T1 at {t_str}"}
            return {"status": "SL_HIT", "pnl_r": -1.0, "exit_price": sl, "exit_time": t_str, "detail": f"SL Hit at {t_str}"}

        # Check Target 2 (1:3 R:R)
        if b_high >= t2:
            return {"status": "T2_HIT", "pnl_r": 3.0, "exit_price": t2, "exit_time": t_str, "detail": f"T2 Hit (+3.0R) at {t_str}"}

        # Check Target 1 (1:2 R:R) -> Trail SL to Breakeven
        if b_high >= t1 and not t1_hit:
            t1_hit = True
            sl = entry  # Move SL to cost

    # EOD Close
    eod_close = float(df_intraday.iloc[-1]["close"])
    eod_time = df_intraday.iloc[-1]["timestamp"].strftime("%H:%M")
    realized_r = round((eod_close - entry) / sl_dist, 2)

    if t1_hit:
        return {"status": "T1_PARTIAL_EOD", "pnl_r": round(1.0 + (realized_r * 0.5), 2), "exit_price": eod_close, "exit_time": eod_time, "detail": f"T1 + EOD Close at {eod_time}"}
    else:
        return {"status": "EOD_CLOSE", "pnl_r": realized_r, "exit_price": eod_close, "exit_time": eod_time, "detail": f"EOD Close at {eod_time}"}


# ══════════════════════════════════════════════════════════════════
# MAIN 10-DAY BACKTEST RUNNER
# ══════════════════════════════════════════════════════════════════
def run_10day_backtest():
    print("=" * 60)
    print("ALPHAQUANT AI — 10-DAY INSTITUTIONAL BACKTEST ENGINE")
    print("=" * 60)

    # 1. Login & Master
    smart_api = login_angel()
    nse_tokens = fetch_nse_eq_tokens()

    # Use 300 stocks from NSE Cash Equity Master
    STOCK_UNIVERSE = list(nse_tokens.keys())[:300]

    # 2. Fetch Nifty data for last 20 days
    logger.info("Fetching historical data for Nifty 50...")
    nifty_df = fetch_candles(smart_api, NIFTY_TOKEN, days_back=20)
    if nifty_df.empty:
        print("Error: Nifty historical candles empty.")
        return

    # Get distinct trading dates
    trading_dates = sorted(list(set(nifty_df["timestamp"].dt.date)))
    # Take last 10 trading days
    last_10_dates = trading_dates[-10:] if len(trading_dates) >= 10 else trading_dates

    print(f"\nTargeting 10 Trading Days: {last_10_dates[0]} to {last_10_dates[-1]}")
    print(f"Expanded Watchlist Size: {len(STOCK_UNIVERSE)} stocks (No price range limit)\n")

    # 3. Pre-fetch candle data for all stocks
    logger.info("Pre-fetching historical 5-minute candles for all %d stocks...", len(STOCK_UNIVERSE))
    stock_dfs = {}
    for idx, sym in enumerate(STOCK_UNIVERSE, 1):
        token = nse_tokens.get(sym)
        if not token:
            continue
        df_stock = fetch_candles(smart_api, token, days_back=20)
        if not df_stock.empty:
            stock_dfs[sym] = df_stock
        time.sleep(0.08)

    logger.info("Successfully cached candle data for %d/%d stocks.", len(stock_dfs), len(STOCK_UNIVERSE))

    # 4. Backtest Day by Day
    all_trades = []
    daily_summaries = []

    for trade_date in last_10_dates:
        date_str = trade_date.strftime("%Y-%m-%d")

        # Nifty check for this day
        nifty_day = nifty_df[nifty_df["timestamp"].dt.date == trade_date]
        if nifty_day.empty:
            continue
        nifty_first = nifty_day.iloc[0]
        nifty_bullish = float(nifty_first["close"]) > float(nifty_first["open"])

        # ── Step A: Pre-Market Screening (08:45 AM simulation using data before trade_date) ──
        premarket_candidates = []
        for sym, df_s in stock_dfs.items():
            prev_df = df_s[df_s["timestamp"].dt.date < trade_date]
            if prev_df.empty:
                continue

            # Compute daily close & 20 EMA on 5-min data leading up to trade_date
            df_copy = prev_df.copy()
            df_copy["ema20"] = df_copy["close"].ewm(span=20, adjust=False).mean()

            last_bar = df_copy.iloc[-1]
            p_close = float(last_bar["close"])
            p_open = float(last_bar["open"])
            p_ema = float(last_bar["ema20"])

            # Pre-Market Rules: Bullish Body, > 20 EMA (No Price Limit)
            if (p_close > p_open) and (p_close > p_ema):
                premarket_candidates.append(sym)

        # ── Step B: 09:20 AM Breakout Scan on candidates ──
        day_trades = []

        if nifty_bullish:
            for sym in premarket_candidates:
                df_s = stock_dfs[sym]
                day_candles = df_s[df_s["timestamp"].dt.date == trade_date]
                if day_candles.empty:
                    continue

                c0 = day_candles.iloc[0]
                c_open = float(c0["open"])
                c_high = float(c0["high"])
                c_low = float(c0["low"])
                c_close = float(c0["close"])

                prev_candles = df_s[df_s["timestamp"].dt.date < trade_date]
                if prev_candles.empty:
                    continue
                prev_close = float(prev_candles.iloc[-1]["close"])

                # Rule 2: Gap-Up (Open > Prev Close)
                if c_open <= prev_close:
                    continue

                # Rule 3: Open = Low (0.05% tolerance)
                if c_open > 0 and ((c_open - c_low) / c_open) > OPEN_LOW_TOLERANCE:
                    continue

                # Rule 4: Bullish Body (Close > Open)
                if c_close <= c_open:
                    continue

                # Rule 5: Upper Wick <= 50%
                candle_range = c_high - c_low
                if candle_range <= 0:
                    continue
                upper_wick = c_high - c_close
                if upper_wick > 0.50 * candle_range:
                    continue

                # Rule 6: Close > 20 EMA
                df_copy = df_s[df_s["timestamp"] <= c0["timestamp"]].copy()
                df_copy["ema20"] = df_copy["close"].ewm(span=20, adjust=False).mean()
                ema_val = float(df_copy.iloc[-1]["ema20"])
                if c_close <= ema_val:
                    continue

                # ── RULE 7: DEEPSEEK AI MODEL EVALUATION & SCORING ──
                wick_pct = (upper_wick / candle_range) * 100
                hit_dict = {
                    "symbol": sym,
                    "open": round(c_open, 2),
                    "high": round(c_high, 2),
                    "low": round(c_low, 2),
                    "close": round(c_close, 2),
                    "ema20": round(ema_val, 2),
                    "wick_pct": round(wick_pct, 1)
                }

                ai_eval = analyze_hit_with_deepseek(hit_dict, nifty_bullish=nifty_bullish)
                ai_score = ai_eval.get("score", 75)
                ai_rec = ai_eval.get("recommendation", "BUY")

                # Filter out low-confidence AI trades (< 75)
                if ai_score < AI_MIN_SCORE or ai_rec == "AVOID":
                    logger.info("REJECTED BY AI MODEL: %s (AI Score: %d/100) -> %s", sym, ai_score, ai_eval.get("reasoning"))
                    continue

                # ── TRIGGER HIGH-CONFIDENCE AI TRADE ──
                entry = round(c_close * (1 + SLIPPAGE_PCT), 2)
                sl = round(c_low * 0.997, 2)
                risk = max(entry - sl, 0.50)
                t1 = round(entry + (risk * 2.0), 2)
                t2 = round(entry + (risk * 3.0), 2)
                shares = int(CAPITAL * RISK_PCT / risk)

                intraday_bars = day_candles.iloc[1:]
                outcome = simulate_intrabar(entry, sl, t1, t2, intraday_bars)

                trade_info = {
                    "date": date_str,
                    "symbol": sym,
                    "entry": entry,
                    "sl": sl,
                    "t1": t1,
                    "t2": t2,
                    "shares": shares,
                    "ai_score": ai_score,
                    "ai_reasoning": ai_eval.get("reasoning", ""),
                    "outcome": outcome
                }
                day_trades.append(trade_info)
                all_trades.append(trade_info)

        day_r = sum(t["outcome"]["pnl_r"] for t in day_trades)
        daily_summaries.append({
            "date": date_str,
            "nifty_bullish": nifty_bullish,
            "candidates": len(premarket_candidates),
            "trades": len(day_trades),
            "net_r": day_r
        })

        logger.info("DATE %s | Nifty: %s | PreMarket Candidates: %d | Trades: %d | Net R: %+.2f R",
                    date_str, "BULLISH" if nifty_bullish else "BEARISH", len(premarket_candidates), len(day_trades), day_r)

    # ── Final Performance Report ──
    total_trades = len(all_trades)
    wins = sum(1 for t in all_trades if t["outcome"]["pnl_r"] > 0)
    losses = sum(1 for t in all_trades if t["outcome"]["pnl_r"] < 0)
    total_net_r = sum(t["outcome"]["pnl_r"] for t in all_trades)
    win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
    profit_rs = total_net_r * (CAPITAL * RISK_PCT)

    print("\n" + "=" * 70)
    print("ALPHAQUANT AI — 10-DAY BACKTEST PERFORMANCE RESULTS")
    print("=" * 70)
    print(f"Period                     : {last_10_dates[0]} to {last_10_dates[-1]}")
    print(f"Total Trading Days Scanned : {len(last_10_dates)}")
    print(f"Total Trades Executed     : {total_trades}")
    print(f"Winning Trades             : {wins}")
    print(f"Losing Trades              : {losses}")
    print(f"Win Rate                   : {win_rate:.1f}%")
    print(f"Total Net Risk-Reward (R)  : {total_net_r:+.2f} R")
    print(f"Starting Capital           : ₹{CAPITAL:,.0f} (1% risk/trade)")
    print(f"Est. Net P/L (Rupees)      : ₹{profit_rs:+,.2f}")
    print("=" * 70)

    print("\nDAILY BREAKDOWN TABLE:")
    print(f"{'Date':<12} | {'Nifty':<8} | {'Candidates':<10} | {'Trades':<6} | {'Day Net R'}")
    print("-" * 55)
    for d in daily_summaries:
        n_str = "BULLISH" if d["nifty_bullish"] else "BEARISH"
        print(f"{d['date']:<12} | {n_str:<8} | {d['candidates']:<10} | {d['trades']:<6} | {d['net_r']:+.2f} R")
    print("-" * 55)

    if all_trades:
        print("\nEXECUTED TRADES LOG:")
        for i, t in enumerate(all_trades, 1):
            out = t["outcome"]
            print(f"  {i}. [{t['date']}] {t['symbol']:10s} Entry: ₹{t['entry']:<7.2f} SL: ₹{t['sl']:<7.2f} | Exit: {out['detail']} -> {out['pnl_r']:+.2f} R")

    # Send summary to Telegram
    tg_summary = (
        f"📊 *ALPHAQUANT AI - 10-DAY BACKTEST REPORT*\n"
        f"{'='*40}\n"
        f"Period: *{last_10_dates[0]} to {last_10_dates[-1]}*\n"
        f"Days Scanned: *{len(last_10_dates)} trading days*\n"
        f"{'='*40}\n"
        f"Total Trades: *{total_trades}*\n"
        f"Wins: *{wins}* | Losses: *{losses}*\n"
        f"Win Rate: *{win_rate:.1f}%*\n"
        f"Net R:R: *{total_net_r:+.2f} R*\n"
        f"Capital: *Rs.{CAPITAL:,.0f}* (1% risk/trade)\n"
        f"Net Profit: *Rs.{profit_rs:+,.2f}*\n"
        f"{'='*40}\n"
        f"_Engine: Pre-Market Screen + 6-Rule Breakout + Intrabar Simulator_"
    )
    send_telegram(tg_summary)


if __name__ == "__main__":
    run_10day_backtest()
