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
POLL_INTERVAL_SECONDS  = 60   
OFFLINE_GRACE_CHECKS   = 2    
CONCURRENCY_LIMIT      = 5    # Number of simultaneous checks allowed

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# Semaphore limits how many TikTok requests happen at the exact same time
check_semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

# ─── Persistent state helpers ─────────────────────────────────────────────────

def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            d = json.loads(DATA_FILE.read_text())
            d.setdefault("offline_counts", {})
            return d
        except Exception:
            pass
    return {"chat_ids": [], "accounts": [], "live_status": {}, "live_started": {}, "offline_counts": {}}

def save_data(data: dict) -> None:
    DATA_FILE.write_text(json.dumps(data, indent=2))

def escape_md(text: str) -> str:
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in text)

def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    return f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")

# ─── Optimized TikTok Check ───────────────────────────────────────────────────

async def is_user_live(username: str) -> tuple[bool, str, str]:
    """Fetches status and metadata with a concurrency lock and timeout."""
    async with check_semaphore:
        try:
            client = TikTokLiveClient(unique_id=username)
            # Timeout prevents a single stuck request from hanging the whole bot
            is_live = await asyncio.wait_for(client.is_live(), timeout=15.0)
            
            flv_url, thumb_url = "", ""
            
            if is_live:
                try:
                    # Fetching room info directly from the web payload
                    room_info = await asyncio.wait_for(client.fetch_room_info(), timeout=10.0)
                    dump_str = json.dumps(room_info).replace('\\"', '"').replace('\\/', '/')
                    
                    # 1. Improved Stream URL Regex
                    flv_matches = re.findall(r'"(https://[^"]+?\.flv[^"]*?)"', dump_str)
                    if flv_matches:
                        flv_url = flv_matches[0].replace("&only_audio=1", "").rstrip("\\")
                    
                    # 2. Improved Thumbnail Regex (Cover/Avatar fallback)
                    img_matches = re.findall(r'"(https://p[0-9]+-[^"]+?\.(?:webp|jpg|jpeg|image)[^"]*?)"', dump_str)
                    
                    # Prioritize 'cover' URLs, then 'avatar' URLs
                    covers = [m for m in img_matches if "cover" in m.lower()]
                    avatars = [m for m in img_matches if "avatar" in m.lower()]
                    
                    if covers:
                        thumb_url = covers[0]
                    elif avatars:
                        thumb_url = avatars[0]
                    elif img_matches:
                        thumb_url = img_matches[0]

                except Exception as inner_e:
                    log.debug("Metadata fetch timed out or failed for %s", username)

            return is_live, flv_url, thumb_url
        except Exception as e:
            log.warning("Check failed for %s: %s", username, e)
            return False, "", ""

# ─── Commands ─────────────────────────────────────────────────────────────────

async def cmd_pics(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    accounts = data.get("accounts", [])
    if not accounts:
        await update.message.reply_text("No accounts monitored.")
        return

    wait_msg = await update.message.reply_text(f"📸 Checking {len(accounts)} accounts in parallel...")
    
    # Run all checks simultaneously (respecting semaphore)
    results = await asyncio.gather(*[is_user_live(u) for u in accounts])
    
    found_any = False
    for username, (is_live, flv, thumb) in zip(accounts, results):
        if is_live:
            found_any = True
            caption = f"👤 *{escape_md('@' + username)}* is LIVE\\!\n🔗 [Watch](https://www.tiktok.com/@{username}/live)"
            if flv: caption += f"\n📥 [Download Stream]({flv})"
            
            try:
                if thumb:
                    await ctx.bot.send_photo(chat_id=update.effective_chat.id, photo=thumb, caption=caption, parse_mode="MarkdownV2")
                else:
                    await ctx.bot.send_message(chat_id=update.effective_chat.id, text=caption, parse_mode="MarkdownV2")
            except Exception:
                pass

    await wait_msg.delete()
    if not found_any:
        await update.message.reply_text("😴 No one is live right now.")

async def cmd_online(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    accounts = data.get("accounts", [])
    if not accounts:
        await update.message.reply_text("No accounts monitored.")
        return

    wait_msg = await update.message.reply_text(f"🔍 Checking status...")
    results = await asyncio.gather(*[is_user_live(u) for u in accounts])
    await wait_msg.delete()

    live_lines = []
    now = time.time()
    for username, (is_live, flv, _) in zip(accounts, results):
        if is_live:
            started = data.get("live_started", {}).get(username)
            dur = f" — {escape_md(format_duration(now - started))}" if started else ""
            line = f"• [{escape_md('@' + username)}](https://www.tiktok.com/@{username}/live){dur}"
            if flv: line += f"\n  └ 📥 [Download]({flv})"
            live_lines.append(line)

    if not live_lines:
        await update.message.reply_text("😴 No one is online.")
    else:
        await update.message.reply_text(f"🟢 *Live Right Now:*\n" + "\n".join(live_lines), parse_mode="MarkdownV2", disable_web_page_preview=True)

# ─── Polling & Core ───────────────────────────────────────────────────────────

async def poll_loop(app: Application) -> None:
    while True:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        data = load_data()
        accounts, chat_ids = data.get("accounts", []), data.get("chat_ids", [])
        if not accounts or not chat_ids: continue

        results = await asyncio.gather(*[is_user_live(u) for u in accounts])
        
        for username, (now_live, flv_url, thumb_url) in zip(accounts, results):
            was_live = data["live_status"].get(username, False)
            
            # Grace period logic for false offlines
            if not now_live and was_live:
                count = data["offline_counts"].get(username, 0) + 1
                data["offline_counts"][username] = count
                if count < OFFLINE_GRACE_CHECKS: continue
            else:
                data["offline_counts"][username] = 0

            if now_live == was_live: continue

            data["live_status"][username] = now_live
            now = time.time()

            if now_live:
                data["live_started"][username] = now
                msg = f"🟢 *{escape_md('@' + username)} is LIVE\\!*"
                if flv_url: msg += f"\n📥 [Download Stream]({flv_url})"
            else:
                started = data["live_started"].pop(username, None)
                dur = f" for {escape_md(format_duration(now - started))}" if started else ""
                msg = f"🔴 *{escape_md('@' + username)} is offline\\.*{dur}"
            
            save_data(data)
            for cid in chat_ids:
                try:
                    await app.bot.send_message(chat_id=cid, text=msg, parse_mode="MarkdownV2", disable_web_page_preview=True)
                except: pass

async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([("online", "Who is live"), ("pics", "Live previews"), ("add", "Add User"), ("remove", "Remove User"), ("list", "List Users")])
    asyncio.create_task(poll_loop(app))

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE": return
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    for cmd, handler in [("start", cmd_start), ("add", cmd_add), ("remove", cmd_remove), ("list", cmd_list), ("online", cmd_online), ("pics", cmd_pics)]:
        app.add_handler(CommandHandler(cmd, handler))
    app.run_polling(drop_pending_updates=True)

# Boilerplate for add/remove/list/start
async def cmd_start(u, c): await u.message.reply_text("TikTok Notifier Active. /add username to start.")
async def cmd_add(u, c): 
    d = load_data(); raw = c.args[0].lower().lstrip("@") if c.args else ""
    if raw and raw not in d["accounts"]: 
        d["accounts"].append(raw); d["live_status"][raw]=False; save_data(d); await u.message.reply_text(f"Added @{raw}")
async def cmd_remove(u, c):
    d = load_data(); raw = c.args[0].lower().lstrip("@") if c.args else ""
    d["accounts"] = [a for a in d["accounts"] if a != raw]; save_data(d); await u.message.reply_text(f"Removed @{raw}")
async def cmd_list(u, c):
    d = load_data(); await u.message.reply_text("Monitoring:\n" + "\n".join(d["accounts"]))

if __name__ == "__main__":
    main()
