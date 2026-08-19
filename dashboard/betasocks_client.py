"""
betasocks_client.py — BetaSocks.com automated proxy retrieval & daily limit engine.
Handles: login, session persistence, SOCKS5 proxy extraction, and daily quota limits.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

DASH_DB = os.path.join(os.path.dirname(__file__), "dashboard.db")

# ── Database Proxy Settings Helper ────────────────────────────────────────────
def init_proxy_db():
    conn = sqlite3.connect(DASH_DB)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS proxy_settings (
            id INTEGER PRIMARY KEY DEFAULT 1,
            betasocks_email TEXT DEFAULT 'mentlinda38@gmail.com',
            betasocks_password TEXT DEFAULT 'Meandyou2580',
            daily_limit INTEGER DEFAULT 50,
            fetched_today_count INTEGER DEFAULT 0,
            last_reset_date TEXT DEFAULT ''
        )
    """)
    # Insert default row if empty
    c.execute("SELECT COUNT(*) FROM proxy_settings")
    if c.fetchone()[0] == 0:
        c.execute("""
            INSERT INTO proxy_settings (id, betasocks_email, betasocks_password, daily_limit, fetched_today_count, last_reset_date)
            VALUES (1, 'mentlinda38@gmail.com', 'Meandyou2580', 50, 0, DATE('now'))
        """)
    conn.commit()
    conn.close()

init_proxy_db()


import threading

_FETCH_LOCK = threading.Lock()

def get_proxy_settings() -> dict:
    conn = sqlite3.connect(DASH_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM proxy_settings WHERE id=1").fetchone()
    conn.close()
    today_str = time.strftime("%Y-%m-%d")
    if row:
        d = dict(row)
        if d.get("last_reset_date") != today_str:
            d["fetched_today_count"] = 0
            d["last_reset_date"] = today_str
        return d
    return {
        "betasocks_email": "mentlinda38@gmail.com",
        "betasocks_password": "Meandyou2580",
        "daily_limit": 50,
        "fetched_today_count": 0,
        "last_reset_date": today_str,
    }


def update_proxy_settings(email: str, password: str, daily_limit: int) -> bool:
    conn = sqlite3.connect(DASH_DB)
    # If the user sets/adjusts daily limit, ensure fetched_today_count does not exceed new limit
    today_str = time.strftime("%Y-%m-%d")
    conn.execute(
        """
        UPDATE proxy_settings
        SET betasocks_email=?, betasocks_password=?, daily_limit=?, last_reset_date=?
        WHERE id=1
        """,
        (email.strip(), password.strip(), max(1, daily_limit), today_str),
    )
    conn.commit()
    conn.close()
    return True


def reset_daily_fetch_count() -> bool:
    conn = sqlite3.connect(DASH_DB)
    today_str = time.strftime("%Y-%m-%d")
    conn.execute("UPDATE proxy_settings SET fetched_today_count=0, last_reset_date=? WHERE id=1", (today_str,))
    conn.commit()
    conn.close()
    return True


def increment_daily_fetch_count(count: int = 1) -> int:
    today_str = time.strftime("%Y-%m-%d")
    conn = sqlite3.connect(DASH_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM proxy_settings WHERE id=1").fetchone()

    fetched = 0
    if row:
        last_date = row["last_reset_date"]
        current = row["fetched_today_count"] if last_date == today_str else 0
        fetched = current + count
        conn.execute(
            """
            UPDATE proxy_settings
            SET fetched_today_count=?, last_reset_date=?
            WHERE id=1
            """,
            (fetched, today_str),
        )
        conn.commit()
    conn.close()
    return fetched


# ── BetaSocks Web Authenticator ───────────────────────────────────────────────
class BetaSocksClient:
    def __init__(self, email: Optional[str] = None, password: Optional[str] = None):
        cfg = get_proxy_settings()
        self.email = (email or cfg["betasocks_email"]).strip()
        self.password = (password or cfg["betasocks_password"]).strip()
        self.client = httpx.Client(
            timeout=15,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Referer": "https://betasocks.com/login",
            },
        )

    def login(self) -> bool:
        try:
            self.client.get("https://betasocks.com/login")
            resp = self.client.post(
                "https://betasocks.com/check_user_login",
                data={"user_email": self.email, "user_password": self.password},
            )
            cookies_dict = dict(self.client.cookies)
            if "user_name" in str(cookies_dict) or "user.php" in str(resp.url).lower():
                logger.info("Successfully authenticated with BetaSocks as %s", self.email)
                return True
            logger.warning("BetaSocks login failed for %s", self.email)
            return False
        except Exception as exc:
            logger.error("BetaSocks login exception: %s", exc)
            return False

    def fetch_available_proxies(self, country: str = "usa", limit: int = 10) -> List[str]:
        with _FETCH_LOCK:
            cfg = get_proxy_settings()
            daily_limit = int(cfg.get("daily_limit", 50))
            fetched_today = int(cfg.get("fetched_today_count", 0))
            allowed = max(0, daily_limit - fetched_today)
            if allowed <= 0:
                logger.warning("Strict daily BetaSocks limit reached (%d/%d). Blocking proxy fetch.", fetched_today, daily_limit)
                return []

            effective_limit = min(max(1, limit), allowed)

            if not self.login():
                return []

            endpoint_map = {
                "usa": "view_usa_socks",
                "canada": "view_canada_socks",
                "uk": "view_gb_socks",
                "au": "view_au_socks",
                "all": "view_socks",
            }
            ep = endpoint_map.get(country.lower(), "view_socks")

            try:
                resp = self.client.get(f"https://betasocks.com/user/{ep}")
                if resp.status_code != 200:
                    return []

                raw_text = resp.text
                sock_ids = list(dict.fromkeys(re.findall(r'onclick="socks\((\d+)\)"', raw_text)))

                formatted_proxies = []

                for sid in sock_ids:
                    if len(formatted_proxies) >= effective_limit:
                        break
                    check_resp = self.client.get(f"https://betasocks.com/user/check_ip/{sid}")
                    if "Package expired" in check_resp.text or "buy a package" in check_resp.text:
                        logger.warning("BetaSocks Package Expired: Please renew your subscription on BetaSocks.com")
                        break

                    ip_text = check_resp.text.strip()
                    matches = re.findall(
                        r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{2,5}(?::[^\s<'\"]+:[^\s<'\"]+)?\b",
                        ip_text,
                    )
                    for p in matches:
                        parts = p.split(":")
                        if len(parts) == 4:
                            proxy_url = f"socks5://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
                        else:
                            proxy_url = f"socks5://{p}"
                        if proxy_url not in formatted_proxies:
                            formatted_proxies.append(proxy_url)
                            break

                if not formatted_proxies and not sock_ids:
                    proxy_matches = re.findall(
                        r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{2,5}(?::[^\s<'\"]+:[^\s<'\"]+)?\b",
                        raw_text,
                    )
                    for p in proxy_matches:
                        parts = p.split(":")
                        if len(parts) == 4:
                            proxy_url = f"socks5://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
                        else:
                            proxy_url = f"socks5://{p}"
                        if proxy_url not in formatted_proxies:
                            formatted_proxies.append(proxy_url)
                        if len(formatted_proxies) >= effective_limit:
                            break

                formatted_proxies = formatted_proxies[:effective_limit]

                if formatted_proxies:
                    increment_daily_fetch_count(len(formatted_proxies))
                    logger.info("Retrieved %d proxies from BetaSocks (%s) [Today: %d/%d]", len(formatted_proxies), country, fetched_today + len(formatted_proxies), daily_limit)

                return formatted_proxies
            except Exception as exc:
                logger.error("Error fetching BetaSocks proxies: %s", exc)
                return []

    def get_first_working_socks(self, country: str = "all") -> Optional[str]:
        """
        Retrieves exactly 1 proxy from BetaSocks for single account assignment.
        Does NOT pre-test multiple proxies, preserving daily limit.
        """
        proxies = self.fetch_available_proxies(country=country, limit=1)
        if proxies:
            return proxies[0]
        return None


def test_betasocks_credentials(email: str, password: str) -> dict:
    client = BetaSocksClient(email, password)
    ok = client.login()
    if ok:
        return {"success": True, "message": f"Successfully connected to BetaSocks account ({email})"}
    return {"success": False, "message": "Failed to log in to BetaSocks. Please check email and password."}
