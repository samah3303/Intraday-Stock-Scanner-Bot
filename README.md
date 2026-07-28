# 📈 AlphaQuant AI — Quantitative Intraday Stock Scanner with DeepSeek & ML Engine

A high-performance automated **Intraday Stock Scanner** built with Python, Flask, Angel One SmartAPI, APScheduler, Telegram Bot API, **XGBoost Quantitative Machine Learning Engine**, and **DeepSeek AI Reasoning Engine**.

The system automatically authenticates with Angel One via TOTP 2FA, fetches live 5-minute candle data, scans top NSE universe stocks against a **6-Rule Structural OEL Strategy**, evaluates candidate setups through **AlphaQuant Machine Learning & DeepSeek AI**, and broadcasts high-probability trade signals to Telegram channels and a glassmorphic web control panel.

---

## 🌟 Key Features

- **6-Rule Structural OEL Strategy**: Mechanical technical filtering at 09:20 AM IST.
- **DeepSeek AI Reasoning Engine**: Evaluates matching candidates to assign a **Confidence Score (0-100)**, **Buy Limit Entry**, **Stop Loss (SL)**, **Target 1 (1:2 R:R)**, **Target 2 (1:3 R:R)**, and **AI Technical Justifications**.
- **Quantitative ML Pipeline (`ml_engine.py`)**: XGBoost & LightGBM classification model filtering trades with probability score $\ge 0.75$ using relative volume (RVOL), gap %, Level 2 orderbook imbalance, and Nifty momentum.
- **Dynamic ATR Risk Management**: Replaces static R:R ratios with asset-specific ATR volatility sizing and dynamic position allocation.
- **Telegram Group Alerts**: Markdown-formatted trade signals sent directly to your Telegram trading channel.
- **Glassmorphic Web Control Panel & Live Logs**: Modern UI displaying real-time matching tickers, AI score badges, custom watchlist manager, and auto-refreshing system log terminal.

---

## 📋 Filter Strategy Rules (6/6 Technical + DeepSeek & ML)

A stock ticker must meet **all baseline conditions** at 09:20 AM IST to be evaluated:

| # | Rule | Filter Logic & Formula |
|---|---|---|
| 1 | **Open = Low & Support Alignment** | Today's 09:15 Open == Today's 09:15 Low **AND** Today's 09:15 Open == Previous Day's Last 5-minute Low |
| 2 | **Bullish Candle Body** | Today's 09:15 Close > Today's 09:15 Open |
| 3 | **Minimal Upper Rejection** | Upper Wick `(High - Close)` ≤ `50%` of Candle Range `(High - Low)` |
| 4 | **Price Universe Range** | `300 ≤ Close Price ≤ 3000` |
| 5 | **Market Trend Alignment** | Nifty 50 Index 09:15 Close > Nifty 50 Index 09:15 Open |
| 6 | **Trend Confirmation** | Today's 09:15 Close > 20-period EMA on 5-minute chart |
| 🧠 7 | **DeepSeek & ML Risk Engine** | Quality scoring (0-100), dynamic Buy Limit, structural SL, 1:2 & 1:3 R:R targets, and AI justification |

---

## 🧠 AlphaQuant AI Trade Signals

Every stock passing structural rules is evaluated by DeepSeek AI & XGBoost:

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
ANGEL_PASSWORD=your_4_digit_mpin
ANGEL_MPIN=your_4_digit_mpin
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

# Run ML & DeepSeek tests
python ml_engine.py
python test_deepseek.py
python test_telegram.py

# Launch Flask Web Server
python app.py
```

Open [http://localhost:5000](http://localhost:5000) to view the live dashboard.
