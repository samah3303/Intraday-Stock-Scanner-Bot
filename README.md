# 📈 JABIR — Angel One Intraday Stock Scanner with DeepSeek AI Engine

A high-performance automated **Intraday Stock Scanner** built with Python, Flask, Angel One SmartAPI, APScheduler, Telegram Bot API, and **DeepSeek AI Reasoning Engine**.

The bot automatically logs into Angel One using TOTP 2FA, fetches live 5-minute candle data, scans top NSE universe stocks against a **6-Rule Structural OEL Strategy**, evaluates candidate setups through **DeepSeek AI**, and broadcasts actionable trade signals directly to Telegram group chats and a web dashboard UI.

---

## 🌟 Key Features

- **6-Rule Structural OEL Filter Strategy**: Mechanical technical filtering at 09:20 AM IST.
- **DeepSeek AI Reasoning & Risk Engine**: Evaluates matching candidates to assign a **Confidence Score (0-100)**, **Buy Limit Entry**, **Stop Loss (SL)**, **Target 1 (1:2 R:R)**, **Target 2 (1:3 R:R)**, and concise **AI Trade Justification**.
- **Telegram Group Alerts**: Markdown-formatted alerts sent directly to your Telegram trading channel.
- **Glassmorphic Web Control Panel Dashboard**: Modern UI showing real-time stock ticker lists, AI score badges, trade plans, and custom watchlist manager.
- **Resilient AI Fallback**: Operates smoothly with or without `DEEPSEEK_API_KEY` configured.

---

## 📋 Filter Strategy Rules (6/6)

A stock ticker must meet **all six conditions** at 09:20 AM IST to be flagged:

| # | Rule | Filter Logic & Formula |
|---|---|---|
| 1 | **Open = Low & Support Alignment** | Today's 09:15 Open == Today's 09:15 Low **AND** Today's 09:15 Open == Previous Day's Last 5-minute Low |
| 2 | **Bullish Candle Body** | Today's 09:15 Close > Today's 09:15 Open |
| 3 | **Minimal Upper Rejection** | Upper Wick `(High - Close)` ≤ `50%` of Candle Range `(High - Low)` |
| 4 | **Price Universe Range** | `300 ≤ Close Price ≤ 3000` |
| 5 | **Market Trend Alignment** | Nifty 50 Index 09:15 Close > Nifty 50 Index 09:15 Open |
| 6 | **Trend Confirmation** | Today's 09:15 Close > 20-period EMA on 5-minute chart |

---

## 🧠 DeepSeek AI Trade Plan

Every stock passing the 6 structural rules is submitted to DeepSeek AI:

```
• RELIANCE 🔥 [HIGH] AI Score: 88/100
  O:₹2872.00 H:₹2891.50 L:₹2872.00 C:₹2889.35 | EMA20:₹2865.42
  📍 Entry: ₹2889.35 | 🛑 SL: ₹2866.25
  🎯 T1: ₹2935.55 | 🎯 T2: ₹2958.65
  💡 Strong 09:15 Open=Low base with 11.4% upper wick rejection and 20 EMA alignment.
```

---

## ⚙️ Environment Variables (`.env`)

```env
# Angel One SmartAPI Credentials
ANGEL_API_KEY=your_angel_api_key
ANGEL_CLIENT_CODE=your_client_code
ANGEL_PASSWORD=your_password
ANGEL_TOTP_KEY=your_totp_secret_key

# Telegram Bot
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_group_chat_id

# DeepSeek AI API Key (Optional)
DEEPSEEK_API_KEY=your_deepseek_api_key
```

---

## 🚀 Local Setup & Run

```bash
# Install dependencies
pip install -r requirements.txt

# Run testing scripts
python test_deepseek.py
python test_telegram.py

# Launch Flask Web Server
python app.py
```

Open [http://localhost:5000](http://localhost:5000) to view the live dashboard.
