"""
main.py — Gold Signal Bot
Telegram bot + APScheduler alert engine. Railway-ready.
"""

import asyncio
import logging
import os
import time
import traceback
from datetime import datetime, timezone, timedelta

from telegram import Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from signal_engine import SignalEngine, _current_session

# ══════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# STARTUP VALIDATION — fail fast, not mid-run
# ══════════════════════════════════════════════════════════════════════
def _load_env() -> dict:
    required = {
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
        "TELEGRAM_CHAT_ID":   os.getenv("TELEGRAM_CHAT_ID"),
        "TWELVEDATA_API_KEY": os.getenv("TWELVEDATA_API_KEY"),
        "FINNHUB_API_KEY":    os.getenv("FINNHUB_API_KEY"),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise EnvironmentError(
            f"Missing env vars: {', '.join(missing)}\n"
            "Set them in Railway → service → Variables."
        )
    try:
        chat_id = int(required["TELEGRAM_CHAT_ID"])
    except ValueError:
        raise EnvironmentError(
            f"TELEGRAM_CHAT_ID must be an integer, got: {required['TELEGRAM_CHAT_ID']!r}"
        )
    return {
        "bot_token": required["TELEGRAM_BOT_TOKEN"],
        "chat_id":   chat_id,
        "td_key":    required["TWELVEDATA_API_KEY"],
        "fh_key":    required["FINNHUB_API_KEY"],
        "interval":  int(os.getenv("CHECK_INTERVAL_MINUTES", "5")),
    }


ENV    = _load_env()
engine = SignalEngine(ENV["td_key"], ENV["fh_key"])


# ══════════════════════════════════════════════════════════════════════
# AUTHORIZATION — silently drop requests from unknown chat IDs
# ══════════════════════════════════════════════════════════════════════
def _authorized(update: Update) -> bool:
    uid = update.effective_chat.id if update.effective_chat else None
    if uid != ENV["chat_id"]:
        logger.warning(f"Unauthorized access attempt from chat_id={uid}")
        return False
    return True


# ══════════════════════════════════════════════════════════════════════
# RATE LIMITER — prevents spam-firing expensive API calls
# ══════════════════════════════════════════════════════════════════════
_last_call: dict = {}
_COOLDOWNS = {"signal": 30, "levels": 20, "status": 10, "chart": 45}

def _rate_limited(user_id: int, command: str) -> int:
    key  = f"{user_id}:{command}"
    now  = time.monotonic()
    wait = _COOLDOWNS.get(command, 0)
    last = _last_call.get(key, 0)
    if now - last < wait:
        return int(wait - (now - last))
    _last_call[key] = now
    return 0


# ══════════════════════════════════════════════════════════════════════
# SAFE EDIT HELPER
# ══════════════════════════════════════════════════════════════════════
async def _safe_edit(msg, text: str):
    try:
        await msg.edit_text(text, parse_mode="Markdown")
    except Exception:
        try:
            await msg.edit_text(text)
        except Exception as e:
            logger.error(f"Failed to edit message: {e}")


# ══════════════════════════════════════════════════════════════════════
# COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update): return
    await update.message.reply_text(
        "🥇 *Gold Signal Bot*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Real-time XAU/USD — prediction + confirmation.\n\n"
        "📋 *Commands*\n"
        "/signal — Full signal with entry, SL & TP\n"
        "/levels — Key S/R levels map\n"
        "/chart — Marked-up H4 chart  |  /chart h1\n"
        "/status — Bot health & config\n"
        "/help   — Show this menu\n\n"
        "🔔 *Auto Alerts (3 phases)*\n"
        "👀 Phase 0 — Level approaching (before price arrives)\n"
        "📈 Phase 1 — H4 structural signal flip\n"
        "⚡ Phase 2 — M15 entry confirmation (as move begins)\n"
        "━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)

async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update): return
    wait = _rate_limited(update.effective_user.id, "signal")
    if wait:
        await update.message.reply_text(f"⏱ Wait `{wait}s` before requesting again.", parse_mode="Markdown")
        return
    msg = await update.message.reply_text("⏳ Analyzing Gold…")
    await _safe_edit(msg, await engine.get_signal())

async def cmd_levels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update): return
    wait = _rate_limited(update.effective_user.id, "levels")
    if wait:
        await update.message.reply_text(f"⏱ Wait `{wait}s`.", parse_mode="Markdown")
        return
    msg = await update.message.reply_text("⏳ Fetching levels…")
    await _safe_edit(msg, await engine.get_levels())

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update): return
    wait = _rate_limited(update.effective_user.id, "status")
    if wait:
        await update.message.reply_text(f"⏱ Wait `{wait}s`.", parse_mode="Markdown")
        return

    eat = timezone(timedelta(hours=3))
    now = datetime.now(eat).strftime("%Y-%m-%d %H:%M EAT")
    cfg = engine.cfg

    await update.message.reply_text(
        f"✅ *Bot Status: Online*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Instrument:          XAU/USD\n"
        f"🕐 Time:                `{now}`\n"
        f"📊 Last H4 Signal:      `{engine.last_signal or 'None yet'}`\n"
        f"🔔 S/R Watches:         `{len(engine.alerted_levels)}`\n"
        f"👀 Approach Watches:    `{len(engine.approach_alerted)}`\n"
        f"⚡ Entry Watches:       `{len(engine.entry_alerted)}`\n"
        f"🔁 Flips today:         `{engine.daily_flip_count}/{cfg.daily_flip_limit}`\n"
        f"🕐 Session:             `{_current_session()}`\n"
        f"⏱ Check interval:      every `{ENV['interval']} min`\n\n"
        f"⚙️ *Config*\n"
        f"  ATR SL mult:         `{cfg.atr_sl_mult}×`\n"
        f"  TP R:R:               `1:{cfg.tp_rr}`\n"
        f"  Bias threshold:      `{cfg.bias_threshold}`\n"
        f"  Combined score min:  `{cfg.combined_score_min}`\n"
        f"  S/R alert zone:      `{cfg.sr_alert_pct*100:.1f}%`\n"
        f"  Approach warn:       `{cfg.approach_warn_pct*100:.1f}%`\n"
        f"  M15 wick ratio:      `{cfg.m15_rejection_wick_ratio:.0%}`\n"
        f"  News blackout:       `±{cfg.news_blackout_minutes}min`\n"
        f"━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
    )

async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update): return
    wait = _rate_limited(update.effective_user.id, "chart")
    if wait:
        await update.message.reply_text(f"⏱ Wait `{wait}s` before requesting again.", parse_mode="Markdown")
        return

    # Parse optional timeframe arg: /chart h1
    args = context.args
    tf = "1h" if args and args[0].lower() in ("h1", "1h") else "4h"
    tf_label = "H1" if tf == "1h" else "H4"

    msg = await update.message.reply_text(f"⏳ Generating {tf_label} chart…")
    buf, caption = await engine.get_chart(tf)

    if buf is None:
        await _safe_edit(msg, caption)
        return

    await msg.delete()
    await update.message.reply_photo(photo=buf, caption=caption)


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update): return
    await update.message.reply_text(
        "❓ Unknown command. Try /help to see available commands.",
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════════════════════════════════
# ERROR HANDLER
# ══════════════════════════════════════════════════════════════════════
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Telegram error: {context.error}", exc_info=context.error)


# ══════════════════════════════════════════════════════════════════════
# SCHEDULED ALERT JOB
# ══════════════════════════════════════════════════════════════════════
async def run_alert_check(bot: Bot):
    logger.info("Running alert check…")
    try:
        alerts = await engine.check_alerts()
        for alert in alerts:
            try:
                await bot.send_message(
                    chat_id=ENV["chat_id"],
                    text=alert,
                    parse_mode="Markdown",
                )
                logger.info("Alert sent.")
            except Exception as e:
                logger.error(f"Failed to send alert: {e}")
    except Exception as e:
        logger.error(f"Alert check failed: {e}", exc_info=True)


# ══════════════════════════════════════════════════════════════════════
# STARTUP NOTIFICATION
# ══════════════════════════════════════════════════════════════════════
async def send_startup_message(bot: Bot):
    eat = timezone(timedelta(hours=3))
    now = datetime.now(eat).strftime("%Y-%m-%d %H:%M EAT")
    try:
        await bot.send_message(
            chat_id=ENV["chat_id"],
            text=(
                f"✅ *Gold Signal Bot — Online*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⏰ Started: `{now}`\n"
                f"⏱ Alert interval: every `{ENV['interval']} min`\n"
                f"📡 3-phase alerts active (Predict → Structural → M15 Entry)\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Send /signal to get your first analysis."
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Startup notification failed: {e}")


# ══════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════
def main():
    app = Application.builder().token(ENV["bot_token"]).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(CommandHandler("levels", cmd_levels))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("chart",  cmd_chart))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    app.add_error_handler(error_handler)

    scheduler = AsyncIOScheduler(timezone="Africa/Nairobi")
    scheduler.add_job(
        run_alert_check,
        "interval",
        minutes=ENV["interval"],
        args=[app.bot],
        id="alert_check",
    )
    scheduler.start()
    logger.info(f"Scheduler started — every {ENV['interval']} min (EAT)")

    # Boot notification (run once before polling starts)
    async def post_init(application):
        await send_startup_message(application.bot)

    app.post_init = post_init

    logger.info("Gold Signal Bot polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
