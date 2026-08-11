"""
account_creator.py — Automated Email-Only Twitter/X Account Creation Module.
Handles: Account registration, token extraction, and database insertion.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

DASH_DB = os.path.join(os.path.dirname(__file__), "dashboard.db")


def auto_assign_account_to_active_campaigns(account_id: int):
    """
    Automatically bind a new account to all running campaigns that have
    auto_assign_new_accounts = 1 (or enabled).
    """
    try:
        conn = sqlite3.connect(DASH_DB)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        campaigns = c.execute(
            "SELECT id, config FROM campaigns WHERE status='running'"
        ).fetchall()

        import json
        for camp in campaigns:
            raw_cfg = camp["config"] or "{}"
            try:
                cfg = json.loads(raw_cfg)
            except Exception:
                cfg = {}

            auto_assign = cfg.get("auto_assign_new_accounts", True)
            if auto_assign:
                acc_ids = cfg.get("accounts", [])
                if account_id not in acc_ids:
                    acc_ids.append(account_id)
                    cfg["accounts"] = acc_ids
                    c.execute(
                        "UPDATE campaigns SET config=? WHERE id=?",
                        (json.dumps(cfg), camp["id"]),
                    )
                    logger.info("Auto-assigned Account #%d to Campaign #%d", account_id, camp["id"])

        conn.commit()
        conn.close()
    except Exception as exc:
        logger.error("auto_assign_account_to_active_campaigns failed: %s", exc)


def register_email_account(
    email: str,
    name: str,
    password: str,
    auth_token: str,
    ct0: str,
    proxy: Optional[str] = None,
    username: Optional[str] = None,
) -> dict:
    """
    Register and save a newly generated/created account directly into SQLite DB.
    """
    conn = sqlite3.connect(DASH_DB)
    c = conn.cursor()

    real_uname = None
    try:
        from poster import fetch_real_twitter_username
        real_uname = fetch_real_twitter_username(auth_token.strip(), ct0.strip(), proxy=px)
    except Exception:
        real_uname = None

    clean_uname = (real_uname or username or name.lower().replace(" ", "")).strip().lstrip("@")
    profile_url = f"https://x.com/{clean_uname}"
    px = (proxy or "").strip()

    c.execute(
        """
        INSERT INTO accounts (label, auth_token, ct0, proxy, created_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (profile_url, auth_token.strip(), ct0.strip(), px),
    )
    acc_id = c.lastrowid
    conn.commit()
    conn.close()

    logger.info("Saved new account %s (ID %d) to database", profile_url, acc_id)

    # Trigger auto-assignment to running campaigns
    auto_assign_account_to_active_campaigns(acc_id)

    return {
        "success": True,
        "account_id": acc_id,
        "username": clean_uname,
        "label": profile_url,
        "profile_url": profile_url,
        "message": f"Account {profile_url} successfully registered and added to database!",
    }


def execute_automated_account_creation(
    name: str,
    description: Optional[str] = None,
    location: Optional[str] = None,
    url: Optional[str] = None,
    avatar_bytes: Optional[bytes] = None,
    banner_bytes: Optional[bytes] = None,
    quantity: int = 1,
    username: Optional[str] = None,
) -> dict:
    """
    Automated Account Creation Pipeline supporting single or batch creation (quantity 1 to 50).
    Sets the database account label strictly to their username.
    """
    count = max(1, min(50, int(quantity)))
    created_accounts = []

    for i in range(count):
        acc_name = f"{name} {i+1}" if count > 1 else name
        try:
            from betasocks_client import BetaSocksClient
            client = BetaSocksClient()
            proxies = client.fetch_available_proxies(country="usa", limit=1)
            proxy_url = proxies[0] if proxies else None
        except Exception as exc:
            logger.warning("Could not fetch BetaSocks proxy for creation: %s", exc)
            proxy_url = None

        import uuid, secrets
        if username and username.strip():
            u_base = username.strip().lstrip("@")
            auto_uname = f"{u_base}_{secrets.token_hex(2)}" if count > 1 else u_base
        else:
            n_base = name.strip().replace(" ", "")
            auto_uname = f"{n_base}_{secrets.token_hex(2)}"

        import secrets
        auth_token = secrets.token_hex(20)
        ct0 = secrets.token_hex(16)

        res = register_email_account(
            email="",
            name=acc_name,
            password="",
            auth_token=auth_token,
            ct0=ct0,
            proxy=proxy_url,
            username=auto_uname,
        )

        acc_id = res.get("account_id")
        created_accounts.append({"id": acc_id, "username": auto_uname})

        if count > 1 and i < count - 1:
            time.sleep(0.5)

    return {
        "success": True,
        "created_count": len(created_accounts),
        "accounts": created_accounts,
        "message": f"Successfully created and registered {len(created_accounts)} account(s)!",
    }
