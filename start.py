# handlers/start.py
"""
/start command handler + text message router.

Handles:
  /start          — welcome + main keyboard
  📲 Get Number   — service select menu
  📦 My Numbers   — show current session
  📡 Custom Range — manual range input
  🚦 Live Traffic — OTP traffic stats
  ✈️ Telegram     — Telegram panel shortcut
  👤 Profile      — user info
  🆘 Support      — support link
"""

import time
import asyncio
from datetime import datetime

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from config import (
    ADMIN_ID,
    SUPPORT_ADMIN_LINK,
    JOIN_CHANNEL_LINK,
    OTP_CHANNEL_JOIN_LINK,
    BACKUP_CHANNEL_LINK,
)
from utils.logger import get_logger
from utils.state import user_data, user_msg
from utils.helpers import COUNTRY_FLAGS_CODE, COUNTRY_NAMES_CODE, APP_EMOJIS
from keyboards.menus import (
    main_keyboard,
    join_channel_keyboard,
    panel_select_inline,
    get_welcome_text,
    SERVICE_SELECT_TEXT,
    after_number_inline_s3,
)
from handlers.helpers import (
    init_user,
    cancel_all_otp_tasks,
    check_user_joined,
    processing_users,
)
from panels.s3 import (
    s3_add_user,
    s3_get_session,
    s3_get_user_count,
    get_numbers_pool,
    parse_pool_key,
    otp_cache,
)

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#                  /start COMMAND
# ══════════════════════════════════════════════════════════

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — show personalized welcome + main keyboard."""
    user    = update.effective_user
    user_id = user.id
    chat_id = update.effective_chat.id

    init_user(user_id)

    # Register user in S3 db
    first  = user.first_name or "User"
    last   = user.last_name  or ""
    uname  = user.username   or str(user_id)
    joined = datetime.now().strftime("%Y-%m-%d")
    user_data[user_id].update({
        "name":     f"{first} {last}".strip(),
        "username": uname,
        "joined":   joined,
    })
    s3_add_user(user_id, uname)

    # Channel join check
    joined_all = await check_user_joined(context.bot, user_id)
    if not joined_all:
        await update.message.reply_text(
            "⚠️ Bot ব্যবহার করতে সব channel join করুন:",
            reply_markup=join_channel_keyboard(
                join_link=JOIN_CHANNEL_LINK,
                otp_link=OTP_CHANNEL_JOIN_LINK,
                backup_link=BACKUP_CHANNEL_LINK,
            ),
        )
        return

    welcome = get_welcome_text(first)
    await update.message.reply_text(
        welcome,
        parse_mode="HTML",
        reply_markup=main_keyboard(user_id),
    )


# ══════════════════════════════════════════════════════════
#                  TEXT MESSAGE ROUTER
# ══════════════════════════════════════════════════════════

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route reply-keyboard button presses and free-text input."""
    user    = update.effective_user
    user_id = user.id
    chat_id = update.effective_chat.id
    text    = (update.message.text or "").strip()

    init_user(user_id)

    # ── Admin /panel shortcut ──
    if text == "/panel" and user_id == ADMIN_ID:
        from keyboards.menus import admin_keyboard
        await update.message.reply_text(
            "🔧 *Admin Panel*",
            parse_mode="Markdown",
            reply_markup=admin_keyboard(),
        )
        return

    # ── Channel join check ──
    joined_all = await check_user_joined(context.bot, user_id)
    if not joined_all:
        await update.message.reply_text(
            "⚠️ Bot ব্যবহার করতে সব channel join করুন:",
            reply_markup=join_channel_keyboard(
                join_link=JOIN_CHANNEL_LINK,
                otp_link=OTP_CHANNEL_JOIN_LINK,
                backup_link=BACKUP_CHANNEL_LINK,
            ),
        )
        return

    # ── Waiting states ──
    waiting = user_data[user_id].get("waiting_for")

    if waiting == "custom_range":
        user_data[user_id]["waiting_for"] = None
        raw   = text.strip().upper().replace(" ", "").replace("+", "")
        panel = user_data[user_id].get("panel", "A1")
        user_data[user_id]["range"] = raw
        await update.message.delete()
        if panel == "A1":
            asyncio.create_task(
                context.bot.send_message(chat_id, f"📡 A1 range: `{raw}`\n⏳ Searching...", parse_mode="Markdown")
            )
            from panels.a1 import do_get_number_a1
            asyncio.create_task(do_get_number_a1(update.message, user_id, bot=context.bot))
        elif panel == "A2":
            from panels.a2 import do_get_number_a2
            asyncio.create_task(do_get_number_a2(update.message, user_id, bot=context.bot))
        return

    if waiting == "broadcast" and user_id == ADMIN_ID:
        user_data[user_id]["waiting_for"] = None
        from handlers.admin import do_broadcast
        await do_broadcast(context.bot, text)
        await update.message.reply_text("✅ Broadcast sent!", reply_markup=main_keyboard(user_id))
        return

    # ── S3 admin: pending delete ──
    if context.bot_data.get("pending_delete_number") and user_id == ADMIN_ID:
        context.bot_data["pending_delete_number"] = False
        number = text.strip().lstrip("+")
        pool   = get_numbers_pool()
        deleted = False
        for pk, nums in pool.items():
            if number in nums:
                nums.remove(number)
                deleted = True
                await update.message.reply_text(
                    f"✅ *Number Deleted!*\n\n📱 `+{number}`\n🌍 Pool: `{pk}`",
                    parse_mode="Markdown",
                )
                break
        if not deleted:
            await update.message.reply_text(
                f"❌ Number `+{number}` কোনো pool এ নেই", parse_mode="Markdown"
            )
        return

    # ══════════════════════════════════════════════════════
    #                  MENU BUTTONS
    # ══════════════════════════════════════════════════════

    # ── 📲 Get Number ──
    if text in ("📲 Get Number", "Get Number"):
        if user_id in processing_users:
            return
        processing_users.add(user_id)
        try:
            cancel_all_otp_tasks(user_id)
            user_data[user_id]["waiting_for"] = None
            user_data[user_id]["otp_active"]  = False
            kb = await panel_select_inline()
            msg = await context.bot.send_message(
                chat_id, SERVICE_SELECT_TEXT,
                reply_markup=kb,
            )
            user_msg[chat_id] = msg.message_id
        finally:
            processing_users.discard(user_id)
        return

    # ── 📦 My Numbers ──
    if text in ("📦 My Numbers", "📋 My Numbers"):
        panel = user_data[user_id].get("panel", "A1")
        if panel == "S3":
            session = s3_get_session(user_id)
            if session:
                nums     = session.get("numbers", [session.get("number")] if session.get("number") else [])
                pool_key = session["pool_key"]
                code, service, _ = parse_pool_key(pool_key)
                flag  = COUNTRY_FLAGS_CODE.get(code, "🌍")
                cname = COUNTRY_NAMES_CODE.get(code, "Unknown")
                svc   = "FACEBOOK" if service == "fb" else "INSTAGRAM"
                card  = (
                    f"✅ <b>Numbers Assigned!</b>\n\n"
                    f"<b>Service:</b> {svc}\n"
                    f"🌍 <b>Country:</b> {flag} {cname}\n"
                    f"⏳ <b>Reserved:</b> 30 min\n\n"
                    f"📩 OTPs forwarded automatically."
                )
                await update.message.reply_text(
                    card, parse_mode="HTML",
                    reply_markup=after_number_inline_s3(pool_key, nums),
                )
            else:
                await update.message.reply_text(
                    "❌ কোনো active S3 session নেই।",
                    reply_markup=main_keyboard(user_id),
                )
        else:
            last = str(user_data[user_id].get("last_number", "")).replace("+", "").strip()
            if not last:
                await update.message.reply_text(
                    "❌ কোনো number নেওয়া হয়নি।",
                    reply_markup=main_keyboard(user_id),
                )
            else:
                await update.message.reply_text(
                    f"📋 Last number: `{last}`",
                    parse_mode="Markdown",
                    reply_markup=main_keyboard(user_id),
                )
        return

    # ── 📡 Custom Range ──
    if text in ("📡 Custom Range", "Custom Range"):
        panel = user_data[user_id].get("panel", "A1")
        if panel == "S3":
            await update.message.reply_text(
                "❌ S3 তে Custom Range নেই। অন্য panel select করুন।",
                reply_markup=main_keyboard(user_id),
            )
            return
        cancel_all_otp_tasks(user_id)
        user_data[user_id]["waiting_for"] = "custom_range"
        app = user_data[user_id].get("app", "FACEBOOK")
        await update.message.reply_text(
            f"📡 Custom Range লিখুন:\n\n🖥 Panel: {panel}\n📱 App: {app}\n\nউদাহরণ: 23762155XXX",
            reply_markup=main_keyboard(user_id),
        )
        return

    # ── 🚦 Live Traffic ──
    if text == "🚦 Live Traffic":
        await update.message.reply_text(
            "⏳ লোড হচ্ছে...", reply_markup=main_keyboard(user_id)
        )
        try:
            pool     = get_numbers_pool()
            fb_count = sum(len(v) for k, v in pool.items() if k.endswith("_fb"))
            ig_count = sum(len(v) for k, v in pool.items() if k.endswith("_ig"))
            s3_otp   = sum(1 for k in otp_cache if k.startswith("s3:"))
            bd_now   = datetime.now().strftime("%I:%M %p")
            msg = (
                f"🚦 <b>Live OTP Traffic</b>\n\n"
                f"🔴 S3 OTP (session): <b>{s3_otp}</b>\n\n"
                f"📘 FB Numbers in pool: <b>{fb_count}</b>\n"
                f"📸 IG Numbers in pool: <b>{ig_count}</b>\n\n"
                f"🕐 {bd_now}"
            )
            await update.message.reply_text(
                msg, parse_mode="HTML", reply_markup=main_keyboard(user_id)
            )
        except Exception as e:
            logger.error(f"Live Traffic error: {e}")
            await update.message.reply_text(
                "❌ Data load error.", reply_markup=main_keyboard(user_id)
            )
        return

    # ── ✈️ Telegram ──
    if text in ("✈️ Telegram", "Telegram"):
        await update.message.reply_text(
            "✈️ *Telegram Panel*\n\nSelect করুন:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🆕 Telegram A1", callback_data="select_panel_TG_A1",
                                      api_kwargs={"style": "success"})],
                [InlineKeyboardButton("⚡ Telegram A2", callback_data="select_panel_TG_A2",
                                      api_kwargs={"style": "success"})],
                [InlineKeyboardButton("◀️ Back", callback_data="back_app",
                                      api_kwargs={"style": "primary"})],
            ]),
        )
        return

    # ── 👤 Profile ──
    if text in ("👤 Profile", "Profile"):
        name   = user_data[user_id].get("name", user.first_name or "User")
        uname  = user_data[user_id].get("username", str(user_id))
        joined = user_data[user_id].get("joined", "Unknown")
        panel  = user_data[user_id].get("panel", "A1")
        last   = user_data[user_id].get("last_number", "—")
        total  = s3_get_user_count()
        await update.message.reply_text(
            f"👤 <b>Profile</b>\n\n"
            f"📛 Name: <b>{name}</b>\n"
            f"🆔 ID: <code>{user_id}</code>\n"
            f"🔗 Username: @{uname}\n"
            f"📅 Joined: {joined}\n"
            f"🖥 Panel: {panel}\n"
            f"📱 Last Number: <code>{last}</code>\n\n"
            f"👥 Total Users: {total}",
            parse_mode="HTML",
            reply_markup=main_keyboard(user_id),
        )
        return

    # ── 🆘 Support ──
    if text in ("🆘 Support", "Support"):
        await update.message.reply_text(
            f"🆘 <b>Support</b>\n\nAdmin: {SUPPORT_ADMIN_LINK}",
            parse_mode="HTML",
            reply_markup=main_keyboard(user_id),
        )
        return
