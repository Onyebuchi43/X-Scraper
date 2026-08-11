import asyncio
import logging
import re
import secrets
import sqlite3
import time
from typing import Optional, Dict
import httpx
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

DASH_DB = "dashboard.db"

def create_temp_email() -> tuple[Optional[str], Optional[str]]:
    try:
        d_res = httpx.get("https://api.mail.tm/domains", timeout=10)
        domains = d_res.json().get("hydra:member", [])
        if not domains:
            return None, None
        domain = domains[0]["domain"]
        username = f"usr_{secrets.token_hex(4)}"
        email = f"{username}@{domain}"
        password = f"P@ss{secrets.token_hex(6)}"
        r = httpx.post("https://api.mail.tm/accounts", json={"address": email, "password": password}, timeout=10)
        if r.status_code not in (200, 201):
            return None, None
        t_res = httpx.post("https://api.mail.tm/token", json={"address": email, "password": password}, timeout=10)
        token = t_res.json().get("token")
        return email, token
    except Exception as exc:
        logger.error("create_temp_email failed: %s", exc)
        return None, None

def poll_twitter_code(jwt_token: str, timeout_sec: int = 90) -> Optional[str]:
    headers = {"Authorization": f"Bearer {jwt_token}"}
    start = time.time()
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
            logger.warning("Error checking email inbox: %s", exc)
        time.sleep(3)
    return None

async def register_single_twitter_account_async(
    name: str,
    proxy_url: Optional[str] = None
) -> Optional[Dict[str, str]]:
    email, mail_token = create_temp_email()
    if not email or not mail_token:
        logger.error("Failed to generate temp email for automated Twitter registration")
        return None

    logger.info("Starting automated Twitter email registration for %s (%s)...", name, email)

    pw_kwargs = {}
    if proxy_url:
        pw_kwargs["proxy"] = {"server": proxy_url}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, **pw_kwargs)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = await context.new_page()

        try:
            await page.goto("https://x.com/i/flow/signup", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(4)

            btn_phone = page.locator('text="Continue with phone"')
            if await btn_phone.count() > 0:
                await btn_phone.first.click()
                await asyncio.sleep(2)

            btn_email = page.locator('text="Use email instead"')
            if await btn_email.count() > 0:
                await btn_email.first.click()
                await asyncio.sleep(2)

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
                await btn_next.first.click()
                await asyncio.sleep(2)

            btn_next2 = page.locator('button:has-text("Next")')
            if await btn_next2.count() > 0:
                await btn_next2.first.click()
                await asyncio.sleep(2)

            btn_signup = page.locator('button:has-text("Sign up")')
            if await btn_signup.count() > 0:
                await btn_signup.first.click()
                await asyncio.sleep(3)

            logger.info("Waiting for Twitter 6-digit confirmation code on %s...", email)
            code = poll_twitter_code(mail_token, timeout_sec=90)
            if code:
                logger.info("Retrieved Twitter verification code: %s", code)
                code_inp = page.locator('input[name="verification_code"], input[autocomplete="one-time-code"]')
                if await code_inp.count() > 0:
                    await code_inp.fill(code)
                    await page.locator('button:has-text("Next")').first.click()
                    await asyncio.sleep(3)

                    pwd_inp = page.locator('input[name="password"]')
                    if await pwd_inp.count() > 0:
                        account_password = f"Pass{secrets.token_hex(6)}!"
                        await pwd_inp.fill(account_password)
                        await page.locator('button:has-text("Next")').first.click()
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
