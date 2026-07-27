"""
Send sample DeepSeek AI Structural OEL intraday strategy alert to Telegram group.
Run: python test_telegram.py
"""
import os
import sys
import time
from datetime import datetime
from dotenv import load_dotenv
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

token = os.getenv("TELEGRAM_BOT_TOKEN")
chat_id = os.getenv("TELEGRAM_CHAT_ID")
url = f"https://api.telegram.org/bot{token}/sendMessage"
today = datetime.now().strftime("%d %b %Y %H:%M IST")


def send(msg):
    if not token or not chat_id:
        print("[FAIL] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing in .env")
        return False
    resp = requests.post(url, json={
        "chat_id": chat_id, "text": msg, "parse_mode": "Markdown",
    }, timeout=10)
    return resp.ok


# ── Sample DeepSeek AI Telegram Alert ─────────────────────────────────
msg = f"""🎯 *DEEPSEEK AI MATCHING INTRADAY STOCKS*
📅 *{today}*

• *RELIANCE* 🔥 [HIGH] AI Score: *88/100*
  O:₹2872.00 H:₹2891.50 L:₹2872.00 C:₹2889.35 | EMA20:₹2865.42
  📍 *Entry*: ₹2889.35 | 🛑 *SL*: ₹2866.25
  🎯 *T1*: ₹2935.55 | 🎯 *T2*: ₹2958.65
  💡 _Strong 09:15 Open=Low base with 11.4% upper wick rejection and 20 EMA alignment._

• *INFY* ⚡ [MED] AI Score: *74/100*
  O:₹1520.00 H:₹1538.00 L:₹1520.00 C:₹1535.50 | EMA20:₹1515.10
  📍 *Entry*: ₹1535.50 | 🛑 *SL*: ₹1516.96
  🎯 *T1*: ₹1572.58 | 🎯 *T2*: ₹1591.12
  💡 _Bullish continuation above 20 EMA with structural low support._

✅ *2 stock(s)* passed 6 rules + AI Evaluation.
_Universe scanned: Custom Watchlist (2 stocks)_"""

print(f"Sending DeepSeek AI sample alert to Telegram chat/group ({chat_id})...")
if send(msg):
    print("[OK] DeepSeek AI sample alert sent successfully to Telegram!")
else:
    print("[FAIL] Telegram alert delivery failed.")
