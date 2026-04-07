"""
TikTok Live Notifier — Telegram Bot
====================================
Commands:
  /add username     — Add a TikTok account to monitor
  /remove username  — Remove an account from monitoring
  /list             — Show all monitored accounts
  /online           — Show which monitored accounts are live right now

Setup:
  pip install python-telegram-bot==20.* TikTokLive

Run:
  BOT_TOKEN=your_token python tiktok_live_bot.py
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from TikTokLive import TikTokLiveClient
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ─── Config ───────────────────────────────────────────────────────────────────

BOT_TOKEN   = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
DATA_FILE   = Path("/data/tiktok_bot_data.json")
POLL_INTERVAL_SECONDS = 60          # how often to check for live streams
NOTIFY_COOLDOWN_SECONDS = 300       # don't re-notify for the same streamer within 5 min

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ─── Persistent state helpers ─────────────────────────────────────────────────

def load_data() -> dict:
    """Load bot state from disk."""
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {"chat_ids": [], "accounts": [], "last_notified": {}}

def save_data(data: dict) -> None:
    DATA_FILE.write_text(json.dumps(data, indent=2))

# ─── TikTok live-status check ─────────────────────────────────────────────────

async def is_user_live(username: str) -> bool:
    """Check if a TikTok user is currently live using the TikTokLive library."""
    try:
        client = TikTokLiveClient(unique_id=username)
        return await client.is_live()
    except Exception as e:
        log.warning("Error checking %s: %s", username, e)
        return False

# ─── Telegram command handlers ────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    data = load_data()
    if chat_id not in data["chat_ids"]:
        data["chat_ids"].append(chat_id)
        save_data(data)
    await update.message.reply_text(
        "👋 *TikTok Live Notifier*\n\n"
        "I'll ping you whenever a TikTok account you follow goes live.\n\n"
        "Commands:\n"
        "• /add username — start monitoring an account\n"
        "• /remove username — stop monitoring\n"
        "• /list — see all monitored accounts\n"
        "• /online — check who's live right now",
        parse_mode="Markdown",
    )

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    data = load_data()

    # Register this chat if not already known
    if chat_id not in data["chat_ids"]:
        data["chat_ids"].append(chat_id)

    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /add username")
        return

    raw = args[0].lstrip("@").lower()
    if not re.match(r"^[a-zA-Z0-9_.]{1,30}$", raw):
        await update.message.reply_text("⚠️ That doesn't look like a valid TikTok username.")
        return

    accounts = [a.lower() for a in data.get("accounts", [])]
    if raw in accounts:
        await update.message.reply_text(f"@{raw} is already in your list.")
        return

    data.setdefault("accounts", []).append(raw)
    save_data(data)
    await update.message.reply_text(f"✅ Now monitoring *@{raw}* for live streams.", parse_mode="Markdown")
    log.info("Added account: %s", raw)

async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /remove username")
        return

    raw = args[0].lstrip("@").lower()
    accounts = [a.lower() for a in data.get("accounts", [])]
    if raw not in accounts:
        await update.message.reply_text(f"@{raw} isn't in your list.")
        return

    data["accounts"] = [a for a in data["accounts"] if a.lower() != raw]
    data.get("last_notified", {}).pop(raw, None)
    save_data(data)
    await update.message.reply_text(f"🗑️ Removed *@{raw}* from monitoring.", parse_mode="Markdown")
    log.info("Removed account: %s", raw)

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    accounts = data.get("accounts", [])
    if not accounts:
        await update.message.reply_text("No accounts monitored yet. Use /add username to get started.")
        return
    lines = "\n".join(f"• @{a}" for a in accounts)
    await update.message.reply_text(f"📋 *Monitored accounts ({len(accounts)}):*\n{lines}", parse_mode="Markdown")

async def cmd_online(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    accounts = data.get("accounts", [])
    if not accounts:
        await update.message.reply_text("No accounts monitored yet. Use /add username to get started.")
        return

    checking_msg = await update.message.reply_text(f"🔍 Checking {len(accounts)} account(s)…")

    live_accounts = []
    results = await asyncio.gather(*[is_user_live(u) for u in accounts])

    for username, live in zip(accounts, results):
        if live:
            live_accounts.append(username)

    await checking_msg.delete()

    if not live_accounts:
        await update.message.reply_text("😴 Nobody on your list is live right now.")
        return

    lines = "\n".join(
        f"• [@{u}](https://www.tiktok.com/@{u}/live)" for u in live_accounts
    )
    await update.message.reply_text(
        f"🔴 *Live right now ({len(live_accounts)}/{len(accounts)}):*\n{lines}",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )

# ─── Background polling loop ──────────────────────────────────────────────────

async def poll_loop(app: Application) -> None:
    """Continuously poll TikTok for live streams and send Telegram notifications."""
    log.info("Polling loop started (interval: %ds)", POLL_INTERVAL_SECONDS)
    while True:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        data = load_data()
        accounts = data.get("accounts", [])
        chat_ids = data.get("chat_ids", [])
        last_notified: dict = data.setdefault("last_notified", {})

        if not accounts or not chat_ids:
            continue

        log.info("Checking %d account(s)…", len(accounts))
        for username in accounts:
            live = await is_user_live(username)
            if not live:
                continue

            # Double-check after 10 seconds to filter out false positives
            log.info("%s appears live, verifying in 10s…", username)
            await asyncio.sleep(10)
            live = await is_user_live(username)
            if not live:
                log.info("%s was a false positive, skipping.", username)
                continue

            now = time.time()
            last = last_notified.get(username, 0)
            if now - last < NOTIFY_COOLDOWN_SECONDS:
                log.info("%s is live but cooldown active, skipping notify.", username)
                continue

            # Update cooldown timestamp before sending
            last_notified[username] = now
            save_data(data)

            profile_url = f"https://www.tiktok.com/@{username}/live"
            msg = (
                f"🔴 *@{username} is LIVE on TikTok!*\n"
                f"[Watch now →]({profile_url})"
            )
            for chat_id in chat_ids:
                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=msg,
                        parse_mode="Markdown",
                        disable_web_page_preview=False,
                    )
                    log.info("Notified chat %s: %s is live", chat_id, username)
                except Exception as e:
                    log.warning("Failed to notify chat %s: %s", chat_id, e)

# ─── Entry point ──────────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    """Register bot commands with Telegram and launch the polling loop."""
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("start",  "Start the bot"),
        BotCommand("add",    "Add a TikTok account to monitor"),
        BotCommand("remove", "Remove a TikTok account"),
        BotCommand("list",   "List all monitored accounts"),
        BotCommand("online", "Check who is live right now"),
    ])
    asyncio.create_task(poll_loop(app))

def main() -> None:
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise SystemExit("❌  Set BOT_TOKEN env variable before running.\n"
                         "    export BOT_TOKEN=123456:ABC-your-token")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("add",    cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list",   cmd_list))
    app.add_handler(CommandHandler("online", cmd_online))

    log.info("Bot is running…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
