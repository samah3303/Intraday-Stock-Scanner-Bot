"""
AlphaQuant AI — Send Live Sample Entry Trigger Alert to Telegram
================================================================
Dispatches a sample live "ENTRY TRIGGERED" alert directly to Telegram.
"""

import os
import sys
import time
import requests
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

msg = (
    "⚡ *ALPHAQUANT AI — ENTRY TRIGGERED / EXECUTED ALERT*\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "🔥 *TATAMOTORS-EQ* | *BUY TRADE EXECUTED*\n"
    "📍 *Exact Entry Hit*: ₹994.50 (Time: *09:20:15 AM IST*)\n"
    "🛑 *Stop-Loss (SL)*: ₹984.20 (-1.03%)\n"
    "🎯 *Target 1*: ₹1015.10 | 🎯 *Target 2*: ₹1025.40\n"
    "📊 *Position Executed*: 97 Shares (Capital ₹1L | Max Risk ₹1,000)\n"
    "⏱️ *Status*: ACTIVE INTRADAY TRADE (Monitoring SL/Targets...)\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "✅ _Live Entry Confirmation Notification_"
)

url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

for attempt in range(5):
    try:
        resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=25)
        if resp.ok:
            print("✅ Sample Entry Triggered Message sent successfully to Telegram!")
            break
        else:
            print(f"Attempt {attempt + 1} response: {resp.text}")
    except Exception as exc:
        print(f"Attempt {attempt + 1} error: {exc}")
        time.sleep(2.0)
