"""
Test script for DeepSeek AI Stock Analysis Integration.
Run: python test_deepseek.py
"""
import os
import sys
import json
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

from shared.deepseek_analyzer import analyze_hit_with_deepseek


if __name__ == "__main__":
    sample_hit = {
        "symbol": "TATAMOTORS",
        "open": 980.00,
        "high": 995.50,
        "low": 980.00,
        "close": 992.10,
        "ema20": 975.40,
        "wick_pct": 12.5,
    }
    
    print("Testing DeepSeek AI analysis module with sample ticker TATAMOTORS...")
    result = analyze_hit_with_deepseek(sample_hit, nifty_bullish=True)
    print("\n--- DeepSeek AI Evaluation Result ---")
    print(json.dumps(result, indent=2))
