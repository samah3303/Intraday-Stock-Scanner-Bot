"""
AlphaQuant Pro 3.0 — Multi-Strategy Module Package
===================================================
Contains modular quantitative strategies for NSE equities:
1. OELBreakoutStrategy (6-Rule Open=Low Breakout)
2. VWAPPullbackStrategy (Intraday VWAP + 20 EMA Rebound)
3. ORB15MinStrategy (15-Min Opening Range Breakout + RVOL)
4. SectorLeaderStrategy (Sector Relative Strength Leader)
"""

from .oel_breakout_strategy import OELBreakoutStrategy
from .vwap_pullback_strategy import VWAPPullbackStrategy
from .orb_15min_strategy import ORB15MinStrategy
from .sector_leader_strategy import SectorLeaderStrategy

STRATEGY_REGISTRY = {
    "OEL_BREAKOUT": OELBreakoutStrategy(),
    "VWAP_PULLBACK": VWAPPullbackStrategy(),
    "ORB_15MIN": ORB15MinStrategy(),
    "SECTOR_LEADER": SectorLeaderStrategy(),
}

def evaluate_all_strategies(symbol: str, df_5min: object, nifty_bullish: bool = True) -> list[dict]:
    """Runs all registered strategies on a scrip's 5-minute data and returns matching setup signals."""
    matches = []
    for strat_id, strat_obj in STRATEGY_REGISTRY.items():
        try:
            res = strat_obj.evaluate(symbol, df_5min, nifty_bullish)
            if res:
                res["strategy_id"] = strat_id
                matches.append(res)
        except Exception:
            pass
    return matches
