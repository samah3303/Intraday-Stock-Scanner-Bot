"""
Send sample dual-strategy alerts to Telegram.
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
today = datetime.now().strftime("%d %b %Y")


def send(msg):
    resp = requests.post(url, json={
        "chat_id": chat_id, "text": msg, "parse_mode": "Markdown",
    }, timeout=10)
    return resp.ok


# ── Strategy A — Structural OEL ──────────────────────────────────────
msg_a = f"""🔍 *Strategy A — Structural OEL*
📅 *{today}*

• *RELIANCE* | O:2872.00 H:2891.50 L:2872.00 C:2889.35 | EMA20:2865.42

_6-Rule Filter: Open=Low+Support | Bullish | Wick≤50% | ₹300-3K | Nifty↑ | >20EMA_"""

print("Sending Strategy A alert...")
if send(msg_a):
    print("[OK] Strategy A sent!")
else:
    print("[FAIL] Strategy A failed.")

time.sleep(1)

# ── Strategy B — OEL Breakout ─────────────────────────────────────────
msg_b = f"""📊 *Strategy B — OEL Breakout*
📅 *{today}*
_Open=Low First Candle Breakout | R:R 1:1.43_

• *GOCOLORS* | Entry: 405.00 | SL: 388.82 (−4.0%) | TGT: 428.14 | R:R 1:1.43
• *CARTRADE* | Entry: 2067.40 | SL: 2026.04 (−2.0%) | TGT: 2126.55 | R:R 1:1.43
• *EMSLIMITED* | Entry: 429.60 | SL: 418.30 (−2.6%) | TGT: 445.76 | R:R 1:1.43
• *NIACL* | Entry: 192.00 | SL: 185.80 (−3.2%) | TGT: 200.86 | R:R 1:1.43
• *QUADFUTURE* | Entry: 416.80 | SL: 405.10 (−2.8%) | TGT: 433.52 | R:R 1:1.43

✅ *5 stocks* passed filters (scanned 1247)"""

print("Sending Strategy B alert...")
if send(msg_b):
    print("[OK] Strategy B sent!")
else:
    print("[FAIL] Strategy B failed.")

print("\nDone — check your Telegram for both alerts!")
