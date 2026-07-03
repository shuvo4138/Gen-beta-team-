# handlers/admin.py
"""
Admin-only handlers.

Commands:
  /admin  — show admin panel
  /stats  — quick stats

Broadcast helper used by message_handler when waiting_for == "broadcast".
"""

import asyncio
from datetime import datetime

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from config import ADMIN_ID, ZENEX_API_KEY, A2_API_KEY
from utils.logger import get_logger
from utils.state import user_data
from keyboards.menus import admin_keyboard, admin_keyboard_s3
from handlers.helpers import safe_edit, init_user
from panels.s3 import (
    get_numbers_pool,
    s3_get_user_count,
    otp_cache,
    tg_load_all,
)

logger = get_logger(__name__)

_back_btn = [[InlineKeyboardButton("◀️ Back", callback_data="admin_main",
                                    api_kwargs={"style": "primary"})]]


# ══════════════════════════════════════════════════════════
#                  /admin COMMAND
# ══════════════════════════════════════════════════════════

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Admin only!")
        return
    init_user(user_id)
    await update.message.reply_text(
        "🔧 *Admin Panel*",
        parse_mode="Markdown",
        reply_markup=admin_keyboard(),
    )


# ══════════════════════════════════════════════════════════
#                  /stats COMMAND
# ══════════════════════════════════════════════════════════

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return
    pool     = get_numbers_pool()
    fb_count = sum(len(v) for k, v in pool.items() if k.endswith("_fb"))
    ig_count = sum(len(v) for k, v in pool.items() if k.endswith("_ig"))
    s3_otp   = sum(1 for k in otp_cache if k.startswith("s3:"))
    total    = s3_get_user_count()
    now      = datetime.now().strftime("%Y-%m-%d %H:%M")
    msg = (
        f"📊 *Stats*\n\n"
        f"👥 Users: `{total}`\n"
        f"📘 FB pool: `{fb_count}`\n"
        f"📸 IG pool: `{ig_count}`\n"
        f"🔴 S3 OTPs: `{s3_otp}`\n"
        f"🆕 A1: {'✅' if ZENEX_API_KEY else '❌'}\n"
        f"⚡ A2: {'✅' if A2_API_KEY else '❌'}\n"
        f"🕐 {now}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ══════════════════════════════════════════════════════════
#                  BROADCAST
# ══════════════════════════════════════════════════════════

async def do_broadcast(bot, message_text: str) -> tuple[int, int]:
    """
    Send a text message to all known users.
    Returns (sent, failed) counts.
    """
    from panels.s3 import s3_get_all_users
    user_ids = s3_get_all_users()
    sent = failed = 0
    for uid_str in user_ids:
        try:
            await bot.send_message(int(uid_str), message_text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    logger.info(f"Broadcast done: sent={sent} failed={failed}")
    return sent, failed


# ══════════════════════════════════════════════════════════
#                  ADMIN CALLBACK ACTIONS
# ══════════════════════════════════════════════════════════

async def handle_admin_callback(query, user_id: int, action: str, context) -> None:
    """
    Handle all admin_ and s3admin_ callback actions.
    Called from handlers/callbacks.py.
    """
    if user_id != ADMIN_ID:
        await query.answer("❌ Admin only!", show_alert=True)
        return

    # ── Main panel ──
    if action in ("main", "refresh"):
        await safe_edit(query, "🔧 *Admin Panel*",
                        parse_mode="Markdown", reply_markup=admin_keyboard())
        return

    # ── Stats ──
    if action == "stats":
        pool     = get_numbers_pool()
        fb_count = sum(len(v) for k, v in pool.items() if k.endswith("_fb"))
        ig_count = sum(len(v) for k, v in pool.items() if k.endswith("_ig"))
        s3_otp   = sum(1 for k in otp_cache if k.startswith("s3:"))
        total    = s3_get_user_count()
        now      = datetime.now().strftime("%Y-%m-%d %H:%M")
        await safe_edit(query,
            f"📊 *Bot Statistics*\n\n"
            f"👥 Total Users: `{total}`\n"
            f"📘 FB Pool: `{fb_count}`\n"
            f"📸 IG Pool: `{ig_count}`\n"
            f"🔴 S3 OTPs (session): `{s3_otp}`\n"
            f"🆕 A1 (ZENEX): {'✅' if ZENEX_API_KEY else '❌'}\n"
            f"⚡ A2 (VOLTX): {'✅' if A2_API_KEY else '❌'}\n\n"
            f"🕐 {now}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(_back_btn),
        )
        return

    # ── A1 panel info ──
    if action == "a1panel":
        try:
            from panels.a1 import zenex_get_active_ranges
            ranges = await zenex_get_active_ranges()
            rc     = len(ranges)
        except Exception:
            rc = 0
        await safe_edit(query,
            f"🆕 *A1 Panel — ZENEX Network*\n\n"
            f"Status: {'✅ Online' if ZENEX_API_KEY else '❌ API Key নেই'}\n"
            f"Active Ranges: `{rc}`\n\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh Ranges", callback_data="admin_refresh_a1",
                                      api_kwargs={"style": "success"})],
                *_back_btn,
            ]),
        )
        return

    if action == "refresh_a1":
        from panels.a1 import _zenex_ranges_cache, zenex_get_active_ranges
        _zenex_ranges_cache["time"] = 0
        try:
            rc = len(await zenex_get_active_ranges())
        except Exception:
            rc = 0
        await safe_edit(query,
            f"✅ *A1 Ranges Refreshed!*\n\nActive: `{rc}`\n🕐 {datetime.now().strftime('%H:%M')}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(_back_btn),
        )
        return

    # ── A2 panel info ──
    if action == "a2panel":
        try:
            from panels.a2 import a2_get_active_ranges
            rc = len(await a2_get_active_ranges())
        except Exception:
            rc = 0
        await safe_edit(query,
            f"⚡ *A2 Panel — VOLTX SMS*\n\n"
            f"Status: {'✅ Online' if A2_API_KEY else '❌ API Key নেই'}\n"
            f"Active Ranges: `{rc}`\n\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh Ranges", callback_data="admin_refresh_a2",
                                      api_kwargs={"style": "success"})],
                *_back_btn,
            ]),
        )
        return

    if action == "refresh_a2":
        from panels.a2 import _ranges_cache, a2_get_active_ranges
        _ranges_cache["time"] = 0
        try:
            rc = len(await a2_get_active_ranges(force=True))
        except Exception:
            rc = 0
        await safe_edit(query,
            f"✅ *A2 Ranges Refreshed!*\n\nActive: `{rc}`\n🕐 {datetime.now().strftime('%H:%M')}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(_back_btn),
        )
        return

    # ── S4 Shark ──
    if action == "s4panel":
        from database.supabase import fetch_shark_otps_from_supabase
        rows = await fetch_shark_otps_from_supabase()
        await safe_edit(query,
            f"🦈 *S4 Shark Panel — CR API*\n\n"
            f"Recent OTPs (30 min): `{len(rows)}`\n\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(_back_btn),
        )
        return

    # ── Broadcast ──
    if action == "broadcast":
        user_data[ADMIN_ID]["waiting_for"] = "broadcast"
        await safe_edit(query,
            "📢 *Broadcast*\n\nMessage লিখুন — সব user কে পাঠানো হবে:",
            parse_mode="Markdown",
        )
        return

    # ── Clear cache ──
    if action == "clearcache":
        otp_cache.clear()
        await query.answer("🧹 Cache cleared!")
        await safe_edit(query,
            "🧹 *Cache Cleared!*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(_back_btn),
        )
        return

    # ── Restart ──
    if action == "restart":
        await query.answer("♻️ Restarting...")
        await safe_edit(query,
            "♻️ *Bot restarting...*\n\nকিছুক্ষণ পরে আবার চালু হবে।",
            parse_mode="Markdown",
        )
        import sys, os
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)
        return

    # ── Stop ──
    if action == "stop":
        await query.answer("🛑 Bot বন্ধ হচ্ছে...")
        await safe_edit(query, "🛑 *Bot stopped by admin.*", parse_mode="Markdown")
        import asyncio as _a
        _a.get_event_loop().stop()
        return

    # ── S3 admin sub-panel ──
    if action == "s3stats":
        pool         = get_numbers_pool()
        total_nums   = sum(len(v) for v in pool.values())
        await safe_edit(query,
            f"📊 *S3 Statistics*\n\n"
            f"👥 S3 Users: `{s3_get_user_count()}`\n"
            f"📱 Total Numbers: `{total_nums}`\n\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            parse_mode="Markdown",
            reply_markup=admin_keyboard_s3(),
        )
        return

    if action == "addnumbers":
        user_data[ADMIN_ID]["waiting_for"] = "upload_numbers"
        await safe_edit(query,
            "📤 *Upload Numbers*\n\n"
            "Format: `pool_key` এর জন্য numbers.txt file পাঠান।\n"
            "প্রতি line এ একটি number।",
            parse_mode="Markdown",
        )
        return

    if action == "delete":
        context.bot_data["pending_delete_number"] = True
        await safe_edit(query,
            "🗑️ *Delete Number*\n\nDelete করার number টি পাঠান:",
            parse_mode="Markdown",
        )
        return

    if action == "broadcast_s3":
        user_data[ADMIN_ID]["waiting_for"] = "broadcast"
        await safe_edit(query,
            "📢 *S3 Broadcast*\n\nMessage লিখুন:",
            parse_mode="Markdown",
        )
        return

    if action == "reload":
        await tg_load_all(context.bot)
        pool   = get_numbers_pool()
        total  = sum(len(v) for v in pool.values())
        await safe_edit(query,
            f"✅ *Pool Reloaded!*\n\nTotal Numbers: `{total}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(_back_btn),
        )
        return
