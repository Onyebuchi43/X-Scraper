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
            "SELECT id, account_ids, auto_assign_new_accounts FROM campaigns WHERE status='running'"
        ).fetchall()

        for camp in campaigns:
            auto_assign = camp["auto_assign_new_accounts"] if "auto_assign_new_accounts" in camp.keys() else 1
            if auto_assign:
                raw_ids = camp["account_ids"] or "[]"
                try:
                    import json
                    ids = json.loads(raw_ids)
                except Exception:
                    ids = []
                if account_id not in ids:
                    ids.append(account_id)
                    c.execute(
                        "UPDATE campaigns SET account_ids=? WHERE id=?",
                        (json.dumps(ids), camp["id"]),
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

    uname = (username or name.lower().replace(" ", "")).strip().lstrip("@")
    px = (proxy or "").strip()

    c.execute(
        """
        INSERT INTO accounts (username, auth_token, ct0, proxy, created_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (uname, auth_token.strip(), ct0.strip(), px),
    )
    acc_id = c.lastrowid
    conn.commit()
    conn.close()

    logger.info("Saved new account @%s (ID %d) to database", uname, acc_id)

    # Trigger auto-assignment to running campaigns
    auto_assign_account_to_active_campaigns(acc_id)

    return {
        "success": True,
        "account_id": acc_id,
        "username": uname,
        "message": f"Account @{uname} successfully registered and added to database!",
    }
