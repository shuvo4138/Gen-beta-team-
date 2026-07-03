# handlers/helpers.py
"""
Shared utilities used across all handlers.
No panel logic — only cross-cutting concerns.
"""

import asyncio
from telegram import Message

from config import ADMIN_ID
from utils.logger import get_logger
from utils.state import user_data, user_msg, a1_poll_tasks, a2_poll_tasks, shark_poll_tasks

logger = get_logger(__name__)

# ── Processing guard (prevent double-tap) ──
processing_users: set[int] = set()

# ── Join check cache ──
_join_cache: dict[int, float] = {}

# ── Per-user OTP task registry ──
_otp_tasks: dict[int, list] = {}


# ══════════════════════════════════════════════════════════
#                  USER INIT
# ══════════════════════════════════════════════════════════

def init_user(user_id: int) -> None:
    """Ensure user_data[user_id] dict exists with safe defaults."""
    if user_id not in user_data:
        user_data[user_id] = {}
    defaults = {
        "panel":           "A1",
        "app":             "FACEBOOK",
        "country":         None,
        "range":           None,
        "number":          None,
        "numbers":         [],
        "last_number":     None,
        "otp_active":      False,
        "otp_running":     False,
        "auto_otp_cancel": False,
        "waiting_for":     None,
        "a1_service":      "Facebook New Account",
        "a1_zenex_service":"Facebook",
    }
    for k, v in defaults.items():
        user_data[user_id].setdefault(k, v)


# ══════════════════════════════════════════════════════════
#                  OTP TASK MANAGEMENT
# ══════════════════════════════════════════════════════════

def add_otp_task(user_id: int, task: asyncio.Task) -> None:
    _otp_tasks.setdefault(user_id, []).append(task)


def cancel_all_otp_tasks(user_id: int) -> None:
    """Cancel all running OTP poll tasks for this user."""
    # A1 poll
    t = a1_poll_tasks.pop(user_id, None)
    if t and not t.done():
        t.cancel()
    # A2 poll
    t = a2_poll_tasks.pop(user_id, None)
    if t and not t.done():
        t.cancel()
    # Shark poll
    t = shark_poll_tasks.pop(user_id, None)
    if t and not t.done():
        t.cancel()
    # Generic task registry
    for task in _otp_tasks.pop(user_id, []):
        if not task.done():
            task.cancel()
    # Flag
    if user_id in user_data:
        user_data[user_id]["otp_active"]      = False
        user_data[user_id]["otp_running"]     = False
        user_data[user_id]["auto_otp_cancel"] = True


# ══════════════════════════════════════════════════════════
#                  SAFE MESSAGE EDIT
# ══════════════════════════════════════════════════════════

async def safe_edit(query, text: str, **kwargs) -> None:
    """Edit a callback query message, silently swallowing 'not modified' errors."""
    try:
        await query.message.edit_text(text, **kwargs)
    except Exception as e:
        if "not modified" not in str(e).lower():
            logger.warning(f"safe_edit error: {e}")


# ══════════════════════════════════════════════════════════
#                  CHANNEL JOIN CHECK
# ══════════════════════════════════════════════════════════

async def check_user_joined(bot, user_id: int) -> bool:
    """
    Return True if user has joined all required channels.
    Admins always pass.
    Results cached for 60 seconds to avoid hammering Telegram API.
    """
    import time
    if user_id == ADMIN_ID:
        return True

    now = time.time()
    if now - _join_cache.get(user_id, 0) < 60:
        return True

    from config import MAIN_CHANNEL_CHECK_ID, OTP_CHANNEL_CHECK_ID, BACKUP_CHANNEL_CHECK_ID
    channels = [c for c in [MAIN_CHANNEL_CHECK_ID, OTP_CHANNEL_CHECK_ID, BACKUP_CHANNEL_CHECK_ID] if c]

    for channel_id in channels:
        try:
            member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
            if member.status in ("left", "kicked", "banned"):
                return False
        except Exception:
            pass  # channel not accessible — skip check

    _join_cache[user_id] = now
    return True
