"""
TikTok Live Notifier — Telegram Bot (v2.0 - Stabilized)
====================================
New Features:
  /pics — See live thumbnails/screenshots of active streams
  * Fixed false offline notifications via consecutive check logic.
  * Reduced rate-limiting by throttling requests.
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from TikTokLive import TikTokLiveClient
from telegram import Update, InputMediaPhoto
from telegram.ext import Application, CommandHandler, ContextTypes

# ─── Config ───────────────────────────────────────────────────────────────────

BOT_TOKEN              = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
DATA_FILE              = Path("/data/tiktok_bot_data.json")
POLL_INTERVAL_SECONDS  = 60   
OFFLINE_GRACE_CHECKS   = 2    # Number of times an account must be "offline" before notifying

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ─── Persistent state helpers ─────────────────────────────────────────────────

def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            d = json.loads(DATA_FILE.read_text())
            # Ensure new keys exist
            d.setdefault("offline_counts", {})
            return d
        except Exception:
            pass
    return {
        "chat_ids": [], 
        "accounts": [], 
        "live_status": {}, 
        "live_started": {}, 
        "offline_counts": {}
    }

def save_data(data: dict) -> None:
    DATA_FILE.write_text(json.dumps(data, indent=2))

def escape_md(text: str) -> str:
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in text)

def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    if h: return f"{h}h {m}m"
    if m: return f"{m}m {s}s"
    return f"{s}s"

# ─── Enhanced TikTok live-status check ────────────────────────────────────────

async def is_user_live(username: str) -> tuple[bool, str, str]:
    """Returns (is_live, flv_url, thumbnail_url)"""
    try:
        client = TikTokLiveClient(unique_id=username)
        is_live = await client.is_live()
        flv_url = ""
        thumb_url = ""
        
        if is_live:
            try:
                room_info = {}
                if hasattr(client, 'room_info') and client.room_info:
                    room_info = client.room_info
                else:
                    room_info = await client.fetch_room_info()
                
                dump_str = json.dumps(room_info).replace('\\"', '"').replace('\\/', '/')
                
                # 1. Extract FLV (Stream)
                flv_matches = re.findall(r'"(https://[^"]+\.flv[^"]+)"', dump_str)
                if flv_matches:
                    flv_url = flv_matches[0].replace("&only_audio=1", "").rstrip("\\")
                
                # 2. Extract Thumbnail (Cover Image)
                # Look for high-res cover or avatar URLs
                img_matches = re.findall(r'"(https://p[0-9]+-[^"]+\.(?:webp|jpg|jpeg|image)[^"]*)"', dump_str)
                # Filter for URLs containing 'cover' first, else take first image
                covers = [m for m in img_matches if "cover" in m.lower()]
                thumb_url = covers[0] if covers else (img_matches[0] if img_matches else "")
                
            except Exception as inner_e:
                log.warning("Metadata extraction failed for %s: %s", username, inner_e)

        return is_live, flv_url, thumb_url
    except Exception as e:
        log.warning("Error checking %s: %s", username, e)
        return False, "", ""

# ─── Telegram Command Handlers ────────────────────────────────────────────────

async def cmd_pics(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    accounts = data.get("accounts", [])
    if not accounts:
        await update.message.reply_text("No accounts monitored. Use /add username.")
        return

    wait_msg = await update.message.reply_text("📸 Fetching current live previews...")
    
    live_count = 0
    for username in accounts:
        # Throttle to avoid rate limits
        await asyncio.sleep(0.5)
        is_live, flv, thumb = await is_user_live(username)
        
        if is_live and thumb:
            live_count += 1
            caption = (
                f"👤 *{escape_md('@' + username)}* is LIVE\\!\n"
                f"🔗 [Watch on TikTok](https://www.tiktok.com/@{username}/live)\n"
            )
            if flv:
                caption += f"📥 [Download Stream \\(\\.flv\\)]({flv})"
            
            try:
                await ctx.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=thumb,
                    caption=caption,
                    parse_mode="MarkdownV2"
                )
            except Exception:
                await update.message.reply_text(f"Could not load image for @{username}, but they are live!")

    await wait_msg.delete()
    if live_count == 0:
        await update.message.reply_text("😴 No one is live right now to take a picture of.")

async def cmd_online(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    accounts = data.get("accounts", [])
    if not accounts:
        await update.message.reply_text("No accounts monitored.")
        return

    checking_msg = await update.message.reply_text(f"🔍 Checking {len(accounts)} account(s)...")
    
    live_data = []
    for u in accounts:
        await asyncio.sleep(0.4) # Small delay to prevent rate limits
        res = await is_user_live(u)
        if res[0]:
            live_data.append((u, res[1]))

    await checking_msg.delete()
    if not live_data:
        await update.message.reply_text("😴 Nobody is live.")
        return

    lines = []
    now = time.time()
    for u, flv_url in live_data:
        started = data.get("live_started", {}).get(u)
        duration = f" — {escape_md(format_duration(now - started))}" if started else ""
        lines.append(f"• [{escape_md('@' + u)}](https://www.tiktok.com/@{u}/live){duration}")
        if flv_url:
            lines.append(f"  └ 📥 [Download Stream]({flv_url})")
            
    await update.message.reply_text(
        f"🟢 *Live right now \\({len(live_data)}\\):*\n" + "\n".join(lines),
        parse_mode="MarkdownV2",
        disable_web_page_preview=True
    )

# ─── (Standard Add/Remove/List handlers remain the same) ───────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    data = load_data()
    if chat_id not in data["chat_ids"]:
        data["chat_ids"].append(chat_id)
        save_data(data)
    await update.message.reply_text(
        "👋 *TikTok Notifier v2.0*\n\n"
        "• /add username\n• /remove username\n• /list\n• /online — check live users\n• /pics — see live thumbnails",
        parse_mode="MarkdownV2"
    )

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    data = load_data()
    if chat_id not in data["chat_ids"]: data["chat_ids"].append(chat_id)
    if not ctx.args:
        await update.message.reply_text("Usage: /add username")
        return
    raw = ctx.args[0].lstrip("@").lower()
    if raw in data["accounts"]:
        await update.message.reply_text(f"@{raw} is already monitored.")
        return
    data["accounts"].append(raw)
    data["live_status"][raw] = False
    data["offline_counts"][raw] = 0
    save_data(data)
    await update.message.reply_text(f"✅ Monitoring @{raw}")

async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    if not ctx.args: return
    raw = ctx.args[0].lstrip("@").lower()
    data["accounts"] = [a for a in data["accounts"] if a != raw]
    data["live_status"].pop(raw, None)
    data["offline_counts"].pop(raw, None)
    save_data(data)
    await update.message.reply_text(f"🗑️ Removed @{raw}")

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    accs = data.get("accounts", [])
    if not accs:
        await update.message.reply_text("Empty list.")
        return
    text = "\n".join([f"• @{a}" for a in accs])
    await update.message.reply_text(f"📋 *Monitoring:*\n{text}", parse_mode="MarkdownV2")

# ─── Background Polling Loop with Stability Fix ───────────────────────────────

async def poll_loop(app: Application) -> None:
    while True:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        data = load_data()
        accounts = data.get("accounts", [])
        chat_ids = data.get("chat_ids", [])
        if not accounts or not chat_ids: continue

        for username in accounts:
            await asyncio.sleep(0.5) # Throttle loop to avoid IP bans
            was_live = data["live_status"].get(username, False)
            now_live, flv_url, thumb_url = await is_user_live(username)

            # --- LOGIC TO PREVENT FALSE OFFLINES ---
            if not now_live and was_live:
                # User appears offline. Increment "grace" counter.
                count = data["offline_counts"].get(username, 0) + 1
                data["offline_counts"][username] = count
                save_data(data)
                
                if count < OFFLINE_GRACE_CHECKS:
                    log.info("Suspected offline for %s (Check %d/%d) - Ignoring for now.", username, count, OFFLINE_GRACE_CHECKS)
                    continue # Skip notification until confirmed twice
            else:
                # If they are online, or were already offline, reset counter
                data["offline_counts"][username] = 0
            
            # If status hasn't changed (after grace logic), move to next account
            if now_live == was_live:
                continue

            # --- STATUS CHANGE CONFIRMED ---
            data["live_status"][username] = now_live
            now = time.time()
            
            if now_live:
                data["live_started"][username] = now
                msg = f"🟢 *{escape_md('@' + username)} is LIVE on TikTok\\!*"
                if flv_url: msg += f"\n📥 [Download Stream \\(\\.flv\\)]({flv_url})"
            else:
                started = data["live_started"].pop(username, None)
                dur = f" for {escape_md(format_duration(now - started))}" if started else ""
                msg = f"🔴 *{escape_md('@' + username)} is no longer live\\.*{dur}"
            
            save_data(data)
            for cid in chat_ids:
                try:
                    await app.bot.send_message(chat_id=cid, text=msg, parse_mode="MarkdownV2", disable_web_page_preview=(not now_live))
                except: pass

# ─── Entry Point ──────────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        ("start", "Menu"), ("add", "Add User"), ("remove", "Remove User"),
        ("list", "List Users"), ("online", "Who is live"), ("pics", "See live previews")
    ])
    asyncio.create_task(poll_loop(app))

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE": return
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("online", cmd_online))
    app.add_handler(CommandHandler("pics", cmd_pics))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
