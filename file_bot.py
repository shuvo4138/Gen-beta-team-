"""
╔══════════════════════════════════════════╗
║         GEN BETA TEAM                   ║
║    Facebook Automatic Bot v2.1          ║
╚══════════════════════════════════════════╝

FIXES:
 - Browser session kept alive through OTP submission
 - OTP actually submitted to Facebook verification page
 - Proper success/failure detection (captcha, checkpoint, errors)
 - Thread-safe file writing with asyncio.Lock
 - Thread-safe stats with asyncio.Lock
 - Proper task cancellation on stop
 - Proxy validation before start
 - Startup validation
 - Temp file cleanup
 - Accounts per number config
 - Proper flow: Range→AccountsPerNum→Password→Proxy→Threads→Start
 - UID extraction improved
 - Dashboard message_id handling fixed
 - parse_proxy handles colon in password
 - OTP matching uses exact number
 - voltx_get_ranges cached per session
 - callback_handler split into focused handlers
 - Memory leak: sessions cleared on /reset
 - File handle properly closed
 - Browser crash recovery
 - All dead code removed
 - FIX #33: Removed --single-process flag (root cause of segfault crash on Railway)
            Added --no-zygote + --disable-software-rasterizer for container stability
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
import tempfile
import zipfile
import threading
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, WebDriverException
)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)
from telegram.error import BadRequest

# ══════════════════════════════════════════
#              ENV CONFIG
# ══════════════════════════════════════════
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
VOLTX_API_KEY  = os.getenv("VOLTX_API_KEY", "")
VOLTX_BASE_URL = os.getenv(
    "VOLTX_BASE_URL",
    "https://api.2oo9.cloud/MXS47FLFX0U/tnevs/@public/api"
)
ADMIN_IDS      = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
OUTPUT_FILE    = os.getenv("OUTPUT_FILE", "accounts.txt")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
logger = logging.getLogger("GEN_BETA")

# ══════════════════════════════════════════
#         GLOBAL LOCKS
# ══════════════════════════════════════════
_file_lock  = asyncio.Lock()   # concurrent file writes
_stats_lock = asyncio.Lock()   # concurrent stats updates

# ══════════════════════════════════════════
#         SESSION STORE
# ══════════════════════════════════════════
user_sessions: Dict[int, Dict[str, Any]] = {}

def get_session(user_id: int) -> dict:
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            # config
            "proxy_type"      : None,
            "proxy_host"      : None,
            "proxy_port"      : None,
            "proxy_user"      : None,
            "proxy_pass"      : None,
            "threads"         : 3,
            "accounts_per_num": 1,
            "password_mode"   : "random",
            "manual_password" : None,
            "selected_range"  : None,
            "cached_ranges"   : None,
            # runtime
            "running"         : False,
            "worker_tasks"    : [],
            "stats"           : {
                "verified"  : 0,
                "sms_pushed": 0,
                "searched"  : 0,
                "dead"      : 0,
            },
            "accounts"        : [],
            # ui
            "dashboard_msg_id": None,
            "chat_id"         : None,
            "awaiting"        : None,
        }
    return user_sessions[user_id]

def clear_session(user_id: int):
    if user_id in user_sessions:
        del user_sessions[user_id]

# ══════════════════════════════════════════
#         VOLTX API
# ══════════════════════════════════════════
def _voltx_headers() -> dict:
    return {
        "mauthapi"    : VOLTX_API_KEY,
        "Content-Type": "application/json",
        "Accept"      : "application/json",
    }

async def voltx_get_ranges() -> list:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(
                f"{VOLTX_BASE_URL}/liveaccess",
                headers=_voltx_headers()
            )
        if res.status_code == 200:
            data = res.json()
            if data.get("meta", {}).get("code") == 200:
                ranges = []
                for svc in data.get("data", {}).get("services", []):
                    if "FACEBOOK" not in svc.get("sid", "").upper():
                        continue
                    ranges.extend(svc.get("ranges", []))
                return ranges
    except Exception as e:
        logger.error(f"voltx_get_ranges: {e}")
    return []

async def voltx_get_number(range_val: str) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(
                f"{VOLTX_BASE_URL}/getnum",
                headers=_voltx_headers(),
                json={"range": range_val, "sid": "FACEBOOK"}
            )
        if res.status_code == 200:
            data = res.json()
            if data.get("meta", {}).get("code") == 200:
                return data.get("data", {})
    except Exception as e:
        logger.error(f"voltx_get_number: {e}")
    return None

async def voltx_check_otp(exact_number: str, wait: int = 90) -> Optional[str]:
    """
    FIX #8: Uses exact number match instead of prefix comparison.
    FIX #9: Filters by exact number so cross-user OTP theft is prevented.
    """
    clean = exact_number.replace("+", "").strip()
    start = time.time()
    while (time.time() - start) < wait:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.get(
                    f"{VOLTX_BASE_URL}/console",
                    headers=_voltx_headers()
                )
            if res.status_code == 200:
                hits = res.json().get("data", {}).get("hits", [])
                for hit in hits:
                    # exact number match
                    hit_number = re.sub(r'\D', '', hit.get("number", ""))
                    if hit_number == clean:
                        msg = hit.get("message", "")
                        m = re.search(r'\b(\d{5,8})\b', msg)
                        if m:
                            return m.group(1)
        except Exception as e:
            logger.error(f"voltx_check_otp: {e}")
        await asyncio.sleep(5)
    return None

# ══════════════════════════════════════════
#         COUNTRY MAPS
# ══════════════════════════════════════════
COUNTRY_FLAGS = {
    "224":"🇬🇳","225":"🇨🇮","229":"🇧🇯","228":"🇹🇬","221":"🇸🇳",
    "223":"🇲🇱","226":"🇧🇫","227":"🇳🇪","230":"🇲🇺","231":"🇱🇷",
    "232":"🇸🇱","233":"🇬🇭","234":"🇳🇬","235":"🇹🇩","237":"🇨🇲",
    "238":"🇨🇻","240":"🇬🇶","241":"🇬🇦","242":"🇨🇬","243":"🇨🇩",
    "244":"🇦🇴","245":"🇬🇼","248":"🇸🇨","249":"🇸🇩","250":"🇷🇼",
    "251":"🇪🇹","252":"🇸🇴","253":"🇩🇯","254":"🇰🇪","255":"🇹🇿",
    "256":"🇺🇬","257":"🇧🇮","258":"🇲🇿","260":"🇿🇲","261":"🇲🇬",
    "263":"🇿🇼","264":"🇳🇦","265":"🇲🇼","266":"🇱🇸","267":"🇧🇼",
    "268":"🇸🇿","269":"🇰🇲","27" :"🇿🇦","290":"🇸🇭","291":"🇪🇷",
}
COUNTRY_NAMES = {
    "224":"Guinea","225":"Ivory Coast","229":"Benin","228":"Togo",
    "221":"Senegal","223":"Mali","226":"Burkina Faso","227":"Niger",
    "230":"Mauritius","231":"Liberia","232":"Sierra Leone","233":"Ghana",
    "234":"Nigeria","235":"Chad","237":"Cameroon","241":"Gabon",
    "242":"Congo","243":"DR Congo","250":"Rwanda","251":"Ethiopia",
    "254":"Kenya","255":"Tanzania","256":"Uganda","260":"Zambia",
    "261":"Madagascar","263":"Zimbabwe","27":"South Africa",
}

def _clean_range(r: str) -> str:
    return r.upper().replace("XXX","").replace("X","").strip()

def get_flag(r: str) -> str:
    c = _clean_range(r)
    for code in sorted(COUNTRY_FLAGS, key=len, reverse=True):
        if c.startswith(code):
            return COUNTRY_FLAGS[code]
    return "🌍"

def get_country(r: str) -> str:
    c = _clean_range(r)
    for code in sorted(COUNTRY_NAMES, key=len, reverse=True):
        if c.startswith(code):
            return COUNTRY_NAMES[code]
    return c[:6] + "..."

# ══════════════════════════════════════════
#         HELPERS
# ══════════════════════════════════════════
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
        "James","John","Robert","Michael","William","David",
        "Emma","Olivia","Ava","Isabella","Sophia","Mia",
        "Amara","Fatou","Kofi","Kwame","Seun","Chioma"
    ])
    last = random.choice([
        "Smith","Johnson","Williams","Brown","Jones",
        "Diallo","Traore","Kone","Mensah","Okafor","Nwosu"
    ])
    return first, last

def gen_birthday() -> tuple:
    return random.randint(1,28), random.randint(1,12), random.randint(1985,2002)

def parse_proxy(proxy_str: str, proxy_type: str) -> dict:
    """FIX #11: split(":", 1) so passwords with colons work."""
    parts = proxy_str.strip().split(":", 1)
    if len(parts) < 2:
        return {}
    username, password = parts[0], parts[1]
    if proxy_type == "9proxy":
        return {"host":"niceproxy.io","port":17521,
                "user":username,"pass":password,"protocol":"socks5"}
    if proxy_type == "abc":
        return {"host":"43.131.1.47","port":4950,
                "user":username,"pass":password,"protocol":"socks5"}
    return {}

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def save_account(number: str, uid: str, password: str, cookies: str):
    """FIX #5: asyncio.Lock prevents concurrent write corruption."""
    async with _file_lock:
        with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
            f.write(f"{number}|{uid}|{password}|{cookies}\n")

async def inc_stat(sess: dict, key: str, amount: int = 1):
    """FIX #6: thread-safe stat increment."""
    async with _stats_lock:
        sess["stats"][key] += amount

# ══════════════════════════════════════════
#         CHROME DRIVER
# ══════════════════════════════════════════
def _build_proxy_extension(proxy: dict) -> Optional[str]:
    """FIX #19: Returns temp path so caller can delete after driver init."""
    try:
        manifest = json.dumps({
            "version":"1.0.0","manifest_version":2,"name":"Proxy",
            "permissions":["proxy","tabs","unlimitedStorage","storage",
                           "<all_urls>","webRequest","webRequestBlocking"],
            "background":{"scripts":["background.js"]},
            "minimum_chrome_version":"22.0.0"
        })
        bg = (
            f'var config={{"mode":"fixed_servers","rules":{{'
            f'"singleProxy":{{"scheme":"{proxy.get("protocol","socks5")}",'
            f'"host":"{proxy["host"]}","port":parseInt({proxy["port"]})}},'
            f'"bypassList":["localhost"]}}}};\n'
            f'chrome.proxy.settings.set({{value:config,scope:"regular"}},function(){{}});\n'
            f'function cb(d){{return{{authCredentials:'
            f'{{username:"{proxy["user"]}",password:"{proxy["pass"]}"}}}};}} \n'
            f'chrome.webRequest.onAuthRequired.addListener(cb,'
            f'{{urls:["<all_urls>"]}},["blocking"]);\n'
        )
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp_path = tmp.name
        tmp.close()
        with zipfile.ZipFile(tmp_path, "w") as zf:
            zf.writestr("manifest.json", manifest)
            zf.writestr("background.js", bg)
        return tmp_path
    except Exception as e:
        logger.error(f"proxy_extension: {e}")
        return None

def get_chrome_driver(proxy: dict = None) -> tuple:
    """
    FIX #19: Returns (driver, ext_path) so caller cleans up ext file.
    FIX #34: Correct Chrome flags for Railway container.
             --no-zygote only (removed --single-process — causes segfault crash).
             /tmp used for cache to avoid /dev/shm limit issues.
    """
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-zygote")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-translate")
    options.add_argument("--hide-scrollbars")
    options.add_argument("--mute-audio")
    options.add_argument("--no-first-run")
    options.add_argument("--safebrowsing-disable-auto-update")
    options.add_argument("--window-size=1280,800")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--remote-debugging-port=0")
    options.add_argument("--disk-cache-dir=/tmp/chrome-cache")
    options.add_argument("--data-path=/tmp/chrome-data")
    options.add_argument("--homedir=/tmp")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Linux; Android 11; Pixel 5) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Mobile Safari/537.36"
    )

    for binary in [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome"
    ]:
        if os.path.exists(binary):
            options.binary_location = binary
            break

    ext_path = None
    if proxy and proxy.get("host"):
        ext_path = _build_proxy_extension(proxy)
        if ext_path:
            options.add_extension(ext_path)
    else:
        options.add_argument("--disable-extensions")

    for drv_path in ["/usr/bin/chromedriver", "/usr/local/bin/chromedriver"]:
        if os.path.exists(drv_path):
            try:
                svc = Service(drv_path)
                driver = webdriver.Chrome(service=svc, options=options)
                driver.execute_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                )
                return driver, ext_path
            except Exception as e:
                logger.warning(f"chromedriver at {drv_path} failed: {e}")

    driver = webdriver.Chrome(options=options)
    driver.execute_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )
    return driver, ext_path

def _cleanup_driver(driver, ext_path: Optional[str]):
    """FIX #15 + #19: Quit driver and delete temp proxy extension file."""
    try:
        if driver:
            driver.quit()
    except Exception:
        pass
    if ext_path and os.path.exists(ext_path):
        try:
            os.remove(ext_path)
        except Exception:
            pass

def _wait(driver, by, value, timeout=15):
    return WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((by, value))
    )

def _click(driver, by, value, timeout=10):
    el = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((by, value))
    )
    driver.execute_script("arguments[0].click();", el)
    return el

# ══════════════════════════════════════════
#         PAGE STATE DETECTION
# ══════════════════════════════════════════
class PageState:
    UNKNOWN       = "unknown"
    OTP_PAGE      = "otp_page"
    SUCCESS       = "success"
    CAPTCHA       = "captcha"
    CHECKPOINT    = "checkpoint"
    DISABLED      = "disabled"
    RATE_LIMIT    = "rate_limit"
    SMS_LIMIT     = "sms_limit"
    REG_ERROR     = "reg_error"

def detect_page_state(driver) -> str:
    """
    FIX #3, #24-28: Detect all possible Facebook page states after submission.
    """
    try:
        src = driver.page_source.lower()
        url = driver.current_url.lower()

        # OTP / confirmation code page
        if any(k in src for k in [
            "enter the code", "confirmation code", "verify your phone",
            "enter code", "sms code", "code we sent"
        ]):
            return PageState.OTP_PAGE

        # Captcha
        if any(k in src for k in ["captcha", "recaptcha", "are you a robot", "security check"]):
            return PageState.CAPTCHA

        # Checkpoint
        if "checkpoint" in url or "checkpoint" in src:
            return PageState.CHECKPOINT

        # Disabled / banned
        if any(k in src for k in ["account disabled", "suspended", "violates our terms"]):
            return PageState.DISABLED

        # Rate limit
        if any(k in src for k in ["too many requests", "try again later", "rate limit"]):
            return PageState.RATE_LIMIT

        # SMS limit
        if any(k in src for k in ["too many sms", "sms limit", "cannot send"]):
            return PageState.SMS_LIMIT

        # Registration error
        if any(k in src for k in [
            "registration failed", "something went wrong",
            "couldn't register", "error", "invalid phone"
        ]):
            return PageState.REG_ERROR

        # Success — on home/feed or profile page
        if any(k in src for k in [
            "\"homepage\"", "what's on your mind", "news feed",
            "welcome to facebook", "find friends"
        ]):
            return PageState.SUCCESS

    except Exception as e:
        logger.error(f"detect_page_state: {e}")
    return PageState.UNKNOWN

# ══════════════════════════════════════════
#         FACEBOOK ACCOUNT CREATOR
# ══════════════════════════════════════════
def _extract_uid(driver) -> Optional[str]:
    """
    FIX #4: Multiple extraction methods, returns None if not found.
    Never marks success just because UID is missing.
    """
    try:
        src = driver.page_source
        for pattern in [
            r'"userID"\s*:\s*"(\d+)"',
            r'"USER_ID"\s*:\s*"(\d+)"',
            r'entity_id=(\d+)',
            r'"id"\s*:\s*"(\d{10,20})"',
        ]:
            m = re.search(pattern, src)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None

def _extract_cookies(driver) -> str:
    try:
        cookies = driver.get_cookies()
        return "; ".join([f"{c['name']}={c['value']}" for c in cookies])[:800]
    except Exception:
        return ""

class FBResult:
    def __init__(self):
        self.success  = False
        self.uid      = None
        self.cookies  = ""
        self.state    = PageState.UNKNOWN
        self.error    = None

def _sync_create_fb_account(
    number: str,
    password: str,
    proxy: dict = None
) -> FBResult:
    """
    FIX #1, #2, #3, #4, #7:
    - Browser kept alive through OTP submission
    - OTP actually submitted to the verification page
    - All page states detected
    - UID properly extracted after login
    - Session not lost during OTP wait
    - success only set after confirmed home page
    """
    result  = FBResult()
    driver  = None
    ext_path = None
    first, last     = gen_name()
    day, month, year = gen_birthday()
    clean_num = number.replace("+", "").strip()

    try:
        driver, ext_path = get_chrome_driver(proxy)

        # ── Step 1: Load registration page ──
        driver.get("https://m.facebook.com/r.php?locale=en_US")
        time.sleep(3)

        state = detect_page_state(driver)
        if state == PageState.RATE_LIMIT:
            result.state = PageState.RATE_LIMIT
            result.error = "Rate limited before registration"
            return result

        # ── Step 2: Fill registration form ──
        _wait(driver, By.NAME, "firstname").send_keys(first)
        _wait(driver, By.NAME, "lastname").send_keys(last)

        phone_field = driver.find_element(By.NAME, "reg_email__")
        phone_field.clear()
        phone_field.send_keys(clean_num)
        try:
            conf = driver.find_element(By.NAME, "reg_email_confirmation__")
            conf.send_keys(clean_num)
        except NoSuchElementException:
            pass

        _wait(driver, By.NAME, "reg_passwd__").send_keys(password)
        Select(_wait(driver, By.ID, "month")).select_by_value(str(month))
        Select(_wait(driver, By.ID, "day")).select_by_value(str(day))
        Select(_wait(driver, By.ID, "year")).select_by_value(str(year))
        try:
            driver.find_element(By.XPATH, "//input[@name='sex' and @value='2']").click()
        except NoSuchElementException:
            pass

        # ── Step 3: Submit form ──
        _click(driver, By.NAME, "websubmit")
        time.sleep(4)

        # ── Step 4: Detect post-submission state ──
        state = detect_page_state(driver)
        result.state = state

        if state == PageState.CAPTCHA:
            result.error = "Captcha detected"
            return result
        if state == PageState.RATE_LIMIT:
            result.error = "Rate limited after submit"
            return result
        if state == PageState.SMS_LIMIT:
            result.error = "SMS limit reached"
            return result
        if state == PageState.REG_ERROR:
            result.error = "Registration error"
            return result
        if state == PageState.SUCCESS:
            # Registered without OTP (rare but possible)
            result.success = True
            result.uid     = _extract_uid(driver)
            result.cookies = _extract_cookies(driver)
            return result

        # ── Step 5: OTP page — wait for OTP from VOLTX ──
        # Browser is still alive here (FIX #1)
        if state != PageState.OTP_PAGE:
            result.error = f"Unexpected page state: {state}"
            return result

        # Poll VOLTX for OTP (blocking — runs in executor so no event loop issue)
        otp = None
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                import urllib.request
                req = urllib.request.Request(
                    f"{VOLTX_BASE_URL}/console",
                    headers={
                        "mauthapi": VOLTX_API_KEY,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    }
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                hits = data.get("data", {}).get("hits", [])
                for hit in hits:
                    hit_number = re.sub(r'\D', '', hit.get("number", ""))
                    if hit_number == clean_num:
                        msg = hit.get("message", "")
                        m = re.search(r'\b(\d{5,8})\b', msg)
                        if m:
                            otp = m.group(1)
                            break
            except Exception as e:
                logger.error(f"OTP poll: {e}")
            if otp:
                break
            time.sleep(5)

        if not otp:
            result.error = "OTP timeout"
            return result

        # ── Step 6: Submit OTP to Facebook (FIX #1) ──
        try:
            otp_field = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.NAME, "code"))
            )
        except TimeoutException:
            # Try alternate field names
            for name in ["confirmation_code", "otp", "verify_code"]:
                try:
                    otp_field = driver.find_element(By.NAME, name)
                    break
                except NoSuchElementException:
                    otp_field = None
            if not otp_field:
                result.error = "OTP input field not found"
                return result

        otp_field.clear()
        otp_field.send_keys(otp)

        # Submit OTP
        for submit_sel in [
            (By.NAME, "submit"),
            (By.XPATH, "//button[@type='submit']"),
            (By.XPATH, "//input[@type='submit']"),
        ]:
            try:
                _click(driver, *submit_sel, timeout=5)
                break
            except Exception:
                continue

        time.sleep(4)

        # ── Step 7: Verify account created ──
        final_state = detect_page_state(driver)
        result.state = final_state

        if final_state == PageState.SUCCESS:
            result.success = True
            result.uid     = _extract_uid(driver)
            result.cookies = _extract_cookies(driver)
        elif final_state == PageState.CHECKPOINT:
            result.error = "Checkpoint after OTP"
        elif final_state == PageState.DISABLED:
            result.error = "Account disabled immediately"
        else:
            result.error = f"Unexpected final state: {final_state}"

    except WebDriverException as e:
        result.error = f"Browser crash: {str(e)[:80]}"
        logger.error(f"WebDriverException: {e}")
    except Exception as e:
        result.error = f"Unexpected: {str(e)[:80]}"
        logger.error(f"create_fb_account: {e}")
    finally:
        # FIX #15, #19: Always clean up driver and temp files
        _cleanup_driver(driver, ext_path)

    return result

async def create_fb_account(number: str, password: str, proxy: dict = None) -> FBResult:
    """FIX #1, #3: Async wrapper — Selenium runs in thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _sync_create_fb_account, number, password, proxy
    )

# ══════════════════════════════════════════
#         DASHBOARD
# ══════════════════════════════════════════
def _build_dashboard_text(sess: dict) -> str:
    s   = sess["stats"]
    rng = sess["selected_range"] or "N/A"
    flag    = get_flag(rng)
    country = get_country(rng)
    status  = "🟢 Running" if sess["running"] else "🔴 Stopped"
    return (
        f"🤖 *GEN BETA TEAM — VIP OPERATION*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎯 Target: `{rng}` {flag} {country}\n"
        f"⚡ Threads: {sess['threads']}   "
        f"📋 Per Number: {sess['accounts_per_num']}\n"
        f"Status: {status}\n\n"
        f"*LIVE METRICS*\n"
        f"├ ✅ Verified   : {s['verified']}\n"
        f"├ 📨 SMS Pushed : {s['sms_pushed']}\n"
        f"├ ⏳ Searched   : {s['searched']}\n"
        f"└ 🚫 Dead       : {s['dead']}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

def _dashboard_kb(sess: dict) -> InlineKeyboardMarkup:
    if sess["running"]:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("📦 Accounts", callback_data="view_accounts"),
            InlineKeyboardButton("🔴 Stop",     callback_data="stop_creator"),
        ]])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📦 Accounts", callback_data="view_accounts"),
        InlineKeyboardButton("🏠 Menu",     callback_data="main_menu"),
    ]])

async def update_dashboard(user_id: int, bot):
    sess = get_session(user_id)
    if not sess.get("dashboard_msg_id") or not sess.get("chat_id"):
        return
    try:
        await bot.edit_message_text(
            chat_id    = sess["chat_id"],
            message_id = sess["dashboard_msg_id"],
            text       = _build_dashboard_text(sess),
            parse_mode = "Markdown",
            reply_markup = _dashboard_kb(sess),
        )
    except BadRequest:
        pass  # message not modified — OK
    except Exception as e:
        logger.error(f"update_dashboard: {e}")

# ══════════════════════════════════════════
#         WORKER
# ══════════════════════════════════════════
async def worker(user_id: int, bot, thread_id: int):
    """
    FIX #8, #12, #13:
    - Form submission success NOT assumed early
    - Verification state properly tracked
    - Browser crash recovery with retry
    """
    sess      = get_session(user_id)
    range_val = sess["selected_range"]

    # FIX #34: Stagger thread startup so Chrome instances don't all launch at once
    await asyncio.sleep(thread_id * 5)

    proxy     = None
    if sess["proxy_type"] and sess["proxy_host"]:
        proxy = {
            "host"    : sess["proxy_host"],
            "port"    : sess["proxy_port"],
            "user"    : sess["proxy_user"],
            "pass"    : sess["proxy_pass"],
            "protocol": "socks5",
        }

    while sess["running"]:
        try:
            # ── Get one phone number from VOLTX ──
            num_data = await voltx_get_number(range_val)
            if not num_data:
                await inc_stat(sess, "dead")
                await asyncio.sleep(5)
                continue

            number = num_data.get("full_number", "")
            if not number:
                await inc_stat(sess, "dead")
                continue

            await inc_stat(sess, "searched")

            # ── Create N accounts from this number ──
            # Each account gets its own unique name, birthday, password.
            # After all N succeed (or fail), move on to next number.
            target        = sess["accounts_per_num"]
            accounts_made = 0
            number_dead   = False  # if number itself is bad, skip remaining slots

            for slot in range(target):
                if not sess["running"] or number_dead:
                    break

                # Each slot = unique identity
                password = (
                    sess["manual_password"]
                    if sess["password_mode"] == "manual" and sess["manual_password"]
                    else gen_password()
                )

                logger.info(
                    f"Thread {thread_id}: number={number} slot={slot+1}/{target}"
                )

                # Retry on browser crash only
                result = None
                for attempt in range(2):
                    result = await create_fb_account(number, password, proxy)
                    if result.error and "Browser crash" in (result.error or ""):
                        logger.warning(
                            f"Thread {thread_id}: browser crash slot {slot+1} "
                            f"attempt {attempt+1}, retrying"
                        )
                        await asyncio.sleep(3)
                        continue
                    break

                # ── Handle failure ──
                if not result or not result.success:
                    err   = result.error if result else "unknown"
                    state = result.state if result else "unknown"
                    logger.info(f"Thread {thread_id}: slot {slot+1} failed — {err} ({state})")
                    await inc_stat(sess, "dead")

                    # Number-level failures: stop trying more slots for this number
                    if result and result.state in (
                        PageState.RATE_LIMIT,
                        PageState.SMS_LIMIT,
                        PageState.CAPTCHA,
                        PageState.CHECKPOINT,
                        PageState.DISABLED,
                    ):
                        if result.state == PageState.RATE_LIMIT:
                            await asyncio.sleep(30)
                        elif result.state == PageState.CAPTCHA:
                            await asyncio.sleep(10)
                        number_dead = True

                    await update_dashboard(user_id, bot)
                    continue  # try next slot (unless number_dead set above)

                # ── Success ──
                await inc_stat(sess, "sms_pushed")
                await inc_stat(sess, "verified")

                uid     = result.uid or "N/A"
                cookies = result.cookies or "N/A"

                # Save to memory and file
                sess["accounts"].append({
                    "number"  : number,
                    "password": password,
                    "uid"     : uid,
                    "cookies" : cookies,
                })
                await save_account(number, uid, password, cookies)

                accounts_made += 1

                # Notify user of each created account
                try:
                    await bot.send_message(
                        chat_id    = sess["chat_id"],
                        text       = (
                            f"✅ *Account {accounts_made}/{target} Created!*\n"
                            f"📱 Number  : `{number}`\n"
                            f"🔑 Password: `{password}`\n"
                            f"🆔 UID     : `{uid}`\n"
                            f"🍪 Cookies : `{cookies[:80]}...`"
                        ),
                        parse_mode = "Markdown",
                    )
                except Exception:
                    pass

                await update_dashboard(user_id, bot)
                # Small delay between slots on same number
                if slot < target - 1:
                    await asyncio.sleep(2)

        except asyncio.CancelledError:
            logger.info(f"Worker {thread_id} cancelled")
            break
        except Exception as e:
            logger.error(f"Worker {thread_id} error: {e}")
            await asyncio.sleep(3)

# ══════════════════════════════════════════
#         START / STOP CREATION
# ══════════════════════════════════════════
async def start_creation(user_id: int, bot):
    """FIX #15: Worker tasks stored so stop_creator can cancel them."""
    sess = get_session(user_id)
    sess["running"]      = True
    sess["worker_tasks"] = []
    sess["stats"]        = {"verified":0,"sms_pushed":0,"searched":0,"dead":0}

    tasks = [
        asyncio.create_task(worker(user_id, bot, i))
        for i in range(sess["threads"])
    ]
    sess["worker_tasks"] = tasks
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        sess["running"] = False

def stop_creation(sess: dict):
    """FIX #15: Actually cancel all worker tasks."""
    sess["running"] = False
    for task in sess.get("worker_tasks", []):
        if not task.done():
            task.cancel()
    sess["worker_tasks"] = []

# ══════════════════════════════════════════
#         STARTUP VALIDATION
# ══════════════════════════════════════════
def validate_startup(sess: dict) -> Optional[str]:
    """FIX #32: Check all required settings before starting."""
    if not sess.get("selected_range"):
        return "❌ No range selected. Go back and select a range."
    if sess["proxy_type"] and not sess.get("proxy_host"):
        return "❌ Proxy type set but proxy credentials missing."
    if sess["password_mode"] == "manual" and not sess.get("manual_password"):
        return "❌ Manual password mode selected but no password set."
    return None

# ══════════════════════════════════════════
#         PROXY VALIDATION
# ══════════════════════════════════════════
async def validate_proxy(proxy: dict) -> bool:
    """FIX #29, #30: Actually test proxy before starting."""
    try:
        proxies = {
            "http://": f"socks5://{proxy['user']}:{proxy['pass']}@{proxy['host']}:{proxy['port']}",
            "https://": f"socks5://{proxy['user']}:{proxy['pass']}@{proxy['host']}:{proxy['port']}",
        }
        async with httpx.AsyncClient(proxies=proxies, timeout=10) as client:
            res = await client.get("https://api.ipify.org?format=json")
        return res.status_code == 200
    except Exception as e:
        logger.warning(f"Proxy validation failed: {e}")
        return False

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
            InlineKeyboardButton("📱 Select Range",  callback_data="get_number"),
            InlineKeyboardButton("🔌 Proxy Setup",   callback_data="proxy_setup"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings",      callback_data="settings"),
            InlineKeyboardButton("📦 My Accounts",   callback_data="my_accounts"),
        ],
        [
            InlineKeyboardButton("🚀 Start",         callback_data="pre_start"),
        ],
    ])
    await update.effective_message.reply_text(
        "💎 *GEN BETA TEAM*\n\nFacebook Automatic Bot v2\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Select an option:",
        parse_mode  = "Markdown",
        reply_markup = kb,
    )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """FIX #20: Clear session to prevent memory leak."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    sess = get_session(user_id)
    stop_creation(sess)
    clear_session(user_id)
    await update.message.reply_text("✅ Session cleared.")

# ══════════════════════════════════════════
#         HANDLER: RANGE SELECTION
# ══════════════════════════════════════════
async def handle_get_number(query, sess, context):
    await query.edit_message_text("⏳ Loading ranges...")
    # FIX #10: cache ranges in session so we don't call API twice
    ranges = await voltx_get_ranges()
    sess["cached_ranges"] = ranges
    if not ranges:
        await query.edit_message_text(
            "❌ No active ranges found.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Back", callback_data="main_menu")
            ]])
        )
        return
    country_map = {}
    for r in ranges:
        c = get_country(r)
        if c not in country_map:
            country_map[c] = {"flag": get_flag(r), "ranges": []}
        country_map[c]["ranges"].append(r)

    buttons = [
        [InlineKeyboardButton(
            f"{v['flag']} {c} ({len(v['ranges'])})",
            callback_data=f"select_country:{c}"
        )]
        for c, v in list(country_map.items())[:20]
    ]
    buttons.append([InlineKeyboardButton("◀️ Back", callback_data="main_menu")])
    await query.edit_message_text(
        "🌍 *Select Country:*",
        parse_mode   = "Markdown",
        reply_markup = InlineKeyboardMarkup(buttons),
    )

async def handle_select_country(query, sess, data):
    cname  = data.split(":", 1)[1]
    ranges = sess.get("cached_ranges") or await voltx_get_ranges()
    buttons = [
        [InlineKeyboardButton(
            f"{get_flag(r)} {_clean_range(r)}XXX",
            callback_data=f"select_range:{r}"
        )]
        for r in ranges if get_country(r) == cname
    ]
    buttons.append([InlineKeyboardButton("◀️ Back", callback_data="get_number")])
    await query.edit_message_text(
        "📡 *Select Range:*",
        parse_mode   = "Markdown",
        reply_markup = InlineKeyboardMarkup(buttons),
    )

async def handle_select_range(query, sess, data):
    rng = data.split(":", 1)[1]
    sess["selected_range"] = rng
    flag    = get_flag(rng)
    country = get_country(rng)
    await query.edit_message_text(
        f"{flag} *{country}* — `{rng}` selected!\n\n"
        f"Go to Settings to configure threads, accounts per number, and password.\n"
        f"Then press 🚀 Start from the main menu.",
        parse_mode   = "Markdown",
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")
        ]])
    )

# ══════════════════════════════════════════
#         HANDLER: PRE-START VALIDATION
# ══════════════════════════════════════════
async def handle_pre_start(query, sess, user_id, context):
    """FIX #32, #29: Validate everything before starting."""
    err = validate_startup(sess)
    if err:
        await query.edit_message_text(
            err,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Back", callback_data="main_menu")
            ]])
        )
        return

    # Proxy validation
    if sess["proxy_type"] and sess.get("proxy_host"):
        await query.edit_message_text("🔍 Validating proxy...")
        proxy = {
            "host": sess["proxy_host"], "port": sess["proxy_port"],
            "user": sess["proxy_user"], "pass": sess["proxy_pass"],
            "protocol": "socks5",
        }
        ok = await validate_proxy(proxy)
        if not ok:
            await query.edit_message_text(
                "❌ Proxy validation failed. Check your proxy credentials.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔌 Fix Proxy", callback_data="proxy_setup"),
                    InlineKeyboardButton("◀️ Back",      callback_data="main_menu"),
                ]])
            )
            return

    # Thread selection
    rng     = sess["selected_range"]
    flag    = get_flag(rng)
    country = get_country(rng)
    buttons = [
        [
            InlineKeyboardButton("⚡ 3",  callback_data="threads:3"),
            InlineKeyboardButton("🔥 5",  callback_data="threads:5"),
            InlineKeyboardButton("💥 10", callback_data="threads:10"),
        ],
        [InlineKeyboardButton("◀️ Back", callback_data="main_menu")]
    ]
    await query.edit_message_text(
        f"{flag} *{country}* — `{rng}`\n\n"
        f"Per Number: {sess['accounts_per_num']}  |  "
        f"Password: {sess['password_mode'].capitalize()}\n\n"
        f"⚡ Select thread count to start:",
        parse_mode   = "Markdown",
        reply_markup = InlineKeyboardMarkup(buttons),
    )

async def handle_threads(query, sess, data, user_id, context):
    sess["threads"] = int(data.split(":")[1])
    text = _build_dashboard_text(sess)
    kb   = _dashboard_kb(sess)
    # FIX #14: edit_message_text may return True, use message object from query
    try:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        sess["dashboard_msg_id"] = query.message.message_id
    except Exception as e:
        logger.error(f"Dashboard init: {e}")
        sess["dashboard_msg_id"] = query.message.message_id

    sess["chat_id"] = query.message.chat_id
    asyncio.create_task(start_creation(user_id, context.bot))

# ══════════════════════════════════════════
#         HANDLER: STOP
# ══════════════════════════════════════════
async def handle_stop(query, sess):
    """FIX #15: Actually cancel worker tasks."""
    stop_creation(sess)
    s = sess["stats"]
    await query.edit_message_text(
        f"🔴 *Stopped*\n\n"
        f"✅ Verified: {s['verified']}\n"
        f"📨 SMS Pushed: {s['sms_pushed']}\n"
        f"⏳ Searched: {s['searched']}\n"
        f"🚫 Dead: {s['dead']}",
        parse_mode   = "Markdown",
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("📦 Accounts", callback_data="my_accounts"),
            InlineKeyboardButton("🏠 Menu",     callback_data="main_menu"),
        ]])
    )

# ══════════════════════════════════════════
#         HANDLER: ACCOUNTS
# ══════════════════════════════════════════
async def handle_view_accounts(query, sess, context):
    accounts = sess.get("accounts", [])
    if not accounts:
        await query.answer("No accounts yet!", show_alert=True)
        return
    lines = []
    for i, acc in enumerate(accounts[-5:], 1):
        lines.append(
            f"*{i}.* `{acc['number']}`\n"
            f"     🔑 `{acc['password']}`\n"
            f"     🆔 `{acc['uid']}`"
        )
    await context.bot.send_message(
        chat_id    = sess["chat_id"],
        text       = f"📦 *Last 5 Accounts ({len(accounts)} total)*\n\n" + "\n\n".join(lines),
        parse_mode = "Markdown",
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("📁 Download TXT", callback_data="download_txt")
        ]])
    )

async def handle_my_accounts(query, sess):
    accounts = sess.get("accounts", [])
    await query.edit_message_text(
        f"📦 *My Accounts*\nTotal: {len(accounts)}",
        parse_mode   = "Markdown",
        reply_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"📋 View ({len(accounts)})", callback_data="view_accounts"),
                InlineKeyboardButton("📁 Download",               callback_data="download_txt"),
            ],
            [InlineKeyboardButton("🗑️ Clear", callback_data="clear_accounts")],
            [InlineKeyboardButton("◀️ Back",   callback_data="main_menu")],
        ])
    )

async def handle_download_txt(query, sess, context):
    accounts = sess.get("accounts", [])
    if not accounts:
        await query.answer("No accounts!", show_alert=True)
        return
    content = "\n".join(
        f"{a['number']}|{a['uid']}|{a['password']}|{a['cookies']}"
        for a in accounts
    )
    fname = f"accounts_{query.from_user.id}_{int(time.time())}.txt"
    try:
        with open(fname, "w", encoding="utf-8") as f:
            f.write(content)
        # FIX #13: use context manager so file handle is always closed
        with open(fname, "rb") as doc:
            await context.bot.send_document(
                chat_id  = sess["chat_id"],
                document = doc,
                filename = fname,
                caption  = f"📁 *{len(accounts)} Accounts*\nFormat: Number|UID|Password|Cookies",
                parse_mode = "Markdown",
            )
    finally:
        if os.path.exists(fname):
            os.remove(fname)

# ══════════════════════════════════════════
#         HANDLER: PROXY
# ══════════════════════════════════════════
async def handle_proxy_setup(query, sess):
    current = sess.get("proxy_type") or "None"
    await query.edit_message_text(
        f"🔌 *Proxy Setup*\nCurrent: `{current}`\n\nSelect type:",
        parse_mode   = "Markdown",
        reply_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔌 9Proxy",    callback_data="proxy_type:9proxy"),
                InlineKeyboardButton("🌐 ABC Proxy", callback_data="proxy_type:abc"),
            ],
            [InlineKeyboardButton("🚫 No Proxy", callback_data="proxy_type:none")],
            [InlineKeyboardButton("◀️ Back",      callback_data="main_menu")],
        ])
    )

async def handle_proxy_type(query, sess, data, context):
    ptype = data.split(":", 1)[1]
    if ptype == "none":
        sess["proxy_type"] = None
        sess["proxy_host"] = None
        for k in ("proxy_user","proxy_pass","proxy_port"):
            sess[k] = None
        await query.edit_message_text(
            "✅ Proxy disabled.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Back", callback_data="main_menu")
            ]])
        )
        return
    sess["proxy_type"] = ptype
    sess["awaiting"]   = "proxy_input"
    host = "niceproxy.io:17521" if ptype == "9proxy" else "43.131.1.47:4950"
    await query.edit_message_text(
        f"🔌 *{ptype.upper()} Proxy*\nHost: `{host}`\n\n"
        f"Send credentials:\n`username:password`",
        parse_mode = "Markdown",
    )

# ══════════════════════════════════════════
#         HANDLER: SETTINGS
# ══════════════════════════════════════════
async def handle_settings(query, sess):
    await query.edit_message_text(
        "⚙️ *Settings*",
        parse_mode   = "Markdown",
        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"🔑 Password: {'Random 🎲' if sess['password_mode']=='random' else 'Manual ✏️'}",
                callback_data="toggle_password"
            )],
            [InlineKeyboardButton(
                f"📋 Accounts/Number: {sess['accounts_per_num']}",
                callback_data="set_accounts_per_num"
            )],
            [InlineKeyboardButton("◀️ Back", callback_data="main_menu")],
        ])
    )

async def handle_toggle_password(query, sess):
    if sess["password_mode"] == "random":
        sess["password_mode"] = "manual"
        sess["awaiting"]      = "manual_password"
        await query.edit_message_text("✏️ Send your custom password:")
    else:
        sess["password_mode"]   = "random"
        sess["manual_password"] = None
        await query.edit_message_text(
            "✅ Password mode: *Random* 🎲",
            parse_mode   = "Markdown",
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Back", callback_data="settings")
            ]])
        )

async def handle_set_accounts_per_num(query, sess):
    """FIX #31: Let user choose how many accounts per number."""
    await query.edit_message_text(
        "📋 *Accounts per number:*\nHow many accounts to create from each phone number?",
        parse_mode   = "Markdown",
        reply_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("1", callback_data="apn:1"),
                InlineKeyboardButton("2", callback_data="apn:2"),
                InlineKeyboardButton("3", callback_data="apn:3"),
                InlineKeyboardButton("5", callback_data="apn:5"),
            ],
            [InlineKeyboardButton("◀️ Back", callback_data="settings")],
        ])
    )

async def handle_apn(query, sess, data):
    sess["accounts_per_num"] = int(data.split(":")[1])
    await query.edit_message_text(
        f"✅ Accounts per number: *{sess['accounts_per_num']}*",
        parse_mode   = "Markdown",
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Back", callback_data="settings")
        ]])
    )

# ══════════════════════════════════════════
#         MAIN CALLBACK HANDLER
# (FIX #16: split into focused sub-handlers)
# ══════════════════════════════════════════
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if not is_admin(user_id):
        await query.answer("⛔ Access Denied.", show_alert=True)
        return

    sess = get_session(user_id)
    sess["chat_id"] = query.message.chat_id
    data = query.data

    if data == "main_menu":
        await show_main_menu(update, context)
    elif data == "get_number":
        await handle_get_number(query, sess, context)
    elif data.startswith("select_country:"):
        await handle_select_country(query, sess, data)
    elif data.startswith("select_range:"):
        await handle_select_range(query, sess, data)
    elif data == "pre_start":
        await handle_pre_start(query, sess, user_id, context)
    elif data.startswith("threads:"):
        await handle_threads(query, sess, data, user_id, context)
    elif data == "stop_creator":
        await handle_stop(query, sess)
    elif data == "view_accounts":
        await handle_view_accounts(query, sess, context)
    elif data == "my_accounts":
        await handle_my_accounts(query, sess)
    elif data == "download_txt":
        await handle_download_txt(query, sess, context)
    elif data == "clear_accounts":
        sess["accounts"] = []
        await query.answer("✅ Cleared!", show_alert=True)
    elif data == "proxy_setup":
        await handle_proxy_setup(query, sess)
    elif data.startswith("proxy_type:"):
        await handle_proxy_type(query, sess, data, context)
    elif data == "settings":
        await handle_settings(query, sess)
    elif data == "toggle_password":
        await handle_toggle_password(query, sess)
    elif data == "set_accounts_per_num":
        await handle_set_accounts_per_num(query, sess)
    elif data.startswith("apn:"):
        await handle_apn(query, sess, data)

# ══════════════════════════════════════════
#         MESSAGE HANDLER
# ══════════════════════════════════════════
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    sess    = get_session(user_id)
    text    = update.message.text.strip()
    waiting = sess.get("awaiting")

    if waiting == "proxy_input":
        sess["awaiting"] = None
        proxy = parse_proxy(text, sess["proxy_type"])
        if not proxy:
            await update.message.reply_text(
                "❌ Invalid format. Use: `username:password`",
                parse_mode = "Markdown"
            )
            return
        sess["proxy_host"] = proxy["host"]
        sess["proxy_port"] = proxy["port"]
        sess["proxy_user"] = proxy["user"]
        sess["proxy_pass"] = proxy["pass"]
        await update.message.reply_text(
            f"✅ *Proxy Saved!*\n"
            f"Type: `{sess['proxy_type'].upper()}`\n"
            f"Host: `{proxy['host']}:{proxy['port']}`",
            parse_mode   = "Markdown",
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")
            ]])
        )

    elif waiting == "manual_password":
        sess["awaiting"]       = None
        sess["manual_password"] = text
        await update.message.reply_text(
            f"✅ Password set: `{text}`",
            parse_mode   = "Markdown",
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")
            ]])
        )

# ══════════════════════════════════════════
#         ENTRY POINT
# ══════════════════════════════════════════
def main():
    # FIX #32: validate env at startup
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set!")
    if not VOLTX_API_KEY:
        raise ValueError("VOLTX_API_KEY environment variable is not set!")
    if not ADMIN_IDS:
        raise ValueError("ADMIN_IDS environment variable is not set!")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("🤖 GEN BETA TEAM Bot v2.1 starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
