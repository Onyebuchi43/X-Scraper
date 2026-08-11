import asyncio
import json
import logging
import re
import secrets
import sqlite3
import subprocess
import time
from typing import Optional, Dict
import httpx
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

DASH_DB = "dashboard.db"

def create_temp_email() -> tuple[Optional[str], Optional[str], str]:
    """
    Creates a temporary email address.
    Returns (email_address, cred_dir_or_token, provider_type) where provider_type is 'atomicmail', 'guerrilla', or 'mailtm'.
    """
    # 1. Try Atomic Mail first (high reputation @atomicmail.ai)
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

    # 2. Try Guerrilla Mail fallback
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

    # 3. Fallback to mail.tm
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

def poll_twitter_code(email_token: str, provider_type: str, timeout_sec: int = 90) -> Optional[str]:
    start = time.time()
    if provider_type == "atomicmail":
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
            time.sleep(3)
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
                        mail_id = item.get("mail_id")
                        if mail_id:
                            f_res = httpx.get(f"https://api.guerrillamail.com/ajax.php?f=fetch_email&sid_token={email_token}&email_id={mail_id}", timeout=10)
                            if f_res.status_code == 200:
                                body = str(f_res.json().get("mail_body", ""))
                                codes = re.findall(r"\b\d{6}\b", body)
                                if codes:
                                    return codes[0]
            except Exception as exc:
                logger.warning("Error polling Guerrilla Mail inbox: %s", exc)
            time.sleep(3)
        return None
    else:
        headers = {"Authorization": f"Bearer {email_token}"}
        while time.time() - start < timeout_sec:
            try:
                r = httpx.get("https://api.mail.tm/messages", headers=headers, timeout=10)
                msgs = r.json().get("hydra:member", [])
                if msgs:
                    msg_id = msgs[0]["id"]
                    detail = httpx.get(f"https://api.mail.tm/messages/{msg_id}", headers=headers, timeout=10).json()
                    text = str(detail.get("text", "")) + str(detail.get("html", ""))
                    codes = re.findall(r"\b\d{6}\b", text)
                    if codes:
                        return codes[0]
            except Exception as exc:
                logger.warning("Error polling mail.tm inbox: %s", exc)
            time.sleep(3)
        return None

async def register_single_twitter_account_async(
    name: str,
    proxy_url: Optional[str] = None
) -> Optional[Dict[str, str]]:
    email, mail_token, provider_type = create_temp_email()
    if not email or not mail_token:
        logger.error("Failed to generate temp email for automated Twitter registration")
        return None

    logger.info("Starting automated Twitter email registration for %s (%s)...", name, email)

    pw_kwargs = {}
    if proxy_url:
        pw_kwargs["proxy"] = {"server": proxy_url}

    args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-accelerated-2d-canvas",
        "--disable-gpu",
    ]
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=args, **pw_kwargs)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = await context.new_page()

        async def js_click(locator):
            """Click via JS dispatchEvent to bypass overlay masks."""
            el = await locator.element_handle()
            if el:
                await page.evaluate("el => el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}))", el)

        async def dismiss_overlay():
            """Dismiss any modal overlay blocking pointer events."""
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

            # Dismiss any cookie consent or loading overlay before interacting
            await dismiss_overlay()

            btn_phone = page.locator('text="Continue with phone"')
            if await btn_phone.count() > 0:
                await dismiss_overlay()
                try:
                    await btn_phone.first.click(timeout=5000)
                except Exception:
                    logger.info("Pointer click blocked — using JS click on 'Continue with phone'")
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
            code = poll_twitter_code(mail_token, provider_type=provider_type, timeout_sec=90)
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
