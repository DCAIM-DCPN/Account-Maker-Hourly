#!/usr/bin/env python3
"""
TensorPix Account Maker Bot — CroxyProxy Edition

Automates bulk account creation on TensorPix via:
  - Boomlify API for disposable email addresses (domain: zikzak.site)
  - CroxyProxy web proxy for automatic IP rotation per account
  - Playwright (headless Chromium) for browser automation

Each cycle:
  1. Create a Boomlify temp inbox
  2. Navigate to TensorPix register page through CroxyProxy
  3. Fill email + password, submit registration
  4. Poll Boomlify for the verification email (up to 3 min)
  5. Open the verification link through CroxyProxy
  6. Log in through CroxyProxy and wait 5 seconds
  7. Save credentials to file

Usage:
    python3 tensorpix-bot-test-1.py --api-key YOUR_KEY --count 5
    python3 tensorpix-bot-test-1.py --api-key YOUR_KEY --count 10 --password TpixAcc2026!
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOOMLIFY_BASE_URL = "https://v1.boomlify.com"
BOOMLIFY_DOMAIN = "bobthebuidnns.opik.net"
BOOMLIFY_INBOX_TTL = "10min"

CROXYPROXY_URL = "https://www.croxyproxy.com"
CROXYPROXY_LOAD_WAIT_S = 8  # seconds to wait after entering URL

REGISTER_URL = "https://app.tensorpix.ai/register"
LOGIN_URL = "https://app.tensorpix.ai/login"

DEFAULT_PASSWORD = "TpixAcc2026!"
DEFAULT_COUNT = 10

CREDENTIALS_FILE = "account_credentials.txt"

# Retry / timeout tunables
REGISTER_MAX_RETRIES = 3
EMAIL_POLL_TIMEOUT_S = 180  # 3 minutes
EMAIL_POLL_INTERVAL_S = 6
PAGE_NAV_TIMEOUT_MS = 60_000

# Regex patterns to fish the verify link out of the email body
VERIFY_LINK_PATTERNS = [
    r"https://app\.tensorpix\.ai/verify-user/[^\s<>\"']*",  # exact match first
    r"https://[^\s<>\"']*verify-user[^\s<>\"']*",
    r"href=[\"']?(https://[^\s<>\"']*verify[^\s<>\"']*)[\"'\s>]",
    r"(https://app\.tensorpix\.ai[^\s<>\"'&]*)",
]

# TensorPix sends from this domain
TENSORPIX_SENDER_DOMAIN = "tensorpix.ai"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("tensorpix_bot")
logger.setLevel(logging.DEBUG)

_formatter = logging.Formatter(
    "[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_console = logging.StreamHandler(sys.stdout)
_console.setLevel(logging.INFO)
_console.setFormatter(_formatter)
logger.addHandler(_console)


def _add_file_handler(path: str) -> logging.FileHandler:
    fh = logging.FileHandler(path, mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_formatter)
    logger.addHandler(fh)
    return fh


# ---------------------------------------------------------------------------
# Boomlify helpers
# ---------------------------------------------------------------------------

def _boomlify_headers(api_key: str) -> dict:
    return {
        "X-API-Key": api_key.strip(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def boomlify_create_inbox(api_key: str) -> tuple[str | None, str | None]:
    """Create a temp inbox. Returns (inbox_id, email_address) or (None, None)."""
    params = urllib.parse.urlencode({
        "time": BOOMLIFY_INBOX_TTL,
        "domain": BOOMLIFY_DOMAIN,
    })
    url = f"{BOOMLIFY_BASE_URL}/api/v1/emails/create?{params}"
    req = urllib.request.Request(
        url,
        method="POST",
        data=b"{}",
        headers=_boomlify_headers(api_key),
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            if not raw:
                logger.error("Boomlify create: empty response body")
                return None, None
            payload = json.loads(raw.decode(errors="replace"))
    except urllib.error.HTTPError as exc:
        body = (exc.read().decode(errors="replace") if exc.fp else "")
        logger.error("Boomlify create HTTP %s: %s", exc.code, body[:500])
        return None, None

    # Expected: {"success": true, "email": {"id": "...", "address": "..."}, ...}
    email_obj = payload.get("email")
    if isinstance(email_obj, dict):
        inbox_id = str(email_obj.get("id", "")).strip()
        address = str(email_obj.get("address", "")).strip()
        if inbox_id and address and "@" in address:
            logger.info("Created inbox: %s  (id=%s)", address, inbox_id)
            return inbox_id, address

    # Fallback deep scan
    inbox_id, address = _deep_scan(payload)
    if inbox_id and address:
        logger.info("Created inbox (fallback): %s  (id=%s)", address, inbox_id)
        return inbox_id, address

    logger.error("Could not parse inbox id/address from response: %s",
                 json.dumps(payload)[:400])
    return None, None


def _deep_scan(obj, depth=0) -> tuple[str | None, str | None]:
    """Walk JSON tree looking for a UUID-like id and an email address."""
    _EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    if depth > 12 or obj is None:
        return None, None
    found_id, found_addr = None, None
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = k.lower().replace("-", "_")
            if kl in ("id", "email_id", "mailbox_id", "_id"):
                if isinstance(v, str) and v.strip():
                    found_id = v.strip()
            if kl in ("email", "address", "email_address"):
                if isinstance(v, str) and "@" in v:
                    found_addr = v.strip()
            cid, cad = _deep_scan(v, depth + 1)
            if cid:
                found_id = cid
            if cad:
                found_addr = cad
    elif isinstance(obj, list):
        for item in obj:
            cid, cad = _deep_scan(item, depth + 1)
            if cid:
                found_id = cid
            if cad:
                found_addr = cad
    elif isinstance(obj, str):
        m = _EMAIL_RE.search(obj)
        if m:
            found_addr = m.group(0)
    return found_id, found_addr


def boomlify_list_messages(api_key: str, inbox_id: str) -> list[dict]:
    """Return the list of messages for a given inbox."""
    eid = urllib.parse.quote(str(inbox_id), safe="")
    url = f"{BOOMLIFY_BASE_URL}/api/v1/emails/{eid}/messages"
    req = urllib.request.Request(url, headers=_boomlify_headers(api_key))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode(errors="replace"))
    except Exception as exc:
        logger.debug("Boomlify list messages error: %s", exc)
        return []

    msgs = data.get("messages")
    if msgs is None and isinstance(data.get("data"), list):
        msgs = data.get("data")
    return msgs if isinstance(msgs, list) else []


def _extract_text(msg: dict) -> str:
    """Pull all text/html body fields out of a message dict."""
    parts: list[str] = []
    content = msg.get("content")
    if isinstance(content, dict):
        for key in ("html", "text", "plain", "body"):
            v = content.get(key)
            if isinstance(v, str) and v.strip():
                parts.append(v)
    for key in ("html", "html_body", "body_html", "body",
                "text", "text_body", "body_text", "content",
                "snippet", "preview"):
        v = msg.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(v)
    return "\n".join(parts)


def _is_tensorpix_sender(msg: dict) -> bool:
    sender = str(
        msg.get("from_email")
        or msg.get("sender_email")
        or msg.get("from")
        or msg.get("sender")
        or ""
    ).lower()
    return TENSORPIX_SENDER_DOMAIN in sender


def extract_verify_link(body: str) -> str | None:
    if not body:
        return None
    for pat in VERIFY_LINK_PATTERNS:
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            link = m.group(1) if "href" in pat else m.group(0)
            link = link.strip().rstrip("\"')>")
            return link
    return None


def poll_for_verification_link(
    api_key: str,
    inbox_id: str,
    timeout: int = EMAIL_POLL_TIMEOUT_S,
    interval: int = EMAIL_POLL_INTERVAL_S,
) -> str | None:
    """Poll Boomlify until a TensorPix verification email arrives.

    Returns the verification URL or None on timeout.
    """
    logger.info("Polling for verification email (inbox=%s, timeout=%ds) ...",
                inbox_id, timeout)
    start = time.time()
    poll_n = 0
    consecutive_404 = 0

    while time.time() - start < timeout:
        try:
            messages = boomlify_list_messages(api_key, inbox_id)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                consecutive_404 += 1
                logger.warning("Inbox 404 (consecutive=%d)", consecutive_404)
                if consecutive_404 >= 3:
                    logger.error("Inbox appears expired/deleted")
                    return None
            time.sleep(interval)
            continue
        except Exception as exc:
            logger.debug("Poll error: %s", exc)
            time.sleep(interval)
            continue

        consecutive_404 = 0
        poll_n += 1

        for msg in reversed(messages):
            if not _is_tensorpix_sender(msg):
                continue
            body = _extract_text(msg)
            link = extract_verify_link(body)
            if link:
                logger.info("Verification link found (poll #%d)", poll_n)
                return link
            logger.debug("TensorPix msg received but no verify link: %s",
                         body[:200])

        elapsed = int(time.time() - start)
        if poll_n % 3 == 0:
            logger.info("No link yet — %ds elapsed, poll #%d", elapsed, poll_n)
        time.sleep(interval)

    logger.error("Verification email not received within %ds", timeout)
    return None


# ---------------------------------------------------------------------------
# Credential persistence
# ---------------------------------------------------------------------------

def save_credentials(email: str, password: str, path: str) -> None:
    line = f"{email}  |  {password}  |  {datetime.utcnow().isoformat()}Z\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


# ---------------------------------------------------------------------------
# Playwright / CroxyProxy helpers
# ---------------------------------------------------------------------------

async def croxyproxy_navigate(page, target_url: str) -> bool:
    """Navigate to a target URL through CroxyProxy.

    Returns True if the proxied page appears to have loaded.
    """
    logger.info("CroxyProxy: navigating to %s", CROXYPROXY_URL)
    try:
        await page.goto(CROXYPROXY_URL, wait_until="load",
                        timeout=PAGE_NAV_TIMEOUT_MS)
    except Exception as exc:
        logger.warning("CroxyProxy home page load issue: %s", exc)
        # Even if wait_until fails the page might be usable — continue

    await page.wait_for_timeout(2_000)

    # Locate the URL input bar - CroxyProxy uses input[name="url"]
    url_input = None
    for selector in ('input[name="url"]', 'input[placeholder*="URL"]', 'input[placeholder*="search query"]'):
        locs = page.locator(selector)
        count = await locs.count()
        for idx in range(count):
            loc = locs.nth(idx)
            try:
                await loc.wait_for(state="visible", timeout=3_000)
                disabled = await loc.is_disabled()
                if not disabled:
                    url_input = loc
                    break
            except Exception:
                continue
        if url_input:
            break

    if url_input is None:
        logger.error("CroxyProxy: could not find URL input field")
        try:
            await page.screenshot(path="/tmp/croxyproxy_no_input.png")
        except Exception:
            pass
        return False

    logger.info("CroxyProxy: typing target URL into input bar")
    await url_input.click()
    await url_input.fill("")
    await url_input.type(target_url, delay=10)
    await page.keyboard.press("Enter")

    # Wait for the proxied page to load inside CroxyProxy's frame
    logger.info("CroxyProxy: waiting %ds for proxied page to load ...",
                CROXYPROXY_LOAD_WAIT_S)
    await page.wait_for_timeout(CROXYPROXY_LOAD_WAIT_S * 1_000)

    # Verify something loaded (check we're no longer on the CroxyProxy home)
    page_text = ""
    try:
        page_text = await page.text_content("body") or ""
    except Exception:
        pass
    if "croxyproxy" in page_text.lower()[:3000] and "enter url" in page_text.lower():
        logger.warning("CroxyProxy: still showing home page after wait")
        await page.wait_for_timeout(5_000)

    logger.info("CroxyProxy: page ready (current URL fragment: %s)",
                page.url[:120])
    return True


async def fill_and_submit_register(page, email: str, password: str) -> bool:
    """Fill the TensorPix register form and click Create account."""
    try:
        # Email field
        email_field = page.locator('input[type="email"], input[name="email"], input[placeholder*="email"]').first
        await email_field.wait_for(state="visible", timeout=15_000)
        await email_field.click()
        await email_field.fill("")
        await email_field.type(email, delay=15)
        logger.info("Register: entered email")

        # Password field
        password_field = page.locator('input[type="password"], input[name="password"]').first
        await password_field.wait_for(state="visible", timeout=10_000)
        await password_field.click()
        await password_field.fill("")
        await password_field.type(password, delay=10)
        logger.info("Register: entered password")

        await page.wait_for_timeout(500)

        # Click "Create account" button
        create_btn = page.locator('button:has-text("Create account")')
        await create_btn.wait_for(state="attached", timeout=10_000)
        # May be disabled until both fields are filled; give it a moment
        await page.wait_for_timeout(500)
        await create_btn.click()
        logger.info("Register: clicked 'Create account'")

        # Wait for navigation / response
        await page.wait_for_timeout(6_000)
        return True

    except Exception as exc:
        logger.error("Register form error: %s", exc)
        try:
            await page.screenshot(path="/tmp/register_error.png")
        except Exception:
            pass
        return False


async def fill_and_submit_login(page, email: str, password: str) -> bool:
    """Fill the TensorPix login form and click Sign in."""
    try:
        email_field = page.locator('input[type="email"], input[name="email"], input[placeholder*="email"]').first
        await email_field.wait_for(state="visible", timeout=15_000)
        await email_field.click()
        await email_field.fill("")
        await email_field.type(email, delay=15)
        logger.info("Login: entered email")

        password_field = page.locator('input[type="password"], input[name="password"]').first
        await password_field.wait_for(state="visible", timeout=10_000)
        await password_field.click()
        await password_field.fill("")
        await password_field.type(password, delay=10)
        logger.info("Login: entered password")

        await page.wait_for_timeout(500)

        sign_in_btn = page.locator('button:has-text("Sign in")')
        await sign_in_btn.wait_for(state="attached", timeout=10_000)
        await page.wait_for_timeout(500)
        await sign_in_btn.click()
        logger.info("Login: clicked 'Sign in'")

        await page.wait_for_timeout(6_000)
        return True

    except Exception as exc:
        logger.error("Login form error: %s", exc)
        try:
            await page.screenshot(path="/tmp/login_error.png")
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# Core account creation cycle
# ---------------------------------------------------------------------------

async def create_one_account(
    browser,
    api_key: str,
    password: str,
    credentials_path: str,
    account_index: int,
) -> bool:
    """Full single-account lifecycle. Returns True on success."""

    # 1. Create Boomlify inbox
    logger.info("=" * 60)
    logger.info("ACCOUNT %d — creating temp email ...", account_index)
    inbox_id = None
    email_addr = None
    for attempt in range(1, 4):
        inbox_id, email_addr = boomlify_create_inbox(api_key)
        if inbox_id and email_addr:
            break
        logger.warning("Inbox creation attempt %d failed, retrying ...", attempt)
        time.sleep(2)

    if not inbox_id or not email_addr:
        logger.error("ACCOUNT %d — could not create temp email", account_index)
        return False

    page = None
    try:
        page = await browser.new_page()

        # 2. Navigate to register page through CroxyProxy
        if not await croxyproxy_navigate(page, REGISTER_URL):
            logger.error("ACCOUNT %d — CroxyProxy navigation failed for register page",
                         account_index)
            return False

        # 3. Fill and submit the registration form
        reg_ok = False
        for retry in range(1, REGISTER_MAX_RETRIES + 1):
            logger.info("Register attempt %d/%d", retry, REGISTER_MAX_RETRIES)
            reg_ok = await fill_and_submit_register(page, email_addr, password)
            if reg_ok:
                break
            logger.warning("Register attempt %d failed", retry)
            if retry < REGISTER_MAX_RETRIES:
                # Re-navigate through CroxyProxy
                await croxyproxy_navigate(page, REGISTER_URL)
                await page.wait_for_timeout(2_000)

        if not reg_ok:
            logger.error("ACCOUNT %d — registration form submission failed",
                         account_index)
            return False

        logger.info("ACCOUNT %d — registration submitted, waiting for email ...",
                     account_index)

        # 4. Poll for verification email
        verify_link = await asyncio.to_thread(
            poll_for_verification_link,
            api_key, inbox_id,
            EMAIL_POLL_TIMEOUT_S,
            EMAIL_POLL_INTERVAL_S,
        )
        if not verify_link:
            logger.error("ACCOUNT %d — no verification link received",
                         account_index)
            return False

        logger.info("ACCOUNT %d — verification link: %s",
                     account_index, verify_link[:100])

        # 5. Open verification link through CroxyProxy
        if not await croxyproxy_navigate(page, verify_link):
            logger.error("ACCOUNT %d — CroxyProxy navigation failed for verify link",
                         account_index)
            return False

        logger.info("ACCOUNT %d — verification page loaded, waiting ...",
                     account_index)
        await page.wait_for_timeout(5_000)

        # 6. Navigate back to CroxyProxy and go to login page
        if not await croxyproxy_navigate(page, LOGIN_URL):
            logger.error("ACCOUNT %d — CroxyProxy navigation failed for login page",
                         account_index)
            return False

        # 7. Fill and submit the login form
        login_ok = await fill_and_submit_login(page, email_addr, password)
        if not login_ok:
            logger.error("ACCOUNT %d — login failed", account_index)
            return False

        logger.info("ACCOUNT %d — logged in, waiting 5s ...", account_index)
        await page.wait_for_timeout(5_000)

        # 8. Save credentials
        save_credentials(email_addr, password, credentials_path)
        logger.info("ACCOUNT %d — DONE  email=%s  saved to %s",
                     account_index, email_addr, credentials_path)
        return True

    except Exception as exc:
        logger.error("ACCOUNT %d — unexpected error: %s", account_index, exc,
                     exc_info=True)
        try:
            if page:
                await page.screenshot(path=f"/tmp/account_{account_index}_crash.png")
        except Exception:
            pass
        return False
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def async_main(args: argparse.Namespace) -> None:
    global REGISTER_URL, LOGIN_URL

    # Override URLs if custom register URL provided
    if args.register_url != REGISTER_URL:
        custom_url = args.register_url
        REGISTER_URL = custom_url
        if "/register" in custom_url:
            LOGIN_URL = custom_url.replace("/register", "/login")
        else:
            # Fallback if the structure is unexpected
            LOGIN_URL = "https://app.tensorpix.ai/login"

    # File logger
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "tensorpix_bot.log")
    _add_file_handler(log_path)

    logger.info("TensorPix Account Maker Bot — CroxyProxy Edition")
    logger.info("Target accounts : %d", args.count)
    logger.info("Password        : %s", args.password)
    logger.info("Register URL    : %s", REGISTER_URL)
    logger.info("Login URL       : %s", LOGIN_URL)
    logger.info("Credentials file: %s", args.credentials_file)
    logger.info("Log file        : %s", log_path)

    if not args.api_key:
        logger.error("--api-key is required (or set BOOMLIFY_API_KEY env)")
        sys.exit(1)

    api_key: str = args.api_key
    password: str = args.password
    count: int = args.count
    creds_path: str = args.credentials_file

    # Ensure output directory exists
    os.makedirs(os.path.dirname(os.path.abspath(creds_path)), exist_ok=True)

    # Write header to credentials file if empty / new
    if not os.path.exists(creds_path) or os.path.getsize(creds_path) == 0:
        with open(creds_path, "w", encoding="utf-8") as f:
            f.write(f"# TensorPix Account Credentials — generated "
                    f"{datetime.utcnow().isoformat()}Z\n")
            f.write(f"# Format: email  |  password  |  timestamp\n")

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
            timeout=30_000,
        )

        successes = 0
        failures = 0

        for i in range(1, count + 1):
            logger.info("-" * 60)
            logger.info("Starting account %d of %d", i, count)
            ok = await create_one_account(
                browser=browser,
                api_key=api_key,
                password=password,
                credentials_path=creds_path,
                account_index=i,
            )
            if ok:
                successes += 1
            else:
                failures += 1

            # Brief pause between accounts (except after the last)
            if i < count:
                # If count is 1, no need to wait. 
                # If user specified a custom count (>1), wait 2 minutes (120s) as requested.
                pause = 120 if count > 1 else 2
                logger.info("Pausing %ds before next account ...", pause)
                await asyncio.sleep(pause)

        await browser.close()

    # Summary
    logger.info("=" * 60)
    logger.info("SUMMARY: %d created, %d failed (of %d requested)",
                successes, failures, count)
    logger.info("Credentials saved to: %s", creds_path)
    logger.info("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TensorPix Account Maker Bot — CroxyProxy Edition",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("BOOMLIFY_API_KEY", ""),
        help="Boomlify API key (or set BOOMLIFY_API_KEY env var)",
    )
    parser.add_argument(
        "--count", "-n",
        type=int,
        default=DEFAULT_COUNT,
        help=f"Number of accounts to create (default: {DEFAULT_COUNT})",
    )
    parser.add_argument(
        "--password", "-p",
        default=DEFAULT_PASSWORD,
        help=f"Password for all accounts (default: {DEFAULT_PASSWORD})",
    )
    parser.add_argument(
        "--credentials-file",
        default=CREDENTIALS_FILE,
        help="Path to save credentials (default: %s)" % CREDENTIALS_FILE,
    )
    parser.add_argument(
        "--register-url",
        default=REGISTER_URL,
        help="TensorPix register URL with referral (default: %s)" % REGISTER_URL,
    )
    args = parser.parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()