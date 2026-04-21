# 🥇 Gold Signal Bot

Telegram bot for real-time XAU/USD trading signals.
Detects key Support & Resistance levels, scores multi-timeframe confluence,
and pushes automatic alerts on signal flips and S/R touches.

---

## Features

| Feature | Detail |
|---|---|
| Signal | BUY / SELL / HOLD with Entry, SL, TP |
| Confluence | 3-timeframe scoring (D1, H4, H1) |
| S/R Detection | Swing high/low clustering across D1 + H4 |
| Auto Alerts | Signal direction flip (BUY↔SELL) |
| Auto Alerts | Price within 0.3% of key S/R level |
| On-demand | /signal, /levels, /status commands |
| Data | Twelve Data (primary) + Finnhub (live price) |

---

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Welcome + command list |
| `/signal` | Full signal with Entry / SL / TP |
| `/levels` | Key S/R levels map above and below price |
| `/status` | Bot health, last signal, active watches |
| `/help` | Re-show menu |

---

## Deploy to Railway

### 1. Create a Telegram Bot
1. Open Telegram → search `@BotFather`
2. `/newbot` → follow prompts → copy the **Bot Token**
3. Start a chat with your bot, then visit:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
4. Send any message to the bot, refresh the URL — copy the `"id"` value inside `"chat"` — that is your **Chat ID**

### 2. Push to GitHub
```bash
git init
git add .
git commit -m "init: gold signal bot"
git remote add origin https://github.com/YOUR_USERNAME/gold-signal-bot.git
git push -u origin main
```

### 3. Deploy on Railway
1. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
2. Select your repo
3. Go to your service → **Variables** tab → add all 4 (or 5) variables:

```
TELEGRAM_BOT_TOKEN    = <from BotFather>
TELEGRAM_CHAT_ID      = <your chat ID>
TWELVEDATA_API_KEY    = <from twelvedata.com>
FINNHUB_API_KEY       = <from finnhub.io>
CHECK_INTERVAL_MINUTES = 5   (optional, default 5)
```

4. Railway auto-detects `railway.toml` and runs `python main.py`
5. Watch logs — you should see:
   ```
   Scheduler started — checking every 5 min (EAT)
   Gold Signal Bot polling…
   ```
6. Send `/start` to your bot in Telegram ✅

---

## Project Structure

```
gold_signal_bot/
├── main.py           # Telegram bot + APScheduler
├── signal_engine.py  # S/R detection, scoring, alert logic
├── data_fetcher.py   # Twelve Data + Finnhub API calls
├── requirements.txt
├── railway.toml
└── .env.example
```

---

## Tuning

| Variable | File | Default | Description |
|---|---|---|---|
| `SR_SWING_N` | signal_engine.py | 5 | Candles each side for swing detection |
| `SR_ALERT_PCT` | signal_engine.py | 0.003 | 0.3% proximity to trigger S/R alert |
| `ATR_SL_MULT` | signal_engine.py | 1.5 | SL distance = ATR × multiplier |
| `TP_RR` | signal_engine.py | 2.0 | Take Profit R:R ratio |
| `CHECK_INTERVAL_MINUTES` | .env | 5 | How often alerts are checked |
