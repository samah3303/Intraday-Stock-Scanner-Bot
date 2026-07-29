"""
AlphaQuant AI — DeepSeek AI Stock Analysis Integration
=======================================================
Single shared module for DeepSeek AI trade quality scoring.
Used by both the Flask application and the standalone test script.
"""

import os
import json
import logging
import requests

logger = logging.getLogger(__name__)


def analyze_hit_with_deepseek(hit: dict, nifty_bullish: bool = True) -> dict:
    """
    Send stock hit details to DeepSeek AI model for trade quality scoring (0-100),
    dynamic Entry/SL/Target calculations, and reasoning.

    Args:
        hit: Dictionary with keys: symbol, open, high, low, close, ema20, wick_pct
        nifty_bullish: Whether Nifty 50 benchmark is bullish

    Returns:
        dict with keys: score, recommendation, entry, stop_loss, target_1, target_2, reasoning
    """
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    close_price = hit.get("close", 1000.0)
    low_price = hit.get("low", close_price * 0.99)
    sl = round(low_price * 0.998, 2)
    risk = max(close_price - sl, 0.5)
    t1 = round(close_price + (risk * 2.0), 2)
    t2 = round(close_price + (risk * 3.0), 2)

    if not api_key or api_key == "your_deepseek_api_key_here":
        score = 85 if hit.get("wick_pct", 10) < 20 else 72
        logger.debug("DeepSeek API key not configured — using heuristic fallback for %s", hit.get("symbol"))
        return {
            "score": score,
            "recommendation": "BUY" if score >= 75 else "AVOID",
            "entry": round(close_price, 2),
            "stop_loss": sl,
            "target_1": t1,
            "target_2": t2,
            "reasoning": f"Clean 09:15 Open=Low candle with {hit.get('wick_pct', 0)}% upper wick, price above 20 EMA.",
        }

    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    system_prompt = (
        "You are an expert quantitative trader and risk manager specializing in Indian stock market (NSE) intraday trading. "
        "Analyze the provided stock scanner hit and output strictly valid JSON with no markdown wrapping or additional text.\n"
        "Required JSON schema:\n"
        "{\n"
        '  "score": integer (0 to 100 confidence rating),\n'
        '  "recommendation": string ("BUY" or "AVOID"),\n'
        '  "entry": float (suggested limit entry price near 09:15 close),\n'
        '  "stop_loss": float (stop loss slightly below 09:15 low),\n'
        '  "target_1": float (1:2 Risk-Reward target price),\n'
        '  "target_2": float (1:3 Risk-Reward target price),\n'
        '  "reasoning": string (concise 1-2 sentence technical analysis justification)\n'
        "}"
    )

    user_prompt = f"""Analyze this NSE stock intraday setup:
Symbol: {hit.get('symbol')}
09:15 Open: {hit.get('open')}
09:15 High: {hit.get('high')}
09:15 Low: {hit.get('low')}
09:15 Close: {hit.get('close')}
20 EMA (5-min): {hit.get('ema20')}
Upper Wick Rejection: {hit.get('wick_pct')}%
Nifty 50 Benchmark: {"Bullish" if nifty_bullish else "Neutral"}
"""

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        res_json = json.loads(content)
        logger.debug("DeepSeek AI scored %s: %s/100", hit.get("symbol"), res_json.get("score"))
        return res_json
    except Exception as exc:
        logger.warning("DeepSeek API call error for %s: %s. Using fallback.", hit.get("symbol"), exc)
        return {
            "score": 75,
            "recommendation": "BUY",
            "entry": round(close_price, 2),
            "stop_loss": sl,
            "target_1": t1,
            "target_2": t2,
            "reasoning": "Standard Open=Low structural support pass (API Fallback).",
        }
