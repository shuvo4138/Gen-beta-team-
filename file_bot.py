"""
╔══════════════════════════════════════════╗
║   GEN BETA TEAM                         ║
║   Facebook Automatic Bot                ║
╚══════════════════════════════════════════╝
"""

import asyncio
import httpx
import time
import re
import random
import logging
import os
import string
import json
from datetime import datetime
from typing import Optional

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# ══════════════════════════════════════════
#              ENV CONFIG
# ══════════════════════════════════════════
BOT_TOKEN       = os.getenv("BOT_TOKEN", "")
VOLTX_API_KEY   = os.getenv("VOLTX_API_KEY", "")
VOLTX_BASE_URL  = os.getenv("VOLTX_BASE_URL", "https://api.2oo9.cloud/MXS47FLFX0U/tnevs/@public/api")
ADMIN_IDS       = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
OUTPUT_FILE     = os.getenv("OUTPUT_FILE", "accounts.txt")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════
#         CONVERSATION STATES
# ══════════════════════════════════════════
(
    STATE_PROXY_TYPE,
    STATE_PROXY_INPUT,
    STATE_THREAD_COUNT,
    STATE_PASSWORD_INPUT,
    STATE_MANUAL_PASSWORD,
) = range(5)

# ══════════════════════════════════════════
#         USER SESSION STORE
# ══════════════════════════════════════════
user_sessions = {}  # user_id → session data

def get_session(user_id: int) -> dict:
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "proxy_type": None,
            "proxy_user": None,
            "proxy_pass": None,
            "proxy_host": None,
            "proxy_port": None,
            "threads": 3,
            "password_mode": "random",
            "manual_password": None,
            "running": False,
            "stats": {"verified": 0, "sms_pushed": 0, "searched": 0, "dead": 0, "total": 0},
            "accounts": [],
            "selected_range": None,
            "dashboard_msg_id": None,
            "chat_id": None,
        }
    return user_sessions[user_id]

# ══════════════════════════════════════════
#         VOLTX API
# ══════════════════════════════════════════
def voltx_headers():
    return {
        "mauthapi": VOLTX_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

async def voltx_get_ranges() -> list:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(f"{VOLTX_BASE_URL}/liveaccess", headers=voltx_headers())
        if res.status_code == 200:
            data = res.json()
            if data.get("meta", {}).get("code") == 200:
                services = data.get("data", {}).get("services", [])
                ranges = []
                for svc in services:
                    if "FACEBOOK" not in svc.get("sid", "").upper():
                        continue
                    for rng in svc.get("ranges", []):
                        ranges.append(rng)
                return ranges
    except Exception as e:
        logger.error(f"VOLTX get_ranges error: {e}")
    return []

async def voltx_get_number(range_val: str) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(
                f"{VOLTX_BASE_URL}/getnum",
                headers=voltx_headers(),
                json={"range": range_val, "sid": "FACEBOOK"}
            )
        if res.status_code == 200:
            data = res.json()
            if data.get("meta", {}).get("code") == 200:
                return data.get("data", {})
    except Exception as e:
        logger.error(f"VOLTX get_number error: {e}")
    return None

async def voltx_check_otp(number: str, wait: int = 90) -> Optional[str]:
    clean = number.replace("+", "").strip()
    start = time.time()
    while (time.time() - start) < wait:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.get(f"{VOLTX_BASE_URL}/console", headers=voltx_headers())
            if res.status_code == 200:
                data = res.json()
                hits = data.get("data", {}).get("hits", [])
                for hit in hits:
                    hit_range = hit.get("range", "").upper().replace("XXX", "").strip()
                    msg = hit.get("message", "")
                    if clean[:6] in hit_range or hit_range in clean[:8]:
                        match = re.search(r'\b(\d{5,8})\b', msg)
                        if match:
                            return match.group(1)
        except Exception as e:
            logger.error(f"VOLTX OTP check error: {e}")
        await asyncio.sleep(5)
    return None

# ══════════════════════════════════════════
#         HELPERS
# ══════════════════════════════════════════
COUNTRY_FLAGS = {
    "224": "🇬🇳", "225": "🇨🇮", "236": "🇨🇲", "229": "🇧🇯",
    "228": "🇹🇬", "221": "🇸🇳", "223": "🇲🇱", "226": "🇧🇫",
    "227": "🇳🇪", "230": "🇲🇺", "231": "🇱🇷", "232": "🇸🇱",
    "233": "🇬🇭", "234": "🇳🇬", "235": "🇹🇩", "237": "🇨🇲",
    "238": "🇨🇻", "240": "🇬🇶", "241": "🇬🇦", "242": "🇨🇬",
    "243": "🇨🇩", "244": "🇦🇴", "245": "🇬🇼", "246": "🇮🇴",
    "248": "🇸🇨", "249": "🇸🇩", "250": "🇷🇼", "251": "🇪🇹",
    "252": "🇸🇴", "253": "🇩🇯", "254": "🇰🇪", "255": "🇹🇿",
    "256": "🇺🇬", "257": "🇧🇮", "258": "🇲🇿", "260": "🇿🇲",
    "261": "🇲🇬", "263": "🇿🇼", "264": "🇳🇦", "265": "🇲🇼",
    "266": "🇱🇸", "267": "🇧🇼", "268": "🇸🇿", "269": "🇰🇲",
    "27":  "🇿🇦", "290": "🇸🇭", "291": "🇪🇷", "297": "🇦🇼",
    "298": "🇫🇴", "299": "🇬🇱",
}

COUNTRY_NAMES = {
    "224": "Guinea", "225": "Ivory Coast", "236": "Central Africa",
    "229": "Benin", "228": "Togo", "221": "Senegal", "223": "Mali",
    "226": "Burkina Faso", "227": "Niger", "230": "Mauritius",
    "231": "Liberia", "232": "Sierra Leone", "233": "Ghana",
    "234": "Nigeria", "235": "Chad", "237": "Cameroon",
    "241": "Gabon", "242": "Congo", "243": "DR Congo",
    "250": "Rwanda", "251": "Ethiopia", "254": "Kenya",
    "255": "Tanzania", "256": "Uganda", "260": "Zambia",
    "261": "Madagascar", "263": "Zimbabwe", "27": "South Africa",
}

def get_flag(range_val: str) -> str:
    clean = range_val.upper().replace("XXX", "").replace("X", "").strip()
    for code in sorted(COUNTRY_FLAGS.keys(), key=len, reverse=True):
        if clean.startswith(code):
            return COUNTRY_FLAGS[code]
    return "🌍"

def get_country(range_val: str) -> str:
    clean = range_val.upper().replace("XXX", "").replace("X", "").strip()
    for code in sorted(COUNTRY_NAMES.keys(), key=len, reverse=True):
        if clean.startswith(code):
            return COUNTRY_NAMES[code]
    return clean[:6] + "..."

def gen_password() -> str:
    chars = string.ascii_letters + string.digits + "!@#$"
    pwd = (
        random.choice(string.ascii_uppercase) +
        random.choice(string.ascii_lowercase) +
        random.choice(string.digits) +
        random.choice("!@#$") +
        "".join(random.choices(chars, k=6))
    )
    return "".join(random.sample(pwd, len(pwd)))

def gen_name() -> tuple:
    first = random.choice([
        "James", "John", "Robert", "Michael", "William", "David",
        "Emma", "Olivia", "Ava", "Isabella", "Sophia", "Mia",
        "Amara", "Fatou", "Kofi", "Kwame", "Seun", "Chioma"
    ])
    last = random.choice([
        "Smith", "Johnson", "Williams", "Brown", "Jones",
        "Diallo", "Traore", "Koné", "Mensah", "Okafor", "Nwosu"
    ])
    return first, last

def gen_birthday() -> tuple:
    return random.randint(1, 28), random.randint(1, 12), random.randint(1985, 2002)

def mask_number(number: str) -> str:
    clean = number.replace("+", "").strip()
    if len(clean) > 8:
        return "+" + clean[:4] + "****" + clean[-3:]
    return number

def parse_proxy(proxy_str: str, proxy_type: str) -> dict:
    parts = proxy_str.strip().split(":")
    if len(parts) < 2:
        return {}
    username, password = parts[0], parts[1]
    if proxy_type == "9proxy":
        return {"host": "niceproxy.io", "port": 17521, "user": username, "pass": password, "protocol": "socks5"}
    elif proxy_type == "abc":
        return {"host": "43.131.1.47", "port": 4950, "user": username, "pass": password, "protocol": "socks5"}
    return {}

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def save_account_to_file(number: str, uid: str, password: str, cookies: str):
    with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
        f.write(f"{number}|{uid}|{password}|{cookies}\n")

# ══════════════════════════════════════════
#         SELENIUM FACEBOOK CREATOR
# ══════════════════════════════════════════
def get_chrome_driver(proxy: dict = None) -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,800")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-extensions")
    options.add_argument("--remote-debugging-port=9222")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Linux; Android 11; Pixel 5) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    )
    # Railway / Linux Chrome binary locations
    for binary in ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"]:
        if os.path.exists(binary):
            options.binary_location = binary
            break

    if proxy and proxy.get("host"):
        ext = _proxy_extension(proxy)
        if ext:
            options.add_extension(ext)

    # Railway / Linux chromedriver locations
    for driver_path in ["/usr/bin/chromedriver", "/usr/local/bin/chromedriver"]:
        if os.path.exists(driver_path):
            try:
                service = Service(driver_path)
                driver = webdriver.Chrome(service=service, options=options)
                driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                return driver
            except Exception as e:
                logger.warning(f"Driver at {driver_path} failed: {e}")
                continue

    # Fallback: let selenium find it automatically
    driver = webdriver.Chrome(options=options)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return driver

def _proxy_extension(proxy: dict) -> Optional[str]:
    try:
        import zipfile, tempfile
        manifest = json.dumps({
            "version": "1.0.0",
            "manifest_version": 2,
            "name": "Proxy",
            "permissions": ["proxy", "tabs", "unlimitedStorage", "storage", "<all_urls>", "webRequest", "webRequestBlocking"],
            "background": {"scripts": ["background.js"]},
            "minimum_chrome_version": "22.0.0"
        })
        background = f"""
var config = {{
    mode: "fixed_servers",
    rules: {{
        singleProxy: {{
            scheme: "{proxy.get('protocol', 'socks5')}",
            host: "{proxy['host']}",
            port: parseInt({proxy['port']})
        }},
        bypassList: ["localhost"]
    }}
}};
chrome.proxy.settings.set({{value: config, scope: "regular"}}, function() {{}});
function callbackFn(details) {{
    return {{
        authCredentials: {{
            username: "{proxy['user']}",
            password: "{proxy['pass']}"
        }}
    }};
}}
chrome.webRequest.onAuthRequired.addListener(
    callbackFn,
    {{urls: ["<all_urls>"]}},
    ["blocking"]
);
"""
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        with zipfile.ZipFile(tmp.name, "w") as zf:
            zf.writestr("manifest.json", manifest)
            zf.writestr("background.js", background)
        return tmp.name
    except Exception as e:
        logger.error(f"Proxy extension error: {e}")
        return None

def wait_el(driver, by, value, timeout=15):
    return WebDriverWait(driver, timeout).until(EC.presence_of_element_located((by, value)))

def click_el(driver, by, value, timeout=10):
    el = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((by, value)))
    driver.execute_script("arguments[0].click();", el)
    return el

# ── FIX: removed nested asyncio.run() inside executor ──
def _sync_create_fb_account(number: str, password: str, proxy: dict = None) -> dict:
    """Synchronous FB account creator — runs in thread executor."""
    result = {"success": False, "uid": None, "cookies": None, "error": None}
    driver = None
    first, last = gen_name()
    day, month, year = gen_birthday()
    clean_num = number.replace("+", "").strip()

    try:
        driver = get_chrome_driver(proxy)
        driver.get("https://m.facebook.com/r.php?locale=en_US")
        time.sleep(3)

        wait_el(driver, By.NAME, "firstname").send_keys(first)
        wait_el(driver, By.NAME, "lastname").send_keys(last)
        phone_field = driver.find_element(By.NAME, "reg_email__")
        phone_field.clear()
        phone_field.send_keys(clean_num)
        try:
            re_phone = driver.find_element(By.NAME, "reg_email_confirmation__")
            re_phone.send_keys(clean_num)
        except Exception:
            pass
        wait_el(driver, By.NAME, "reg_passwd__").send_keys(password)
        Select(wait_el(driver, By.ID, "month")).select_by_value(str(month))
        Select(wait_el(driver, By.ID, "day")).select_by_value(str(day))
        Select(wait_el(driver, By.ID, "year")).select_by_value(str(year))
        try:
            driver.find_element(By.XPATH, "//input[@name='sex' and @value='2']").click()
        except Exception:
            pass
        click_el(driver, By.NAME, "websubmit")
        time.sleep(4)

        result["success"] = True
        cookies = driver.get_cookies()
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
        result["cookies"] = cookie_str[:500]

        try:
            uid_match = re.search(r'"userID":"(\d+)"', driver.page_source)
            if uid_match:
                result["uid"] = uid_match.group(1)
        except Exception:
            pass

    except Exception as e:
        result["error"] = str(e)[:100]
        logger.error(f"FB create error: {e}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
    return result

async def create_fb_account(number: str, password: str, proxy: dict = None) -> dict:
    """Async wrapper — runs sync Selenium in thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _sync_create_fb_account, number, password, proxy
    )

# ══════════════════════════════════════════
#         ACCOUNT CREATION WORKER
# ══════════════════════════════════════════
async def worker(user_id: int, bot, thread_id: int):
    sess = get_session(user_id)
    range_val = sess["selected_range"]
    proxy = None
    if sess["proxy_type"] and sess["proxy_user"]:
        proxy = {
            "host": sess["proxy_host"],
            "port": sess["proxy_port"],
            "user": sess["proxy_user"],
            "pass": sess["proxy_pass"],
            "protocol": "socks5"
        }

    while sess["running"]:
        try:
            num_data = await voltx_get_number(range_val)
            if not num_data:
                await asyncio.sleep(5)
                continue

            number = num_data.get("full_number", "")
            if not number:
                sess["stats"]["dead"] += 1
                continue

            sess["stats"]["searched"] += 1
            password = (
                sess["manual_password"]
                if sess["password_mode"] == "manual" and sess["manual_password"]
                else gen_password()
            )

            # ── FIX: clean async call (no nested asyncio.run) ──
            fb_result = await create_fb_account(number, password, proxy)

            if not fb_result["success"]:
                sess["stats"]["dead"] += 1
                await update_dashboard(user_id, bot)
                continue

            sess["stats"]["sms_pushed"] += 1
            await update_dashboard(user_id, bot)

            otp = await voltx_check_otp(number, wait=90)
            if not otp:
                sess["stats"]["dead"] += 1
                await update_dashboard(user_id, bot)
                continue

            uid = fb_result.get("uid") or "N/A"
            cookies = fb_result.get("cookies") or "N/A"
            sess["stats"]["verified"] += 1
            sess["accounts"].append({
                "number": number, "password": password,
                "uid": uid, "cookies": cookies
            })
            save_account_to_file(number, uid, password, cookies)

            await bot.send_message(
                chat_id=sess["chat_id"],
                text=(
                    f"✅ *Account Created!*\n"
                    f"📱 Number: `{number}`\n"
                    f"🔑 Password: `{password}`\n"
                    f"🆔 UID: `{uid}`\n"
                    f"🍪 Cookies: `{cookies[:80]}...`"
                ),
                parse_mode="Markdown"
            )
            await update_dashboard(user_id, bot)

        except Exception as e:
            logger.error(f"Worker {thread_id} error: {e}")
            await asyncio.sleep(3)

async def start_creation(user_id: int, bot):
    sess = get_session(user_id)
    sess["running"] = True
    sess["stats"] = {"verified": 0, "sms_pushed": 0, "searched": 0, "dead": 0, "total": 0}
    tasks = [asyncio.create_task(worker(user_id, bot, i)) for i in range(sess["threads"])]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass

# ══════════════════════════════════════════
#         DASHBOARD
# ══════════════════════════════════════════
async def update_dashboard(user_id: int, bot):
    sess = get_session(user_id)
    if not sess.get("dashboard_msg_id") or not sess.get("chat_id"):
        return
    s = sess["stats"]
    rng = sess["selected_range"] or "N/A"
    flag = get_flag(rng)
    country = get_country(rng)
    text = (
        f"🤖 *GEN BETA TEAM \\| VIP OPERATION* ✨\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎯 Target: `{rng}` {flag} {country}\n"
        f"⚡ Threads: {sess['threads']}\n\n"
        f"*\\[ LIVE METRICS \\]* •••\n"
        f"├ ✅ Verified Accounts : \\[ {s['verified']} \\]\n"
        f"├ 📨 SMS Pushed        : \\[ {s['sms_pushed']} \\]\n"
        f"├ ⏳ Total Searched    : \\[ {s['searched']} \\]\n"
        f"└ 🚫 Dead / Banned     : \\[ {s['dead']} \\]\n\n"
        f"_System is injecting payloads\\.\\.\\. Please wait\\._\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📦 View Accounts", callback_data="view_accounts"),
            InlineKeyboardButton("🔴 Stop", callback_data="stop_creator")
        ]
    ])
    try:
        await bot.edit_message_text(
            chat_id=sess["chat_id"],
            message_id=sess["dashboard_msg_id"],
            text=text,
            parse_mode="MarkdownV2",
            reply_markup=kb
        )
    except Exception:
        pass

# ══════════════════════════════════════════
#         MAIN MENU
# ══════════════════════════════════════════
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.effective_message.reply_text("⛔ Access Denied.")
        return
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📱 Get Number", callback_data="get_number"),
            InlineKeyboardButton("🔌 Proxy Setup", callback_data="proxy_setup")
        ],
        [
            InlineKeyboardButton("📦 My Accounts", callback_data="my_accounts"),
            InlineKeyboardButton("⚙️ Settings", callback_data="settings")
        ]
    ])
    await update.effective_message.reply_text(
        "💎 *GEN BETA TEAM*\n\n"
        "Facebook Automatic Bot\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Select an option below:",
        parse_mode="Markdown",
        reply_markup=kb
    )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)

# ══════════════════════════════════════════
#         CALLBACK HANDLER
# ══════════════════════════════════════════
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    sess = get_session(user_id)
    sess["chat_id"] = query.message.chat_id

    if data == "main_menu":
        await show_main_menu(update, context)
        return

    if data == "get_number":
        await query.edit_message_text("⏳ Loading ranges...")
        ranges = await voltx_get_ranges()
        if not ranges:
            await query.edit_message_text(
                "❌ No active ranges found. Try again later.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Back", callback_data="main_menu")
                ]])
            )
            return
        country_map = {}
        for rng in ranges:
            country = get_country(rng)
            if country not in country_map:
                country_map[country] = {"flag": get_flag(rng), "ranges": []}
            country_map[country]["ranges"].append(rng)
        buttons = []
        for cname, cdata in list(country_map.items())[:20]:
            count = len(cdata["ranges"])
            buttons.append([InlineKeyboardButton(
                f"{cdata['flag']} {cname} ({count})",
                callback_data=f"select_country:{cname}"
            )])
        buttons.append([InlineKeyboardButton("◀️ Back", callback_data="main_menu")])
        await query.edit_message_text(
            "🌍 *Select Country:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("select_country:"):
        cname = data.split(":", 1)[1]
        ranges = await voltx_get_ranges()
        buttons = []
        for rng in ranges:
            if get_country(rng) == cname:
                flag = get_flag(rng)
                clean = rng.upper().replace("XXX", "").strip()
                buttons.append([InlineKeyboardButton(
                    f"{flag} {clean}XXX",
                    callback_data=f"select_range:{rng}"
                )])
        buttons.append([InlineKeyboardButton("◀️ Back", callback_data="get_number")])
        await query.edit_message_text(
            "📡 *Select Range:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("select_range:"):
        rng = data.split(":", 1)[1]
        sess["selected_range"] = rng
        flag = get_flag(rng)
        country = get_country(rng)
        buttons = [
            [
                InlineKeyboardButton("⚡ 3 Threads", callback_data="threads:3"),
                InlineKeyboardButton("🔥 5 Threads", callback_data="threads:5"),
                InlineKeyboardButton("💥 10 Threads", callback_data="threads:10"),
            ],
            [InlineKeyboardButton("◀️ Back", callback_data="get_number")]
        ]
        await query.edit_message_text(
            f"{flag} *{country}* — `{rng}`\n\n⚡ Select thread count:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("threads:"):
        sess["threads"] = int(data.split(":")[1])
        s = sess["stats"]
        rng = sess["selected_range"]
        flag = get_flag(rng)
        country = get_country(rng)
        text = (
            f"🤖 *GEN BETA TEAM \\| VIP OPERATION* ✨\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🎯 Target: `{rng}` {flag} {country}\n"
            f"⚡ Threads: {sess['threads']}\n\n"
            f"*\\[ LIVE METRICS \\]* •••\n"
            f"├ ✅ Verified Accounts : \\[ 0 \\]\n"
            f"├ 📨 SMS Pushed        : \\[ 0 \\]\n"
            f"├ ⏳ Total Searched    : \\[ 0 \\]\n"
            f"└ 🚫 Dead / Banned     : \\[ 0 \\]\n\n"
            f"_Starting engine\\.\\.\\._\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📦 View Accounts", callback_data="view_accounts"),
            InlineKeyboardButton("🔴 Stop", callback_data="stop_creator")
        ]])
        msg = await query.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=kb)
        sess["dashboard_msg_id"] = msg.message_id
        asyncio.create_task(start_creation(user_id, context.bot))
        return

    if data == "stop_creator":
        sess["running"] = False
        await query.edit_message_text(
            f"🔴 *Stopped!*\n\n"
            f"✅ Verified: {sess['stats']['verified']}\n"
            f"📨 SMS Pushed: {sess['stats']['sms_pushed']}\n"
            f"⏳ Searched: {sess['stats']['searched']}\n"
            f"🚫 Dead: {sess['stats']['dead']}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📦 My Accounts", callback_data="my_accounts"),
                InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")
            ]])
        )
        return

    if data == "view_accounts":
        accounts = sess.get("accounts", [])
        if not accounts:
            await query.answer("No accounts yet!", show_alert=True)
            return
        text = f"📦 *My Accounts ({len(accounts)})*\n\n"
        for i, acc in enumerate(accounts[-5:], 1):
            text += (
                f"*{i}.* 📱 `{acc['number']}`\n"
                f"    🔑 `{acc['password']}`\n"
                f"    🆔 `{acc['uid']}`\n\n"
            )
        await query.answer()
        await context.bot.send_message(
            chat_id=sess["chat_id"],
            text=text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📁 Download TXT", callback_data="download_txt")
            ]])
        )
        return

    if data == "my_accounts":
        accounts = sess.get("accounts", [])
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"📋 View List ({len(accounts)})", callback_data="view_accounts"),
                InlineKeyboardButton("📁 Download TXT", callback_data="download_txt")
            ],
            [InlineKeyboardButton("🗑️ Clear All", callback_data="clear_accounts")],
            [InlineKeyboardButton("◀️ Back", callback_data="main_menu")]
        ])
        await query.edit_message_text(
            f"📦 *My Accounts*\n\nTotal: {len(accounts)} accounts",
            parse_mode="Markdown",
            reply_markup=kb
        )
        return

    if data == "download_txt":
        accounts = sess.get("accounts", [])
        if not accounts:
            await query.answer("No accounts to download!", show_alert=True)
            return
        content = "\n".join([
            f"{a['number']}|{a['uid']}|{a['password']}|{a['cookies']}"
            for a in accounts
        ])
        fname = f"accounts_{user_id}_{int(time.time())}.txt"
        with open(fname, "w") as f:
            f.write(content)
        with open(fname, "rb") as doc:
            await context.bot.send_document(
                chat_id=sess["chat_id"],
                document=doc,
                filename=fname,
                caption=f"📁 *{len(accounts)} Accounts*\nFormat: Number|UID|Password|Cookies",
                parse_mode="Markdown"
            )
        os.remove(fname)
        return

    if data == "clear_accounts":
        sess["accounts"] = []
        await query.answer("✅ Cleared!", show_alert=True)
        return

    if data == "proxy_setup":
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔌 9Proxy", callback_data="proxy_type:9proxy"),
                InlineKeyboardButton("🌐 ABC Proxy", callback_data="proxy_type:abc"),
            ],
            [InlineKeyboardButton("🚫 No Proxy", callback_data="proxy_type:none")],
            [InlineKeyboardButton("◀️ Back", callback_data="main_menu")]
        ])
        current = sess.get("proxy_type") or "None"
        await query.edit_message_text(
            f"🔌 *Proxy Setup*\n\nCurrent: `{current}`\n\nSelect proxy type:",
            parse_mode="Markdown",
            reply_markup=kb
        )
        return

    if data.startswith("proxy_type:"):
        ptype = data.split(":")[1]
        if ptype == "none":
            sess["proxy_type"] = None
            sess["proxy_user"] = None
            sess["proxy_pass"] = None
            await query.edit_message_text(
                "✅ Proxy disabled.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Back", callback_data="main_menu")
                ]])
            )
            return
        sess["proxy_type"] = ptype
        context.user_data["awaiting"] = "proxy_input"
        host = "niceproxy.io:17521" if ptype == "9proxy" else "43.131.1.47:4950"
        await query.edit_message_text(
            f"🔌 *{ptype.upper()} Proxy Setup*\n\n"
            f"Host: `{host}`\n\n"
            f"Send your proxy in format:\n`username:password`",
            parse_mode="Markdown"
        )
        return

    if data == "settings":
        sess = get_session(user_id)
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"🔑 Password: {'Random 🎲' if sess['password_mode'] == 'random' else 'Manual ✏️'}",
                    callback_data="toggle_password"
                )
            ],
            [InlineKeyboardButton("◀️ Back", callback_data="main_menu")]
        ])
        await query.edit_message_text(
            "⚙️ *Settings*",
            parse_mode="Markdown",
            reply_markup=kb
        )
        return

    if data == "toggle_password":
        if sess["password_mode"] == "random":
            sess["password_mode"] = "manual"
            context.user_data["awaiting"] = "manual_password"
            await query.edit_message_text(
                "✏️ *Manual Password*\n\nSend your password:",
                parse_mode="Markdown"
            )
        else:
            sess["password_mode"] = "random"
            await query.edit_message_text(
                "✅ Password mode: *Random* 🎲",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Back", callback_data="settings")
                ]])
            )
        return

# ══════════════════════════════════════════
#         MESSAGE HANDLER
# ══════════════════════════════════════════
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    sess = get_session(user_id)
    awaiting = context.user_data.get("awaiting")
    text = update.message.text.strip()

    if awaiting == "proxy_input":
        context.user_data.pop("awaiting")
        proxy = parse_proxy(text, sess["proxy_type"])
        if not proxy:
            await update.message.reply_text("❌ Invalid format. Use: `username:password`", parse_mode="Markdown")
            return
        sess["proxy_host"] = proxy["host"]
        sess["proxy_port"] = proxy["port"]
        sess["proxy_user"] = proxy["user"]
        sess["proxy_pass"] = proxy["pass"]
        await update.message.reply_text(
            f"✅ *Proxy Saved!*\n\n"
            f"Type: `{sess['proxy_type'].upper()}`\n"
            f"Host: `{proxy['host']}:{proxy['port']}`\n"
            f"User: `{proxy['user']}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")
            ]])
        )
        return

    if awaiting == "manual_password":
        context.user_data.pop("awaiting")
        sess["manual_password"] = text
        await update.message.reply_text(
            f"✅ Password set: `{text}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")
            ]])
        )
        return

# ══════════════════════════════════════════
#         MAIN
# ══════════════════════════════════════════
def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set in environment!")
    if not VOLTX_API_KEY:
        raise ValueError("VOLTX_API_KEY not set in environment!")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("🤖 GEN BETA TEAM Bot starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
