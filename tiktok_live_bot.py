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

BOT_TOKEN              = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
DATA_FILE              = Path("/data/tiktok_bot_data.json")
POLL_INTERVAL_SECONDS  = 60   # how often to check live status

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ─── Persistent state helpers ─────────────────────────────────────────────────

def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    # live_status tracks whether each account was live on the last check
    return {"chat_ids": [], "accounts": [], "live_status": {}, "live_started": {}}

def save_data(data: dict) -> None:
    DATA_FILE.write_text(json.dumps(data, indent=2))

# ─── Markdown helper ──────────────────────────────────────────────────────────

def escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in text)

# ─── Duration helper ────────────────────────────────────────────────────────────

def format_duration(seconds: float) -> str:
    """Convert seconds into a human-readable duration string."""
    seconds = int(seconds)
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

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
        "I'll ping you whenever a TikTok account goes live or ends their stream\\.\n\n"
        "Commands:\n"
        "• /add username — start monitoring an account\n"
        "• /remove username — stop monitoring\n"
        "• /list — see all monitored accounts\n"
        "• /online — check who's live right now",
        parse_mode="MarkdownV2",
    )

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    data = load_data()

    if chat_id not in data["chat_ids"]:
        data["chat_ids"].append(chat_id)

    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /add username")
        return

    raw = args[0].lstrip("@").lower()
    if not re.match(r"^[a-zA-Z0-9_.]{1,24}$", raw):
        await update.message.reply_text("⚠️ That doesn't look like a valid TikTok username.")
        return

    accounts = [a.lower() for a in data.get("accounts", [])]
    if raw in accounts:
        await update.message.reply_text(f"@{raw} is already in your list.")
        return

    data.setdefault("accounts", []).append(raw)
    data.setdefault("live_status", {})[raw] = False
    data.setdefault("live_started", {})[raw] = None
    save_data(data)
    await update.message.reply_text(
        f"✅ Now monitoring *{escape_md('@' + raw)}* for live streams\\.",
        parse_mode="MarkdownV2",
    )
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
    data.get("live_status", {}).pop(raw, None)
    data.get("live_started", {}).pop(raw, None)
    save_data(data)
    await update.message.reply_text(
        f"🗑️ Removed *{escape_md('@' + raw)}* from monitoring\\.",
        parse_mode="MarkdownV2",
    )
    log.info("Removed account: %s", raw)

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    accounts = data.get("accounts", [])
    if not accounts:
        await update.message.reply_text("No accounts monitored yet. Use /add username to get started.")
        return
    lines = "\n".join(
        f"• [{escape_md('@' + a)}](https://www.tiktok.com/@{a})"
        for a in accounts
    )
    await update.message.reply_text(
        f"📋 *Monitored accounts \\({len(accounts)}\\):*\n{lines}",
        parse_mode="MarkdownV2",
        disable_web_page_preview=True,
    )

async def cmd_online(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    accounts = data.get("accounts", [])
    if not accounts:
        await update.message.reply_text("No accounts monitored yet. Use /add username to get started.")
        return

    checking_msg = await update.message.reply_text(f"🔍 Checking {len(accounts)} account(s)…")
    results = await asyncio.gather(*[is_user_live(u) for u in accounts])
    live_accounts = [u for u, live in zip(accounts, results) if live]

    await checking_msg.delete()

    if not live_accounts:
        await update.message.reply_text("😴 Nobody on your list is live right now.")
        return

    live_started = data.get("live_started", {})
    now = time.time()
    lines = []
    for u in live_accounts:
        started = live_started.get(u)
        duration = f" — {escape_md(format_duration(now - started))}" if started else ""
        lines.append(f"• [{escape_md('@' + u)}](https://www.tiktok.com/@{u}/live){duration}")
    lines_text = "\n".join(lines)
    await update.message.reply_text(
        f"🟢 *Live right now \\({len(live_accounts)}/{len(accounts)}\\):*\n{lines_text}",
        parse_mode="MarkdownV2",
        disable_web_page_preview=True,
    )

# ─── Background polling loop# ─── Background polling loop ──────────────────────────────────────────────────

async def poll_loop(app: Application) -> None:
    log.info("Polling loop started (interval: %ds)", POLL_INTERVAL_SECONDS)
    while True:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        data = load_data()
        accounts  = data.get("accounts", [])
        chat_ids  = data.get("chat_ids", [])
        live_status: dict = data.setdefault("live_status", {})

        if not accounts or not chat_ids:
            continue

        live_started: dict = data.setdefault("live_started", {})
        log.info("Checking %d account(s)…", len(accounts))
        for username in accounts:
            was_live = live_status.get(username, False)
            now_live = await is_user_live(username)

            # No change — skip
            if now_live == was_live:
                continue

            now = time.time()
            live_status[username] = now_live

            profile_url = f"https://www.tiktok.com/@{username}/live"

            if now_live:
                # Went live — record start time
                live_started[username] = now
                save_data(data)
                msg = (
                    f"🟢 *{escape_md('@' + username)} is LIVE on TikTok\\!*\n"
                    f"[Watch now →]({profile_url})"
                )
                log.info("%s went live", username)
            else:
                # Went offline — calculate duration
                started = live_started.pop(username, None)
                save_data(data)
                if started:
                    duration = escape_md(format_duration(now - started))
                    msg = (
                        f"🔴 *{escape_md('@' + username)} is no longer live\\.*\n"
                        f"⏱️ Was live for {duration}"
                    )
                else:
                    msg = f"🔴 *{escape_md('@' + username)} is no longer live\\.*"
                log.info("%s went offline", username)

            for chat_id in chat_ids:
                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=msg,
                        parse_mode="MarkdownV2",
                        disable_web_page_preview=False,
                    )
                except Exception as e:
                    log.warning("Failed to notify chat %s: %s", chat_id, e)

# ─── Entry point ──────────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
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
