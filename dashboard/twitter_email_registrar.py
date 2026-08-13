import json
import os
import secrets
import logging
import sqlite3
import subprocess
import time
import re
from typing import Optional, Dict
import httpx
import asyncio
from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

DASH_DB = "dashboard.db"

def create_temp_email() -> tuple[Optional[str], Optional[str], str]:
    """
    Creates a temporary email address.
    Returns (email_address, token_or_session, provider_type) where provider_type is 'outlook', 'atomicmail', 'guerrilla', or 'mailtm'.
    """
    # 1. Try Outlook Plus-Addressing first (100% trusted by Twitter, delivers OTP instantly)
    if os.path.exists("outlook_session.json"):
        tag = secrets.token_hex(4)
        email = f"alexstrickland2026+{tag}@outlook.com"
        logger.info("Generated Outlook Plus-Addressing temp address: %s", email)
        return email, "outlook_session.json", "outlook"

    # 2. Try Atomic Mail fallback
    try:
        uname = f"usr{secrets.token_hex(4)}"
        cred_dir = f"/tmp/atomic_{uname}"
        cmd = f"/usr/bin/node /usr/lib/node_modules/@atomicmail/agent-skill/esm/skill/cli.js register --username '{uname}' --watch on-demand --forced --credentials-dir '{cred_dir}'"
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=35)
        if res.returncode == 0 and res.stdout:
            try:
                data = json.loads(res.stdout.strip())
                inbox = data.get("inbox") or data.get("accountId")
                if inbox:
                    email = f"{inbox}@atomicmail.ai"
                    logger.info("Generated Atomic Mail temp address: %s", email)
                    return email, cred_dir, "atomicmail"
            except Exception:
                pass
    except Exception as exc:
        logger.warning("Atomic Mail generation failed: %s — trying Guerrilla Mail fallback", exc)

    # 3. Try Guerrilla Mail fallback
    try:
        r = httpx.get("https://api.guerrillamail.com/ajax.php?f=get_email_address", timeout=10)
        if r.status_code == 200:
            data = r.json()
            email = data.get("email_addr")
            sid_token = data.get("sid_token")
            if email and sid_token:
                logger.info("Generated Guerrilla Mail temp address: %s", email)
                return email, sid_token, "guerrilla"
    except Exception as exc:
        logger.warning("Guerrilla Mail generation failed: %s — trying mail.tm fallback", exc)

    # 4. Fallback to mail.tm
    try:
        d_res = httpx.get("https://api.mail.tm/domains", timeout=10)
        domains = d_res.json().get("hydra:member", [])
        if domains:
            domain = domains[0]["domain"]
            username = f"usr_{secrets.token_hex(4)}"
            email = f"{username}@{domain}"
            password = f"P@ss{secrets.token_hex(6)}"
            r = httpx.post("https://api.mail.tm/accounts", json={"address": email, "password": password}, timeout=10)
            if r.status_code in (200, 201):
                t_res = httpx.post("https://api.mail.tm/token", json={"address": email, "password": password}, timeout=10)
                token = t_res.json().get("token")
                if token:
                    return email, token, "mailtm"
    except Exception as exc:
        logger.error("mail.tm fallback failed: %s", exc)

    return None, None, ""

async def poll_twitter_code_async(email_token: str, provider_type: str, timeout_sec: int = 90) -> Optional[str]:
    start = time.time()
    if provider_type == "outlook":
        session_path = email_token if (email_token and os.path.exists(email_token)) else "outlook_session.json"
        while time.time() - start < timeout_sec:
            try:
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True)
                    context = await browser.new_context(
                        storage_state=session_path,
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    )
                    page = await context.new_page()
                    await page.goto("https://outlook.live.com/mail/0/", wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(3)
                    content = await page.content()
                    await browser.close()

                    matches = re.findall(r"\b\d{6}\b", content)
                    for c in matches:
                        if c not in ("000000", "242424", "424242", "616161", "808080", "123456", "360679", "038111"):
                            logger.info("Successfully fetched Twitter OTP code from Outlook: %s", c)
                            return c
            except Exception as exc:
                logger.warning("Error polling Outlook inbox via Playwright Async: %s", exc)
            await asyncio.sleep(5)
        return None
    elif provider_type == "atomicmail":
        cmd = f"/usr/bin/node /usr/lib/node_modules/@atomicmail/agent-skill/esm/skill/cli.js jmap_request --ops-file list_inbox.json --credentials-dir '{email_token}'"
        while time.time() - start < timeout_sec:
            try:
                res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
                if res.returncode == 0 and res.stdout:
                    text = res.stdout
                    matches = re.findall(r"\b\d{6}\b", text)
                    if matches:
                        return matches[0]
            except Exception as exc:
                logger.warning("Error polling Atomic Mail inbox: %s", exc)
            await asyncio.sleep(6)
        return None
    elif provider_type == "guerrilla":
        url = f"https://api.guerrillamail.com/ajax.php?f=get_email_list&sid_token={email_token}&offset=0"
        while time.time() - start < timeout_sec:
            try:
                r = httpx.get(url, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    mail_list = data.get("list", [])
                    for item in mail_list:
                        subject = str(item.get("mail_subject", ""))
                        excerpt = str(item.get("mail_excerpt", ""))
                        text = f"{subject} {excerpt}"
                        codes = re.findall(r"\b\d{6}\b", text)
                        if codes:
                            return codes[0]
            except Exception as exc:
                logger.warning("Error polling Guerrilla Mail inbox: %s", exc)
            await asyncio.sleep(5)
        return None
    return None

def poll_twitter_code(email_token: str, provider_type: str, timeout_sec: int = 90) -> Optional[str]:
    return asyncio.run(poll_twitter_code_async(email_token, provider_type, timeout_sec))

def save_account_to_db(auth_token: str, ct0: str, proxy_url: Optional[str] = None, email: Optional[str] = None) -> bool:
    try:
        from poster import fetch_real_twitter_username
        real_username = fetch_real_twitter_username(auth_token, ct0, proxy=proxy_url)
        username = f"@{real_username}" if real_username else f"@usr_{secrets.token_hex(4)}"

        conn = sqlite3.connect(DASH_DB)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                auth_token TEXT,
                ct0 TEXT,
                proxy TEXT,
                status TEXT DEFAULT 'active',
                email TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        parts = proxy_url.split(":") if proxy_url else []
        db_proxy = f"{parts[0]}:{parts[1]}:{parts[2]}:{parts[3]}" if len(parts) == 4 else (proxy_url or "")

        c.execute("""
            INSERT OR REPLACE INTO accounts (username, auth_token, ct0, proxy, status, email)
            VALUES (?, ?, ?, ?, 'active', ?)
        """, (username, auth_token, ct0, db_proxy, email or ""))

        conn.commit()
        conn.close()
        logger.info("Saved new Twitter account to DB: %s", username)
        return True
    except Exception as exc:
        logger.error("Failed to save account to DB: %s", exc)
        return False

async def register_single_twitter_account_async(name: str, proxy_url: Optional[str] = None) -> Optional[Dict[str, str]]:
    email, mail_token, provider_type = create_temp_email()
    if not email:
        logger.error("Failed to generate temp email for registration.")
        return None

    logger.info("Starting automated Playwright Twitter signup for %s with email %s...", name, email)

    pw_kwargs = {}
    if proxy_url:
        clean_px = proxy_url.replace("socks5://", "").replace("http://", "")
        parts = clean_px.split(":")
        if len(parts) == 4:
            pw_kwargs["proxy"] = {"server": f"http://{parts[0]}:{parts[1]}"}
        elif len(parts) == 2:
            pw_kwargs["proxy"] = {"server": f"http://{parts[0]}:{parts[1]}"}
        else:
            pw_kwargs["proxy"] = {"server": proxy_url}

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True, **pw_kwargs)
        except Exception as p_err:
            logger.warning("Playwright proxy launch failed (%s) — falling back to direct connection", p_err)
            browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="en-US"
        )
        page = await context.new_page()

        async def js_click(locator):
            el = await locator.element_handle()
            if el:
                await page.evaluate("el => el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}))", el)

        async def dismiss_overlay():
            mask = page.locator('[data-testid="mask"]')
            if await mask.count() > 0:
                logger.info("Overlay mask detected — dismissing with Escape key")
                await page.keyboard.press("Escape")
                await asyncio.sleep(1)
                try:
                    await mask.first.click(timeout=2000)
                except Exception:
                    pass
                await asyncio.sleep(1)

        try:
            await page.goto("https://x.com/i/flow/signup", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(4)

            await dismiss_overlay()

            btn_phone = page.locator('text="Continue with phone"')
            if await btn_phone.count() > 0:
                await dismiss_overlay()
                try:
                    await btn_phone.first.click(timeout=5000)
                except Exception:
                    await js_click(btn_phone.first)
                await asyncio.sleep(2)

            btn_email = page.locator('text="Use email instead"')
            if await btn_email.count() > 0:
                await dismiss_overlay()
                try:
                    await btn_email.first.click(timeout=5000)
                except Exception:
                    await js_click(btn_email.first)
                await asyncio.sleep(2)

            await dismiss_overlay()

            name_inp = page.locator('input[name="name"]')
            if await name_inp.count() > 0:
                await name_inp.fill(name)

            email_inp = page.locator('input[name="email"]')
            if await email_inp.count() > 0:
                await email_inp.fill(email)

            selects = page.locator('select')
            if await selects.count() >= 3:
                await selects.nth(0).select_option(value="5")
                await selects.nth(1).select_option(value="15")
                await selects.nth(2).select_option(value="1998")

            await asyncio.sleep(1)
            btn_next = page.locator('button:has-text("Next")')
            if await btn_next.count() > 0:
                try:
                    await btn_next.first.click(timeout=5000)
                except Exception:
                    await js_click(btn_next.first)
                await asyncio.sleep(2)

            btn_next2 = page.locator('button:has-text("Next")')
            if await btn_next2.count() > 0:
                try:
                    await btn_next2.first.click(timeout=5000)
                except Exception:
                    await js_click(btn_next2.first)
                await asyncio.sleep(2)

            btn_signup = page.locator('button:has-text("Sign up")')
            if await btn_signup.count() > 0:
                try:
                    await btn_signup.first.click(timeout=5000)
                except Exception:
                    await js_click(btn_signup.first)
                await asyncio.sleep(3)

            logger.info("Waiting for Twitter 6-digit confirmation code on %s...", email)
            code = await poll_twitter_code_async(mail_token, provider_type=provider_type, timeout_sec=90)
            if code:
                logger.info("Retrieved Twitter verification code: %s", code)
                code_inp = page.locator('input[name="verification_code"], input[autocomplete="one-time-code"]')
                if await code_inp.count() > 0:
                    await code_inp.fill(code)
                    try:
                        await page.locator('button:has-text("Next")').first.click(timeout=5000)
                    except Exception:
                        await js_click(page.locator('button:has-text("Next")').first)
                    await asyncio.sleep(3)

                    pwd_inp = page.locator('input[name="password"]')
                    if await pwd_inp.count() > 0:
                        account_password = f"Pass{secrets.token_hex(6)}!"
                        await pwd_inp.fill(account_password)
                        try:
                            await page.locator('button:has-text("Next")').first.click(timeout=5000)
                        except Exception:
                            await js_click(page.locator('button:has-text("Next")').first)
                        await asyncio.sleep(5)

                    cookies = await context.cookies()
                    auth_token = next((c['value'] for c in cookies if c['name'] == 'auth_token'), None)
                    ct0 = next((c['value'] for c in cookies if c['name'] == 'ct0'), None)

                    if auth_token and ct0:
                        logger.info("Successfully extracted live Twitter cookies: auth_token=%s...", auth_token[:8])
                        await browser.close()
                        return {"auth_token": auth_token, "ct0": ct0, "email": email}

        except Exception as exc:
            logger.error("Automated Playwright registration error: %s", exc)

        await browser.close()
        return None

def register_live_twitter_account(name: str, proxy_url: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Synchronous wrapper for Playwright automated email registration."""
    try:
        return asyncio.run(register_single_twitter_account_async(name, proxy_url=proxy_url))
    except Exception as exc:
        logger.error("register_live_twitter_account error: %s", exc)
        return None
