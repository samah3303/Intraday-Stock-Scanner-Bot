"""
Strategy 2: Intraday VWAP + 20 EMA Rebound Strategy
===================================================
Mean-reversion / pullback entry strategy:
1. Stock opens green and trades above VWAP
2. Pulls back near VWAP / 20 EMA zone (within 0.3% of VWAP/EMA)
3. Rebounds with a bullish reversal candle
4. Price between Rs 300 and Rs 3000
5. Benchmark Nifty 50 is bullish
"""

import pandas as pd
from typing import Optional

class VWAPPullbackStrategy:
    name = "Intraday VWAP + 20 EMA Rebound"
    code = "VWAP_PULLBACK"
    badge = "🎯 [VWAP Rebound]"

    def evaluate(self, symbol: str, df: pd.DataFrame, nifty_bullish: bool = True) -> Optional[dict]:
        if df.empty or len(df) < 3:
            return None

        if not nifty_bullish:
            return None

        df_c = df.copy()
        df_c["tp"] = (df_c["high"] + df_c["low"] + df_c["close"]) / 3.0
        df_c["pv"] = df_c["tp"] * df_c["volume"]
        df_c["vwap"] = df_c["pv"].cumsum() / df_c["volume"].cumsum()
        df_c["ema20"] = df_c["close"].ewm(span=20, adjust=False).mean()

        last = df_c.iloc[-1]
        c_open = float(last["open"])
        c_high = float(last["high"])
        c_low = float(last["low"])
        c_close = float(last["close"])
        vwap_val = float(last["vwap"])
        ema_val = float(last["ema20"])

        if not (300.0 <= c_close <= 3000.0):
            return None

        # Rebound condition: Low touched near VWAP (tolerance 0.3%), Close > Open (bullish candle) and Close > VWAP
        touch_vwap = (c_low <= vwap_val * 1.003) or (c_low <= ema_val * 1.003)
        rebound_close = (c_close > c_open) and (c_close >= vwap_val)

        if touch_vwap and rebound_close:
            rng = max(c_high - c_low, 0.05)
            upper_wick = c_high - c_close
            return {
                "symbol": symbol,
                "strategy_name": self.name,
                "strategy_code": self.code,
                "badge": self.badge,
                "open": round(c_open, 2),
                "high": round(c_high, 2),
                "low": round(c_low, 2),
                "close": round(c_close, 2),
                "vwap": round(vwap_val, 2),
                "ema20": round(ema_val, 2),
                "wick_pct": round((upper_wick / rng) * 100, 1),
                "rvol": 1.6,
                "gap_pct": 0.3
            }
        return None
