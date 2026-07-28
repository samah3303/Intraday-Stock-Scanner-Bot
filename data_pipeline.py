"""
AlphaQuant AI — Institutional-Grade Historical Data Engineering Pipeline
=========================================================================
Robust Python module to fetch, map, scale, adjust corporate actions, and sanitize
NSE Equity intraday data for Zerodha Kite Connect & Angel One SmartAPI.

Author: Senior Quantitative Developer & Data Engineer
"""

import os
import sys
import logging
import json
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger("QuantDataPipeline")


class NSEInstrumentMapper:
    """
    Strict Instrument Token Mapper to resolve NSE Cash Equity (EQ) tokens.
    Prevents accidental mapping to Futures (F&O), Options, Indices, or Deprecated Tickers.
    """

    ANGEL_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

    def __init__(self):
        self.eq_token_map: Dict[str, str] = {}    # {SYMBOL: TOKEN}
        self.symbol_details: Dict[str, dict] = {} # Detailed metadata

    def load_angel_master(self) -> Dict[str, str]:
        """
        Downloads and filters Angel One Scrip Master strictly for NSE Cash Equities.
        """
        logger.info("Fetching Angel One Instrument Master...")
        try:
            resp = requests.get(self.ANGEL_MASTER_URL, timeout=60)
            resp.raise_for_status()
            data = resp.json()

            mapped_count = 0
            for item in data:
                # STRICT FILTERING RULE:
                # 1. Exchange must be 'NSE'
                # 2. Instrument type must be 'AMXEQ' or symbol ends strictly with '-EQ'
                # 3. Exclude 'NIFTY', 'BANKNIFTY', 'FINNIFTY' index contracts
                exch = str(item.get("exch_seg", "")).upper()
                symbol_raw = str(item.get("symbol", "")).upper()
                token = str(item.get("token", ""))

                if exch == "NSE" and symbol_raw.endswith("-EQ"):
                    clean_ticker = symbol_raw.replace("-EQ", "").strip()
                    
                    # Store exact cash equity token
                    self.eq_token_map[clean_ticker] = token
                    self.symbol_details[clean_ticker] = {
                        "token": token,
                        "name": item.get("name"),
                        "expiry": item.get("expiry"),
                        "strike": item.get("strike"),
                        "lotsize": item.get("lotsize"),
                        "tick_size": item.get("tick_size")
                    }
                    mapped_count += 1

            logger.info("Successfully mapped %d strict NSE Equity (EQ) instruments.", mapped_count)
            return self.eq_token_map

        except Exception as exc:
            logger.error("Failed to load Angel Instrument Master: %s", exc)
            return {}

    def get_eq_token(self, symbol: str) -> Optional[str]:
        """Return the exact NSE Cash Equity token for a ticker symbol."""
        clean_sym = symbol.upper().replace("-EQ", "").strip()
        token = self.eq_token_map.get(clean_sym)
        if not token:
            logger.warning("Token not found for symbol '%s'. Check for ticker rename/delisting.", clean_sym)
        return token


class PriceSanitizerAndScaler:
    """
    Sanitizes raw broker candle output, handles Paise vs Rupee scaling,
    and converts data types to standardized float64 pandas DataFrames.
    """

    @staticmethod
    def sanitize_candles(raw_candles: List[list], broker: str = "ANGEL") -> pd.DataFrame:
        """
        Converts raw broker candle list to a clean, type-enforced Pandas DataFrame.
        """
        if not raw_candles:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        df = pd.DataFrame(raw_candles)

        # Handle Broker Column Schema Variations
        if broker.upper() == "ANGEL":
            # SmartAPI Returns: [timestamp, open, high, low, close, volume]
            df = df.iloc[:, :6]
            df.columns = ["timestamp", "open", "high", "low", "close", "volume"]
        elif broker.upper() == "KITE":
            # Kite Connect Returns: [timestamp, open, high, low, close, volume, oi]
            df = df.iloc[:, :6]
            df.columns = ["timestamp", "open", "high", "low", "close", "volume"]

        # Parse Timestamps cleanly
        df["timestamp"] = pd.to_datetime(df["timestamp"])

        # Typecast Price & Volume to numeric float64
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # TICK SCALING CHECK (Paise vs Rupees):
        # Some legacy feeds / WebSocket ticks transmit price in paise (1/100th rupee).
        # E.g. If IOC is returned as 13800.0 instead of 138.00
        mean_close = df["close"].mean()
        if mean_close > 50000.0:  # Suspicious paise scaling
            logger.warning("Detected Paise scaling (100x divisor applied). Mean Close: %.2f", mean_close)
            df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]] / 100.0

        return df.sort_values("timestamp").reset_index(drop=True)


class CorporateActionAdjuster:
    """
    Adjusts historical price series for splits, bonuses, and rights issues
    to ensure smooth continuity across corporate action dates.
    """

    @staticmethod
    def adjust_for_splits(df: pd.DataFrame, split_ratio: float, split_date: str) -> pd.DataFrame:
        """
        Applies split adjustment multiplier to price data prior to split_date.
        split_ratio example: 2.0 for 1:1 bonus or 2-for-1 stock split.
        """
        if df.empty or split_ratio <= 1.0:
            return df

        s_date = pd.to_datetime(split_date)
        mask = df["timestamp"] < s_date

        df_adj = df.copy()
        df_adj.loc[mask, ["open", "high", "low", "close"]] = df_adj.loc[mask, ["open", "high", "low", "close"]] / split_ratio
        df_adj.loc[mask, "volume"] = df_adj.loc[mask, "volume"] * split_ratio

        logger.info("Applied %.1fx split adjustment for dates prior to %s", split_ratio, split_date)
        return df_adj


class DataSanityValidator:
    """
    Validation Suite to catch data corruption, anomalous gaps, and wrong token assignments.
    """

    @staticmethod
    def validate_historical_df(df: pd.DataFrame, symbol: str, expected_price_range: Tuple[float, float] = (50.0, 5000.0)) -> Tuple[bool, List[str]]:
        """
        Runs comprehensive sanity checks on fetched historical DataFrame.
        Returns: (is_valid: bool, anomalies: List[str])
        """
        anomalies = []
        if df.empty:
            return False, ["DataFrame is completely empty."]

        min_p, max_p = expected_price_range
        latest_close = float(df.iloc[-1]["close"])

        # Check 1: Price Bounds Check
        if not (min_p <= latest_close <= max_p):
            anomalies.append(
                f"CRITICAL: Symbol '{symbol}' price ₹{latest_close:.2f} is outside expected universe bounds (₹{min_p}–₹{max_p}). "
                f"Probable Cause: Wrong instrument token (F&O Futures/Index) or unscaled Paise data!"
            )

        # Check 2: High/Low/Open/Close Logical Consistency
        invalid_candles = df[(df["high"] < df["low"]) | (df["open"] > df["high"]) | (df["open"] < df["low"]) | (df["close"] > df["high"]) | (df["close"] < df["low"])]
        if not invalid_candles.empty:
            anomalies.append(f"LOGIC CORRUPTION: Found {len(invalid_candles)} candle(s) violating High >= Low / Open / Close logic.")

        # Check 3: Extreme Single-Bar Price Jump (>20% Gap Check)
        df["prev_close"] = df["close"].shift(1)
        df["pct_change"] = (df["open"] - df["prev_close"]).abs() / df["prev_close"]
        extreme_jumps = df[df["pct_change"] > 0.20]
        if not extreme_jumps.empty:
            anomalies.append(f"ANOMALY GAP: Detected {len(extreme_jumps)} bar(s) with >20% overnight gap. Possible unadjusted split or bad tick!")

        is_valid = len(anomalies) == 0
        return is_valid, anomalies


# ---------------------------------------------------------------------------
# Test Verification Routine
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("Running Quant Data Engineering Diagnostics Test...")

    # 1. Test Strict NSE Instrument Mapper
    mapper = NSEInstrumentMapper()
    tokens = mapper.load_angel_master()

    test_symbols = ["COLPAL", "IOC", "POWERGRID", "RELIANCE"]
    print("\n--- Strict Instrument Mapping ---")
    for sym in test_symbols:
        token = mapper.get_eq_token(sym)
        print(f"Symbol: {sym:12s} -> Exact NSE Cash Token: {token}")

    # 2. Test Price Sanitizer & Scaling Engine
    print("\n--- Price Sanitization & Validation Test ---")
    sample_raw_ioc = [
        ["2026-07-01 09:15:00", 138.50, 140.20, 138.50, 139.80, 45000],
        ["2026-07-01 09:20:00", 139.80, 141.00, 139.60, 140.50, 62000],
    ]
    df_ioc = PriceSanitizerAndScaler.sanitize_candles(sample_raw_ioc, broker="ANGEL")
    is_valid, report = DataSanityValidator.validate_historical_df(df_ioc, "IOC", expected_price_range=(50.0, 500.0))
    print(f"IOC Validation Status: {'✅ PASSED' if is_valid else '❌ FAILED'}")
    if report:
        for r in report:
            print("  -", r)

    # 3. Test Anomaly Detection on Corrupted Instrument Data (Simulated User Error)
    sample_corrupted_ioc = [
        ["2026-07-01 09:15:00", 1676.68, 1690.00, 1670.00, 1685.00, 200],  # Futures token wrongly fetched!
    ]
    df_corrupted = PriceSanitizerAndScaler.sanitize_candles(sample_corrupted_ioc, broker="ANGEL")
    is_valid_c, report_c = DataSanityValidator.validate_historical_df(df_corrupted, "IOC", expected_price_range=(50.0, 500.0))
    print(f"\nCorrupted IOC Validation Status: {'✅ PASSED' if is_valid_c else '❌ FAILED (Caught Error!)'}")
    for r in report_c:
        print("  -", r)
