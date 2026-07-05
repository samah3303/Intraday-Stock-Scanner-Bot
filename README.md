<div align="center">

# 📡 JABIR — Intraday Stock Scanner Bot

**Automated intraday strategy scanner powered by Angel One SmartAPI, with Telegram alerts and a live control panel.**

[![Python](https://img.shields.io/badge/Python-3.10+-3776ab?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-3.x-000000?style=flat-square&logo=flask)](https://flask.palletsprojects.com)
[![Render](https://img.shields.io/badge/Deploy-Render-46e3b7?style=flat-square&logo=render)](https://render.com)
[![License](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)](LICENSE)

</div>

---

## Overview

**JABIR** is a lightweight, production-ready Python web application that automates an intraday stock scanning strategy on the Indian equity market. It connects to **Angel One's SmartAPI**, runs a rule-based technical filter on the first 5-minute candle of the trading day, and pushes actionable alerts directly to **Telegram**.

Designed to be deployed on **Render** (free tier compatible), it runs fully hands-free — logging in automatically before market open, scanning at 09:18 AM IST, and keeping itself alive via a health-check endpoint.

---

## ✨ Features

| Feature | Description |
|---|---|
| 🔐 **Auto Login** | TOTP-based 2FA login to Angel One at 08:45 AM IST every trading day |
| 📊 **6-Rule Strategy Engine** | Open=Low alignment, bullish body, wick filter, price range, Nifty trend, and 20-EMA confirmation |
| 📲 **Telegram Alerts** | Instant push notifications with scan results in clean markdown |
| 🖥️ **Dark-Themed Dashboard** | Glassmorphic control panel with live bot status, action buttons, and server info |
| ⏱️ **Background Scheduler** | Non-blocking APScheduler cron jobs for login and scan routines |
| 💚 **Health Check** | `/healthz` endpoint for uptime monitoring and Render keep-alive pings |

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Render (Cloud)                        │
│                                                         │
│  ┌───────────┐    ┌──────────────┐    ┌──────────────┐  │
│  │  Gunicorn  │───▶│   Flask App   │───▶│  APScheduler │  │
│  │  (wsgi.py) │    │   (app.py)    │    │  (Background)│  │
│  └───────────┘    └──────┬───────┘    └──────┬───────┘  │
│                          │                    │          │
│                   ┌──────▼───────┐    ┌──────▼───────┐  │
│                   │  Control UI   │    │  Cron Jobs   │  │
│                   │  / /start     │    │  08:45 Login │  │
│                   │  / /stop      │    │  09:18 Scan  │  │
│                   │  / /healthz   │    └──────┬───────┘  │
│                   └──────────────┘           │          │
└──────────────────────────────────────────────┼──────────┘
                                               │
                        ┌──────────────────────┼────────┐
                        ▼                      ▼        │
                 ┌─────────────┐       ┌────────────┐   │
                 │  Angel One   │       │  Telegram   │   │
                 │  SmartAPI    │       │  Bot API    │   │
                 └─────────────┘       └────────────┘   │
                                                        │
```

---

## 📋 Strategy Rules

The scanner evaluates the **first 5-minute candle (09:15 AM)** of each stock against six filters:

| # | Rule | Condition |
|---|---|---|
| 1 | **Open = Low & Structural Support** | Today's Open == Today's Low **AND** Open == Previous day's last candle Low |
| 2 | **Bullish Candle Body** | Close > Open |
| 3 | **Upper Wick Filter** | Upper Wick ≤ 50% of Candle Range |
| 4 | **Price Range** | 300 ≤ Close ≤ 3000 |
| 5 | **Market Trend (Nifty 50)** | Nifty 09:15 Close > Nifty 09:15 Open |
| 6 | **20-EMA Confirmation** | Close > 20-period EMA (computed over yesterday + today) |

A stock must pass **all six rules** to trigger an alert.

---

## 📁 Project Structure

```
JABIR/
├── app.py              # Core application: routes, scheduler, strategy logic
├── wsgi.py             # Gunicorn WSGI entry point
├── requirements.txt    # Python dependencies
├── .env                # Environment variables (local dev only, git-ignored)
├── .gitignore          # Git ignore rules
└── README.md           # This file
```

---

## ⚙️ Environment Variables

| Variable | Description |
|---|---|
| `ANGEL_API_KEY` | Angel One SmartAPI key |
| `ANGEL_CLIENT_CODE` | Your Angel One client/user ID |
| `ANGEL_PASSWORD` | Angel One login password |
| `ANGEL_TOTP_KEY` | Base32 secret for TOTP 2FA generation |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token (from [@BotFather](https://t.me/BotFather)) |
| `TELEGRAM_CHAT_ID` | Target Telegram chat ID (from [@userinfobot](https://t.me/userinfobot)) |

> **⚠️ Never commit your `.env` file.** It is already listed in `.gitignore`.

---

## 🚀 Quick Start (Local Development)

### 1. Clone the repository

```bash
git clone https://github.com/<your-username>/JABIR.git
cd JABIR
```

### 2. Create a virtual environment

```bash
python -m venv venv
source venv/bin/activate    # Linux/Mac
venv\Scripts\activate       # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

Copy the template and fill in your credentials:

```bash
cp .env.example .env
# Edit .env with your actual values
```

Or create a `.env` file manually:

```env
ANGEL_API_KEY=your_api_key
ANGEL_CLIENT_CODE=your_client_code
ANGEL_PASSWORD=your_password
ANGEL_TOTP_KEY=your_totp_secret
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

### 5. Run the app

```bash
python wsgi.py
```

Open [http://localhost:5000](http://localhost:5000) to see the control panel.

---

## ☁️ Deploy to Render

### 1. Push to GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/<your-username>/JABIR.git
git push -u origin main
```

### 2. Create a Web Service on Render

1. Go to [render.com](https://render.com) → **New → Web Service**
2. Connect your GitHub repository
3. Configure the service:

| Setting | Value |
|---|---|
| **Name** | `jabir-scanner` |
| **Runtime** | Python |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 1 --threads 2` |
| **Plan** | Free |

### 3. Add environment variables

In the Render dashboard, go to **Environment** and add all six variables listed above.

### 4. Keep alive (Free Tier)

Render's free tier spins down after 15 minutes of inactivity. Use an external ping service to hit your `/healthz` endpoint every 5 minutes:

- [UptimeRobot](https://uptimerobot.com) (free, 5-min interval)
- [Cron-Job.org](https://cron-job.org) (free, 1-min interval)

Set the monitor URL to:
```
https://jabir-scanner.onrender.com/healthz
```

---

## 🔌 API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Control panel dashboard |
| `GET` | `/start` | Manually trigger Angel One login |
| `GET` | `/stop` | Stop the bot motor and alert Telegram |
| `GET` | `/healthz` | Health check → `{"status": "healthy"}` |

---

## 🛠️ Customization

### Adding more stocks

Edit the `STOCK_TOKENS` dictionary in [app.py](app.py):

```python
STOCK_TOKENS = {
    "INFY": "1594",
    "RELIANCE": "2885",
    "SBIN": "3045",
    "TCS": "11536",       # Add new stocks here
    "HDFCBANK": "1333",
}
```

> **Tip:** Find symbol tokens from the [Angel One instrument master](https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json).

### Changing scan time

Modify the cron trigger in [app.py](app.py):

```python
scheduler.add_job(
    run_strategy_scan,
    CronTrigger(day_of_week="mon-fri", hour=9, minute=18,  # Change time here
                timezone="Asia/Kolkata"),
    ...
)
```

---

## 📝 License

This project is open-source and available under the [MIT License](LICENSE).

---

<div align="center">

**Built with ❤️ for Indian Markets**

[Angel One SmartAPI](https://smartapi.angelone.in/) · [Flask](https://flask.palletsprojects.com/) · [Telegram Bot API](https://core.telegram.org/bots/api)

</div>
