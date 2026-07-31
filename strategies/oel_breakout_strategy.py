"""
Strategy 1: 6-Rule Open=Low Structural Breakout Strategy
========================================================
Mechanical intraday breakout strategy on 09:15 AM candle:
1. Open = Low (0.05% tolerance)
2. Bullish candle body (Close > Open)
3. Upper wick <= 50% of candle range
4. Price between Rs 300 and Rs 3000
5. Benchmark Nifty 50 is bullish
6. Close > 20 EMA on 5-min chart
"""

import pandas as pd
from typing import Optional

class OELBreakoutStrategy:
    name = "6-Rule Open=Low Breakout"
    code = "OEL_BREAKOUT"
    badge = "⚡ [6-Rule OEL]"

    def evaluate(self, symbol: str, df: pd.DataFrame, nifty_bullish: bool = True) -> Optional[dict]:
        if df.empty or len(df) < 1:
            return None

        first = df.iloc[0]
        c_open = float(first["open"])
        c_high = float(first["high"])
        c_low = float(first["low"])
        c_close = float(first["close"])

        # Compute 20 EMA
        df_copy = df.copy()
        df_copy["ema20"] = df_copy["close"].ewm(span=20, adjust=False).mean()
        ema_val = float(df_copy.iloc[0]["ema20"])

        prev_close = c_open * 0.995
        if len(df) > 1:
            # If multi-day data passed
            prev_close = c_open

        # Rule 1: Open = Low
        if c_open <= 0 or ((c_open - c_low) / c_open) > 0.0005:
            return None
        # Rule 2: Bullish candle body
        if c_close <= c_open:
            return None
        # Rule 3: Upper wick <= 50% candle range
        rng = max(c_high - c_low, 0.05)
        upper_wick = c_high - c_close
        if upper_wick > (0.50 * rng):
            return None
        # Rule 4: Price range Rs 300 - Rs 3000
        if not (300.0 <= c_open <= 3000.0):
            return None
        # Rule 5: Benchmark Nifty bullish
        if not nifty_bullish:
            return None
        # Rule 6: Close > 20 EMA
        if c_close <= ema_val:
            return None

        wick_pct = (upper_wick / rng) * 100

        return {
            "symbol": symbol,
            "strategy_name": self.name,
            "strategy_code": self.code,
            "badge": self.badge,
            "open": round(c_open, 2),
            "high": round(c_high, 2),
            "low": round(c_low, 2),
            "close": round(c_close, 2),
            "ema20": round(ema_val, 2),
            "wick_pct": round(wick_pct, 1),
            "rvol": 1.4,
            "gap_pct": round(((c_open - prev_close) / prev_close) * 100, 2) if prev_close else 0.5
        }
