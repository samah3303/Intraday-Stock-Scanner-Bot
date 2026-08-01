"""
AlphaQuant AI — Shared Constants & Default Configuration
=========================================================
Single source of truth for stock universe, price bounds,
Nifty token, and other system-wide configuration values.
"""

# ── Nifty 50 Index Token ──────────────────────────────────────────────
NIFTY_TOKEN = "99926000"

# ── Price Universe Bounds (Strict ₹300–₹3,000 range to eliminate penny stock noise) ──
MIN_STOCK_PRICE = 300.0
MAX_STOCK_PRICE = 3000.0

# ── Scan Parameters ───────────────────────────────────────────────────
MAX_SCAN_STOCKS = 750          # Max NSE stocks to scan per run
OPEN_LOW_TOLERANCE = 0.0005    # 0.05% max delta between Open and Low
DEFAULT_SLIPPAGE_PCT = 0.0005  # 0.05% market order slippage
DEFAULT_CAPITAL = 100000.0     # Default account capital (₹1,00,000)
DEFAULT_RISK_PCT = 0.01        # 1% risk per trade

# ── Optimized Strategy Parameters (from 3-year backtest) ────────────────
OPT_RVOL_MIN = 1.5             # Minimum relative volume for breakout confirmation
OPT_WICK_MAX_PCT = 50.0        # Maximum upper wick rejection percentage
OPT_AI_MIN_SCORE = 75          # Minimum DeepSeek AI confidence score
OPT_SECTOR_GUARD_ENABLED = True  # Block trades when sector index is weak

# ── Sector Relative Strength Guard ───────────────────────────────────────
# Sectors with win rate < 45% in backtest are blocked
WEAK_SECTORS = []  # Populated after backtest optimization (e.g., ["METALS", "REALTY"])

# ── 100 Top Liquid Bullish NSE Stock Universe ─────────────────────────
DEFAULT_100_STOCKS = [
    "ABB", "ADANIENT", "ADANIPORTS", "AMBUJACEM", "APOLLOHOSP", "ASIANPAINT", "AUBANK", "AXISBANK",
    "BAJAJ-AUTO", "BAJAJFINSV", "BAJFINANCE", "BANKBARODA", "BEL", "BERGEPAINT", "BHARATFORG", "BHARTIARTL",
    "BPCL", "BRITANNIA", "CANBK", "CGPOWER", "CHOLAFIN", "CIPLA", "COALINDIA", "COLPAL",
    "CONCOR", "CUMMINSIND", "DIVISLAB", "DLF", "DRREDDY", "EICHERMOT", "ETERNAL", "FEDERALBNK",
    "GAIL", "GODREJPROP", "GRASIM", "HAL", "HAVELLS", "HCLTECH", "HDFCBANK", "HEROMOTOCO",
    "HINDALCO", "ICICIBANK", "ICICIPRULI", "IDFCFIRSTB", "INDIGO", "INDUSINDBK", "INFY", "IOC",
    "IRCTC", "JINDALSTEL", "JSWSTEEL", "JUBLFOOD", "KOTAKBANK", "LT", "M&M", "MANAPPURAM",
    "MARUTI", "MAXHEALTH", "MOTHERSON", "MUTHOOTFIN", "NAUKRI", "NESTLEIND", "NTPC", "OFSS",
    "ONGC", "PERSISTENT", "PFC", "PIDILITIND", "PNB", "POLYCAB", "POWERGRID", "RECLTD",
    "RELIANCE", "SBIN", "SHRIRAMFIN", "SIEMENS", "SRF", "SUNPHARMA", "TATACOMM", "TATACONSUM",
    "TATAELXSI", "TATAPOWER", "TATASTEEL", "TATATECH", "TCS", "TECHM", "TITAN", "TORNTPHARM",
    "TRENT", "TVSMOTOR", "ULTRACEMCO", "VBL", "VOLTAS", "WESTLIFE", "WIPRO"
]
