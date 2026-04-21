import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta

from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from signal_engine import SignalEngine, _session_label

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config from environment ───────────────────────────────────────────────
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
TD_KEY    = os.environ["TWELVEDATA_API_KEY"]
FH_KEY    = os.environ["FINNHUB_API_KEY"]

CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))

# ── Engine singleton ──────────────────────────────────────────────────────
engine = SignalEngine(TD_KEY, FH_KEY)


# ── Helpers ───────────────────────────────────────────────────────────────

def _eat_now() -> str:
    eat = timezone(timedelta(hours=3))
    return datetime.now(eat).strftime("%Y-%m-%d %H:%M EAT")


# ── Command handlers ──────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🥇 *Gold Signal Bot*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Real-time XAU/USD signals using pure price action:\n"
        "S/R levels · HTF cascade · liquidity grabs · session filter\n\n"
        "📋 *Commands*\n"
        "/signal — Full signal with Entry, SL & TP\n"
        "/levels — Key S/R levels map\n"
        "/status — Bot health & last signal\n"
        "/help   — Show this menu\n\n"
        "🔔 *Automatic Alerts*\n"
        "• Signal direction flip (BUY ↔ SELL)\n"
        "• Price at key S/R level\n\n"
        "📐 *Timeframes*\n"
        "D1 (gate) → H4 → H1 → M15\n"
        "━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Analyzing Gold across all timeframes…")
    result = await engine.get_signal()
    await msg.edit_text(result, parse_mode="Markdown")


async def cmd_levels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Fetching S/R levels…")
    result = await engine.get_levels()
    await msg.edit_text(result, parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    last_sig      = engine.last_signal or "None yet"
    active_alerts = len(engine.alerted_levels)

    await update.message.reply_text(
        f"✅ *Bot Status: Online*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Instrument:       XAU/USD\n"
        f"🕐 Time:             `{_eat_now()}`\n"
        f"📍 Session:          {_session_label()}\n"
        f"📊 Last Signal:      `{last_sig}`\n"
        f"🔔 Active S/R Watches: `{active_alerts}`\n"
        f"⏱ Check Interval:   every `{CHECK_INTERVAL_MINUTES} min`\n\n"
        f"📐 *Timeframe Stack*\n"
        f"  D1 (gate) → H4 → H1 → M15\n\n"
        f"🧠 *Scoring Checks*\n"
        f"  HTF cascade · Liquidity grab · Displacement\n"
        f"  S/R touch weight · Session filter · ATR gate\n"
        f"━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
    )


# ── Scheduled alert job ───────────────────────────────────────────────────

async def run_alert_check(bot: Bot):
    """Called by APScheduler every N minutes."""
    logger.info("Running alert check…")
    alerts = await engine.check_alerts()
    for alert in alerts:
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=alert,
                parse_mode="Markdown",
            )
            logger.info("Alert sent.")
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")


# ── Entry point ───────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(CommandHandler("levels", cmd_levels))
    app.add_handler(CommandHandler("status", cmd_status))

    scheduler = AsyncIOScheduler(timezone="Africa/Nairobi")
    scheduler.add_job(
        run_alert_check,
        "interval",
        minutes=CHECK_INTERVAL_MINUTES,
        args=[app.bot],
        id="alert_check",
    )
    scheduler.start()
    logger.info(
        f"Scheduler started — checking every {CHECK_INTERVAL_MINUTES} min (EAT)"
    )

    logger.info("Gold Signal Bot polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
