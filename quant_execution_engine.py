"""
AlphaQuant AI — Production Strategy Engine & Intrabar Execution Simulator
==========================================================================
Eliminates lookahead bias using 1-minute intrabar price sequence tracking,
enforces strict vector pre-filtering (₹300–₹3000 price bounds), applies real-world
slippage (0.05%), and calculates dynamic 1% risk position sizing.

Author: Senior Quantitative Developer & Algorithmic Trading Specialist
"""

import sys
import logging
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Union

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger("QuantExecutionEngine")



class VectorizedPreFilter:
    """
    Strict Pre-Scan Filter Engine. Enforces Rule 1 (₹300 <= Price <= ₹3000)
    and Open=Low breakout rules prior to ML/DeepSeek AI scoring.
    """

    MIN_PRICE: float = 300.0
    MAX_PRICE: float = 3000.0
    OPEN_LOW_TOLERANCE: float = 0.0005  # 0.05% max delta between Open and Low

    @classmethod
    def filter_candidates(cls, df_universe: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
        """
        Vectorized filter enforcing Rule 1 & Rule 3.
        Required DataFrame columns: ['symbol', 'open', 'high', 'low', 'close']
        """
        if df_universe.empty:
            return pd.DataFrame(), []

        # Vectorized Conditions
        rule1_price_mask = (df_universe["open"] >= cls.MIN_PRICE) & (df_universe["open"] <= cls.MAX_PRICE)
        rule3_open_low_mask = ((df_universe["open"] - df_universe["low"]).abs() / df_universe["open"]) <= cls.OPEN_LOW_TOLERANCE

        passed_mask = rule1_price_mask & rule3_open_low_mask
        df_passed = df_universe[passed_mask].copy()
        
        filtered_out = df_universe[~passed_mask]["symbol"].tolist()
        
        for sym in df_universe[~rule1_price_mask]["symbol"]:
            p = float(df_universe[df_universe["symbol"] == sym]["open"].values[0])
            logger.info("Rule 1 Violation: Ticker '%s' (Price ₹%.2f) excluded (Must be ₹300–₹3000).", sym, p)

        return df_passed.reset_index(drop=True), filtered_out


class DynamicPositionSizer:
    """
    Calculates exact position sizing and real-world execution fill price including slippage.
    """

    DEFAULT_SLIPPAGE_PCT: float = 0.0005  # 0.05% slippage on market order fill

    @classmethod
    def calculate_position(
        cls,
        account_capital: float,
        signal_price: float,
        sl_price: float,
        risk_pct: float = 0.01,
        slippage_pct: float = DEFAULT_SLIPPAGE_PCT
    ) -> Dict[str, float]:
        """
        Calculates execution fill price with slippage, risk amount, and share count.
        """
        # Apply 0.05% slippage to 09:20:00 AM market order fill price
        fill_price = round(signal_price * (1.0 + slippage_pct), 2)
        
        # Risk per share based on actual fill price
        risk_per_share = max(fill_price - sl_price, 0.50)
        max_risk_amount = account_capital * risk_pct
        
        # Position sizing (number of shares)
        shares = int(np.floor(max_risk_amount / risk_per_share))
        position_value = round(shares * fill_price, 2)

        return {
            "signal_price": signal_price,
            "fill_price": fill_price,
            "slippage_paid": round(fill_price - signal_price, 2),
            "sl_price": sl_price,
            "risk_per_share": round(risk_per_share, 2),
            "max_risk_amount": max_risk_amount,
            "shares": shares,
            "position_value": position_value
        }


class IntrabarExecutionSimulator:
    """
    1-Minute Intrabar Execution Engine to eliminate Lookahead Bias.
    Tracks chronological Open -> Low -> High -> Close 1-minute bar dynamics.
    If both SL and Target are touched in the same 1-minute bar, it is conservatively
    marked as a Stop-Loss Hit (-1.0 R).
    """

    @staticmethod
    def validate_trade_intrabar(
        symbol: str,
        entry_price: float,
        sl_price: float,
        t1_price: float,
        t2_price: float,
        minute_df: pd.DataFrame
    ) -> Dict[str, Union[str, float]]:
        """
        Processes 1-minute OHLC bars from 09:20 AM to 15:25 PM chronologically.
        minute_df must contain: ['timestamp', 'open', 'high', 'low', 'close']
        """
        if minute_df.empty:
            return {
                "symbol": symbol,
                "status": "NO_DATA",
                "realized_r": 0.0,
                "exit_price": entry_price,
                "exit_time": "09:20:00",
                "detail": "Insufficient 1-minute intraday data."
            }

        t1_hit = False
        sl_dist = max(entry_price - sl_price, 0.50)

        for idx, bar in minute_df.iterrows():
            b_high = float(bar["high"])
            b_low = float(bar["low"])
            t_stamp = str(bar.get("timestamp", bar.get("time_str", "10:00:00")))

            # CONSERVATIVE LOOKAHEAD BIAS FIX:
            # If both SL and Target 1/Target 2 are touched within the exact same 1-minute bar,
            # assume conservative worst-case execution: STOP LOSS HIT FIRST!
            if b_low <= sl_price and b_high >= t1_price:
                return {
                    "symbol": symbol,
                    "status": "SL_HIT",
                    "realized_r": -1.0,
                    "exit_price": sl_price,
                    "exit_time": t_stamp,
                    "detail": f"🛑 Intrabar Conflict: Both SL & Target touched in 1-min bar at {t_stamp}. Conservative SL (-1.0 R) triggered."
                }

            # Check Stop Loss Hit
            if b_low <= sl_price:
                return {
                    "symbol": symbol,
                    "status": "SL_HIT",
                    "realized_r": -1.0,
                    "exit_price": sl_price,
                    "exit_time": t_stamp,
                    "detail": f"🛑 SL Hit at ₹{sl_price:.2f} (Time: {t_stamp})"
                }

            # Check Target 2 Hit (1:3 R:R)
            if b_high >= t2_price:
                return {
                    "symbol": symbol,
                    "status": "T2_HIT",
                    "realized_r": 3.0,
                    "exit_price": t2_price,
                    "exit_time": t_stamp,
                    "detail": f"🎯🎯 Target 2 Hit (+3.0 R:R) at ₹{t2_price:.2f} (Time: {t_stamp})"
                }

            # Check Target 1 Hit (1:2 R:R)
            if b_high >= t1_price and not t1_hit:
                t1_hit = True
                # If T1 is hit, trail Stop Loss to Breakeven (Entry Price) for remaining position
                sl_price = entry_price

        # EOD Auto Square-off at 15:25 PM
        eod_close = float(minute_df.iloc[-1]["close"])
        eod_time = str(minute_df.iloc[-1].get("timestamp", "15:25:00"))
        realized_r = round((eod_close - entry_price) / sl_dist, 2)
        
        status = "T1_PARTIAL_EOD" if t1_hit else "EOD_CLOSE"
        return {
            "symbol": symbol,
            "status": status,
            "realized_r": realized_r if not t1_hit else round(1.0 + (realized_r * 0.5), 2),
            "exit_price": eod_close,
            "exit_time": eod_time,
            "detail": f"🕒 EOD Close at ₹{eod_close:.2f} ({realized_r:+.2f} R:R) (Time: {eod_time})"
        }


# ---------------------------------------------------------------------------
# Unit Verification & Conflict Test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("Executing Strategy Engine Diagnostics & Unit Tests...")

    # 1. Test Rule 1 Vectorized Pre-Filter Fix
    universe_sample = pd.DataFrame([
        {"symbol": "COLPAL", "open": 2045.00, "high": 2060.00, "low": 2045.00, "close": 2055.00},
        {"symbol": "IOC", "open": 138.50, "high": 141.00, "low": 138.50, "close": 140.00},        # MUST BE FILTERED OUT (₹138.50 < ₹300)
        {"symbol": "POWERGRID", "open": 286.20, "high": 290.00, "low": 286.20, "close": 288.00},  # MUST BE FILTERED OUT (₹286.20 < ₹300)
        {"symbol": "TATAMOTORS", "open": 994.50, "high": 1005.00, "low": 994.50, "close": 1000.00}, # QUALIFIED
        {"symbol": "TCS", "open": 3855.00, "high": 3890.00, "low": 3855.00, "close": 3880.00}     # MUST BE FILTERED OUT (₹3855.00 > ₹3000)
    ])

    df_filtered, excluded = VectorizedPreFilter.filter_candidates(universe_sample)
    print("\n--- Rule 1 Pre-Filter Results ---")
    print(f"Qualified Candidates ({len(df_filtered)}): {df_filtered['symbol'].tolist()}")
    print(f"Excluded Tickers ({len(excluded)}): {excluded}")

    # 2. Test Real-World Slippage & Position Sizing Engine
    pos = DynamicPositionSizer.calculate_position(account_capital=100000.0, signal_price=994.50, sl_price=984.20)
    print("\n--- 1% Risk Position Sizing with 0.05% Slippage ---")
    print(f"Signal Price: ₹{pos['signal_price']:.2f} | Execution Fill Price (0.05% Slippage): ₹{pos['fill_price']:.2f}")
    print(f"Position Sizing: Buy {pos['shares']} Shares (Total Position Value: ₹{pos['position_value']:,.2f})")

    # 3. Test 1-Minute Intrabar Execution Simulator (Testing Conflict Bar Handling)
    print("\n--- 1-Minute Intrabar Simulator Conflict Test ---")
    minute_bars_conflict = pd.DataFrame([
        {"timestamp": "09:20:00", "open": 994.50, "high": 998.00, "low": 994.00, "close": 997.00},
        {"timestamp": "09:21:00", "open": 997.00, "high": 1026.00, "low": 980.00, "close": 990.00}, # CONFLICT BAR: High=1026 (Hits T2) & Low=980 (Hits SL)
    ])

    result = IntrabarExecutionSimulator.validate_trade_intrabar(
        symbol="TATAMOTORS",
        entry_price=pos["fill_price"],
        sl_price=pos["sl_price"],
        t1_price=1015.10,
        t2_price=1025.40,
        minute_df=minute_bars_conflict
    )

    print(f"Trade Result: {result['status']} | Realized R:R: {result['realized_r']} R")
    print(f"Detail: {result['detail']}")
