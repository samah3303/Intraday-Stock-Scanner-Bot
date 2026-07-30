# ⚡ AlphaQuant Pro 3.0 — Institutional Intraday Engine & DeepSeek Tool Agent

**AlphaQuant Pro 3.0** is an institutional-grade, 100% automated **Intraday Stock Scanner & Trading Engine** built with Python, Flask, Angel One SmartAPI, APScheduler, Telegram Bot API, **Calibrated XGBoost ML**, **IsolationForest Market Anomaly Guard**, **DeepSeek v4 Native Tool Agent**, and **Model Context Protocol (MCP)** server.

The system automatically executes pre-market daily screening at **08:45 AM IST** across all ~2,400 NSE Cash Equity scrips, evaluates 09:20 AM breakouts against a **6-Rule Structural OEL Strategy**, verifies setups through **DeepSeek Tool Agent** and **Calibrated Classifier Probabilities**, logs trade outcomes to a self-learning journal, and streams alerts to Telegram and a **Black & Yellow Glassmorphic Control Panel**.

---

## 🌟 Core Architecture & Capabilities

1. **Dynamic Pre-Market Screening (08:45 AM IST)**:
   - Evaluates the full NSE Cash Equity market (`-EQ`) every morning.
   - Screened bounds: $₹300 \le \text{Price} \le ₹3,000$, $\text{Prev Close} > \text{Prev Open}$ (Green Daily Candle), and $\text{Prev Close} > \text{Daily 20 EMA}$.
   - Selects ~15–25 daily uptrend candidate scrips and dispatches a Telegram pre-market brief.

2. **IsolationForest Market Anomaly Guard (`MarketAnomalyDetector`)**:
   - Machine learning anomaly detector (`sklearn.ensemble.IsolationForest`, contamination=0.05).
   - Detects extreme market crash, flash-crash, or abnormal volatility anomalies and pauses trade signals to safeguard capital.

3. **Multi-Step DeepSeek v4 Tool Agent (`shared/deepseek_agent.py`)**:
   - Native tool-calling agent using DeepSeek API with 4 local tool handlers:
     1. `check_technical_rules` (runs 6-rule check in Python)
     2. `get_sector_performance` (maps symbol to sector & checks relative strength)
     3. `calculate_risk_reward` (computes dynamic entry, SL, T1 1:2, T2 1:3)
     4. `submit_final_decision` (outputs final confidence evaluation score $\ge 75$)

4. **Calibrated XGBoost ML Pipeline (`ml_engine.py`)**:
   - `sklearn.calibration.CalibratedClassifierCV(method="isotonic", cv=5)` wrapping XGBoost.
   - Calibrates raw model confidence scores into true historical win probabilities.

5. **Self-Learning Trade Outcome Journal (`trade_journal.py`)**:
   - `log_trade_outcome()` appends real trade features and outcomes to `trade_outcomes.json`.
   - `retrain_from_history()` runs automatically every **Saturday at 10:00 AM IST** to recalibrate ML model parameters on logged trade outcomes.

6. **Model Context Protocol (MCP) Server (`mcp_server.py`)**:
   - Stdio MCP server exposing 7 tools: `scan_market`, `get_scan_results`, `get_bot_status`, `get_system_logs`, `get_watchlist`, `analyze_stock`, `generate_morning_brief`.

7. **Black & Yellow Glassmorphic Dashboard UI**:
   - High-contrast pitch black & neon yellow dashboard with custom embedded **SVG Falcon Shield logo**, pre-market candidates grid, live signal cards, AI score gauges, and real-time terminal console.

---

## 📅 Daily Automated Schedule (Mon – Sat IST)

```
08:45 AM IST (Mon-Fri) ➔ Pre-Market Screening across full NSE market + DeepSeek Morning Brief
     │
09:20 AM IST (Mon-Fri) ➔ Breakout Scan ➔ Anomaly Guard Check ➔ 6 Rules + DeepSeek AI ➔ Telegram Alerts
     │
15:30 PM IST (Mon-Fri) ➔ Post-Market Review ➔ Generates Telegram Trade Journal Summary
     │
10:00 AM IST (Saturday) ➔ Weekly ML Retrain on trade_outcomes.json
```

---

## ⚙️ Environment Variables (`.env`)

```env
SMARTAPI_API_KEY=your_angel_one_api_key
SMARTAPI_CLIENT_CODE=your_client_code
SMARTAPI_PASSWORD=your_password
SMARTAPI_TOTP_SECRET=your_totp_secret
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
DEEPSEEK_API_KEY=your_deepseek_api_key
```

---

## 🚀 Quick Start & Launch

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run Flask Web Application
python app.py

# 3. Run MCP Stdio Server (for AI Assistants)
python mcp_server.py
```
