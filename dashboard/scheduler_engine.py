"""
scheduler_engine.py — Background campaign scheduler.
Runs posting campaigns in a background thread, rotating accounts,
enforcing safe inter-post delays, and logging all activity.

Followers are scraped live from source_profiles (specified per campaign).
Every tagged username is written to the campaign_tagged table so no
user is ever tagged twice for the same campaign, even across restarts.
"""
from __future__ import annotations

import httpx
import json
import logging
import os
import random
import sqlite3
import sys
import subprocess
import threading
import time
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Safe imports for poster and image_editor modules
try:
    import poster
    import image_editor
    from poster import fetch_account_based_in, classify_account_error
except ImportError:
    try:
        from . import poster, image_editor
        from .poster import fetch_account_based_in, classify_account_error
    except ImportError:
        import dashboard.poster as poster
        import dashboard.image_editor as image_editor
        from dashboard.poster import fetch_account_based_in, classify_account_error

DASH_DB = os.path.join(os.path.dirname(__file__), "dashboard.db")

# Ensure Scweet package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Active campaign registry ───────────────────────────────────────────────────
_campaigns: dict[int, "_Campaign"] = {}
_GLOBAL_ACCOUNT_COOLDOWNS: dict[int, float] = {}
_lock = threading.RLock()


def set_account_cooldown(account_id: int, duration_seconds: float) -> None:
    with _lock:
        if account_id:
            _GLOBAL_ACCOUNT_COOLDOWNS[account_id] = time.time() + duration_seconds


def get_account_cooldown(account_id: int) -> float:
    with _lock:
        return _GLOBAL_ACCOUNT_COOLDOWNS.get(account_id, 0.0)


def is_account_cooling(account_id: int) -> bool:
    with _lock:
        until = _GLOBAL_ACCOUNT_COOLDOWNS.get(account_id, 0.0)
        return until > time.time()


def get_campaign(campaign_id: int) -> Optional["_Campaign"]:
    with _lock:
        return _campaigns.get(campaign_id)


def stop_campaign(campaign_id: int) -> bool:
    _set_status(campaign_id, "stopped")
    with _lock:
        c = _campaigns.get(campaign_id)
        if c:
            c.stop()
            _campaigns.pop(campaign_id, None)
            return True
    return False


def stop_all_campaigns() -> None:
    with _lock:
        for cid, c in list(_campaigns.items()):
            try:
                c.stop()
            except Exception:
                pass
        _campaigns.clear()


# ── DB helpers ─────────────────────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DASH_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _log_to_db(campaign_id: int, entry: dict) -> None:
    try:
        conn = _db()
        row = conn.execute(
            "SELECT log FROM campaigns WHERE id=?", (campaign_id,)
        ).fetchone()
        if row:
            log = json.loads(row[0] or "[]")
            log.append(entry)
            conn.execute(
                "UPDATE campaigns SET log=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(log[-2000:]), campaign_id),  # keep last 2000 entries
            )
            conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("_log_to_db failed: %s", exc)


def _set_status(campaign_id: int, status: str) -> None:
    try:
        conn = _db()
        conn.execute(
            "UPDATE campaigns SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, campaign_id),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("_set_status failed: %s", exc)


def _save_list_to_db(account_id: int, list_id: str, list_url: str, list_name: str) -> None:
    try:
        conn = _db()
        conn.execute(
            "INSERT INTO lists (account_id, list_id, list_url, list_name) VALUES (?,?,?,?)",
            (account_id, list_id, list_url, list_name),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("_save_list_to_db failed: %s", exc)


# ── Deduplication helpers ──────────────────────────────────────────────────────
def _load_already_tagged(campaign_id: int) -> set:
    """Return the set of usernames already tagged for this campaign."""
    try:
        conn = _db()
        rows = conn.execute(
            "SELECT username FROM campaign_tagged WHERE campaign_id=?", (campaign_id,)
        ).fetchall()
        conn.close()
        return {r["username"].lower() for r in rows}
    except Exception as exc:
        logger.warning("_load_already_tagged failed: %s", exc)
        return set()


def _mark_tagged(campaign_id: int, usernames: List[str]) -> None:
    """Persist a batch of usernames as tagged for this campaign."""
    if not usernames:
        return
    try:
        conn = _db()
        conn.executemany(
            "INSERT OR IGNORE INTO campaign_tagged (campaign_id, username) VALUES (?,?)",
            [(campaign_id, u.lower()) for u in usernames],
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("_mark_tagged failed: %s", exc)


def _delete_account_from_db(account_id: int) -> None:
    try:
        conn = _db()
        conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        conn.commit()
        conn.close()
        logger.info("Automatically deleted blocked account %d from database", account_id)
    except Exception as exc:
        logger.error("Failed to delete account %d from DB: %s", account_id, exc)


# ── Live follower scraper ──────────────────────────────────────────────────────
def _scrape_followers(
    source_profiles: List[str],
    accounts: List[dict],
    limit: int,
    log_fn,
    min_followers: int = 0,
    max_followers: int = 1000,
    country_filter: str = "",
    scrape_round: int = 1,
    checked_handles_set: Optional[set] = None,
) -> Tuple[List[str], bool, int]:
    """
    Scrape follower handles from `source_profiles`.
    Returns (handles_list, success_bool, total_scraped_raw_count).
    """
    # Always include ALL registered active DB accounts in the scraping pool for maximum rotation
    try:
        conn = _db()
        all_db_accs = conn.execute("SELECT id, auth_token, ct0, proxy FROM accounts").fetchall()
        conn.close()
        pool_accounts = [dict(a) for a in all_db_accs] if all_db_accs else accounts
    except Exception:
        pool_accounts = accounts if accounts else []

    if not pool_accounts:
        log_fn("ERROR", "No accounts available for scraping.")
        return [], False, 0

    if checked_handles_set is None:
        checked_handles_set = set()

    try:
        from Scweet import Scweet, ScweetConfig  # type: ignore

        cookies_pool_list = []
        for acc in pool_accounts:
            entry = {"auth_token": acc["auth_token"], "ct0": acc["ct0"]}
            if acc.get("proxy"): entry["proxy"] = acc["proxy"]
            cookies_pool_list.append(entry)

        start_idx = (scrape_round - 1) % len(cookies_pool_list)
        rotated_cookies = cookies_pool_list[start_idx:] + cookies_pool_list[:start_idx]

        country_keywords = [
            alias.strip().lower()
            for alias in country_filter.split(",")
            if alias.strip()
        ] if country_filter else []

        fetch_limit = max(limit, 100) if (max_followers and max_followers < 1000000) else limit
        country_msg = f", 'Account based in' filter: {country_filter}" if country_filter else ""
        log_fn("INFO", f"Initialising streaming Scweet scraper for source profiles: {source_profiles} (fetch limit: {fetch_limit}, followers range: {min_followers}-{max_followers}{country_msg})")
        cfg = ScweetConfig(daily_requests_limit=100000, daily_tweets_limit=100000)
        s = Scweet(
            cookies=rotated_cookies if len(rotated_cookies) > 1 else rotated_cookies[0],
            config=cfg,
        )

        import time as _time

        handles: List[str] = []
        # resume=True to paginate deeper across rounds
        results = s.get_followers(source_profiles, limit=fetch_limit, save=False, resume=True)
        raw_count = len(results) if results else 0

        # ── Step 1: follower count filter (fast, no extra API calls) ─────────
        candidate_items: List[dict] = []
        if results:
            for item in results:
                if isinstance(item, dict):
                    handle = (
                        item.get("username")
                        or item.get("screen_name")
                        or item.get("handle")
                        or ""
                    ).strip().lstrip("@").lower()
                    loc_str = str(item.get("location") or "").strip().lower()
                    fc = item.get("followers_count") or item.get("followers") or item.get("followers_cnt")
                    if fc is not None and max_followers and max_followers > 0:
                        try:
                            val = int(fc)
                            if not (min_followers <= val <= max_followers):
                                continue
                        except (ValueError, TypeError):
                            pass
                    if handle:
                        candidate_items.append({"handle": handle, "bio_location": loc_str})
                elif isinstance(item, str):
                    handle = item.strip().lstrip("@").lower()
                    if handle:
                        candidate_items.append({"handle": handle, "bio_location": ""})

        # ── Step 2: "Account based in" country filter (AboutAccountQuery + Bio Location fallback) ────
        if country_keywords:
            unprocessed_candidates = [c for c in candidate_items if c["handle"] not in checked_handles_set]
            if unprocessed_candidates:
                from concurrent.futures import ThreadPoolExecutor, as_completed

                scrape_account = pool_accounts[(scrape_round - 1) % len(pool_accounts)]
                scrape_auth  = scrape_account["auth_token"]
                scrape_ct0   = scrape_account["ct0"]
                scrape_proxy = scrape_account.get("proxy")
                skipped_cnt = len(candidate_items) - len(unprocessed_candidates)
                skip_msg = f" ({skipped_cnt} previously checked handles skipped)" if skipped_cnt > 0 else ""
                log_fn("INFO", f"Country filter active — checking location for {len(unprocessed_candidates)} new candidates{skip_msg}...")

                def check_candidate(cand):
                    h = cand["handle"]
                    bio_loc = cand["bio_location"]
                    cntry = fetch_account_based_in(scrape_auth, scrape_ct0, h, proxy=scrape_proxy, timeout=6, accounts_pool=pool_accounts)
                    return h, bio_loc, cntry

                with ThreadPoolExecutor(max_workers=3) as executor:
                    futures = [executor.submit(check_candidate, item) for item in unprocessed_candidates]
                    checked_count = 0
                    for future in as_completed(futures):
                        checked_count += 1
                        handle, bio_loc, account_country = future.result()
                        checked_handles_set.add(handle)

                        if account_country == "RATE_LIMITED":
                            # AboutAccountQuery rate limited — fallback instantly to bio location without 45s delay!
                            account_country = bio_loc

                        if account_country:
                            country_lower = account_country.lower()
                            if any(ck in country_lower for ck in country_keywords):
                                handles.append(handle)
                                log_fn("INFO", f"  [{checked_count}/{len(unprocessed_candidates)}] @{handle}: Location '{account_country}' ✓ MATCH")
                            else:
                                log_fn("DEBUG", f"  [{checked_count}/{len(unprocessed_candidates)}] @{handle}: Location '{account_country}' — skip")
                        else:
                            log_fn("DEBUG", f"  [{checked_count}/{len(unprocessed_candidates)}] @{handle}: Location unavailable — skip")
        else:
            handles = [c["handle"] for c in candidate_items]

        log_fn("INFO", f"Scraped {raw_count} total profiles from Twitter; {len(handles)} matched criteria ({min_followers}-{max_followers} followers{country_msg})")
        return handles, True, raw_count

    except Exception as exc:
        err_type = classify_account_error(exc)
        scrape_acc_id = accounts[0].get("id")
        if err_type == "BLOCKED" and scrape_acc_id:
            log_fn("ERROR", f"🚨 Scraper Account #{scrape_acc_id} was BLOCKED/SUSPENDED ({exc}). Automatically removing from database!")
            _delete_account_from_db(scrape_acc_id)
            accounts.pop(0)
        elif err_type == "RATE_LIMIT":
            log_fn("WARNING", f"⏳ Scraper Account encountered RATE LIMIT / RESTRICTION ({exc}). Leaving account to cool down.")
            if accounts:
                accounts[0]["cooldown_until"] = time.time() + 900
        else:
            log_fn("ERROR", f"Scraping failed: {exc}")
        logger.exception("_scrape_followers failed")
        return [], False, 0


_TESTED_PROXY_HEALTH_CACHE: dict[str, float] = {}

def _auto_heal_account_proxy(account_id: int, log_fn: Callable) -> Optional[str]:
    """
    When an account encounters a proxy error during scraping or posting:
    1. Fetches fresh SOCKS5 proxy from BetaSocks.
    2. Tests proxy connectivity to api.ipify.org.
    3. Updates database accounts table for account_id with new proxy.
    4. Returns new proxy string.
    """
    try:
        log_fn("INFO", f"⚡ Auto-Healing Triggered: Fetching fresh proxy from BetaSocks for Account (ID {account_id})…")
        try:
            from betasocks_client import BetaSocksClient
        except ImportError:
            from dashboard.betasocks_client import BetaSocksClient  # type: ignore

        client = BetaSocksClient()
        fresh_proxies = client.fetch_available_proxies(country="all", limit=5)

        working_proxy = None
        for px in fresh_proxies:
            clean_p = px.replace("socks5://", "").replace("http://", "")
            if "@" in clean_p:
                creds, host = clean_p.split("@")
                u, pw = creds.split(":")
                ip, port = host.split(":")
                db_proxy = f"{ip}:{port}:{u}:{pw}"
                curl_proxy = f"socks5://{u}:{pw}@{ip}:{port}"
            else:
                db_proxy = clean_p
                curl_proxy = f"socks5://{clean_p}"

            cmd = f"curl -s --proxy '{curl_proxy}' --max-time 5 https://api.ipify.org"
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if res.returncode == 0 and res.stdout and len(res.stdout.strip()) > 5:
                working_proxy = db_proxy
                break

        if working_proxy:
            conn = _db()
            conn.execute("UPDATE accounts SET proxy=? WHERE id=?", (working_proxy, account_id))
            conn.commit()
            conn.close()
            log_fn("INFO", f"✅ AUTO-HEAL SUCCESS: Account (ID {account_id}) assigned fresh working BetaSocks proxy ({working_proxy})!")
            return working_proxy
        else:
            log_fn("WARNING", f"⚠️ Auto-Healing: Could not find working BetaSocks proxy for Account (ID {account_id}) right now.")
            return None
    except Exception as exc:
        log_fn("WARNING", f"Auto-healing failed for Account (ID {account_id}): {exc}")
        return None


def _check_and_log_account_proxy_health(accounts: List[dict], log_fn: Callable) -> None:
    """Pre-flight check for account proxies to auto-heal and log explicit errors when a proxy fails."""
    for acc in accounts:
        aid = acc.get("id")
        px = acc.get("proxy")
        if not px:
            continue

        try:
            px_str = str(px).strip()
            if px_str.startswith("socks5://") or px_str.startswith("socks5h://") or px_str.startswith("http://"):
                px_url = px_str
            else:
                parts = px_str.split(":")
                if len(parts) == 4:
                    px_url = f"socks5://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
                else:
                    px_url = f"socks5://{px_str}"

            httpx.get("https://api.ipify.org?format=json", proxy=px_url, timeout=4)
        except Exception as exc:
            err_str = str(exc)
            log_fn("WARNING", f"🔌 Account (ID {aid}) PROXY FAILURE: Proxy '{px}' failed ({err_str}). Triggering auto-healing replacement...")
            healed_px = _auto_heal_account_proxy(aid, log_fn)
            if healed_px:
                acc["proxy"] = healed_px
            else:
                acc["proxy"] = None  # Fallback to direct connection so scraping doesn't stall


def _scrape_tweet_commenters(
    tweet_target: str,
    accounts: List[dict],
    limit: int = 100,
    log_fn: Callable = logger.info,
    min_followers: int = 0,
    max_followers: int = 1000,
    country_filter: str = "",
    scrape_round: int = 1,
    checked_candidates_set: set = None,
) -> Tuple[List[str], bool, int]:
    """
    Scrape handles of users who commented on / replied to a target tweet URL or ID.
    Supports follower count range, verified country location filter, and deduplication.
    Returns (handles, ok, raw_count) tuple.
    """
    if checked_candidates_set is None:
        checked_candidates_set = set()

    # Always include ALL registered active DB accounts in the scraping pool for maximum rotation
    try:
        conn = _db()
        all_db_accs = conn.execute("SELECT id, auth_token, ct0, proxy FROM accounts").fetchall()
        conn.close()
        pool_accounts = [dict(a) for a in all_db_accs] if all_db_accs else accounts
    except Exception:
        pool_accounts = accounts if accounts else []

    if not pool_accounts:
        log_fn("ERROR", "No accounts available for scraping.")
        return [], False, 0

    _check_and_log_account_proxy_health(pool_accounts, log_fn)

    target_clean = tweet_target.strip()
    tweet_id = ""
    target_user = ""

    if "status/" in target_clean:
        parts = target_clean.split("status/")
        tweet_id = parts[1].split("?")[0].split("/")[0].strip()
        if "x.com/" in parts[0] or "twitter.com/" in parts[0]:
            target_user = parts[0].rstrip("/").split("/")[-1].lstrip("@")
    elif target_clean.isdigit():
        tweet_id = target_clean
    else:
        target_user = target_clean.lstrip("@")

    queries = []
    if tweet_id:
        queries.append(f"conversation_id:{tweet_id}")
    if target_user:
        queries.append(f"to:{target_user}")
    if not queries:
        queries.append(target_clean)

    country_keywords = [
        alias.strip().lower()
        for alias in (country_filter or "").split(",")
        if alias.strip()
    ]

    try:
        from Scweet import Scweet, ScweetConfig  # type: ignore

        cookies_pool_list = []
        for acc in pool_accounts:
            entry = {"auth_token": acc["auth_token"], "ct0": acc["ct0"]}
            if acc.get("proxy"): entry["proxy"] = acc["proxy"]
            cookies_pool_list.append(entry)

        start_idx = (scrape_round - 1) % len(cookies_pool_list)
        rotated_cookies = cookies_pool_list[start_idx:] + cookies_pool_list[:start_idx]

        log_fn("INFO", f"Initialising Scweet commenter scraper for target: {target_clean}")
        cfg = ScweetConfig(daily_requests_limit=100000, daily_tweets_limit=100000)
        s = Scweet(
            cookies=rotated_cookies if len(rotated_cookies) > 1 else rotated_cookies[0],
            config=cfg,
        )

        candidate_items: List[dict] = []
        raw_count = 0

        for q in queries:
            results = s.search(q, limit=limit, save=False)
            if results:
                raw_count += len(results)
                for item in results:
                    if isinstance(item, dict):
                        user_obj = item.get("user") or item.get("author") or item
                        if isinstance(user_obj, str): user_obj = item
                        handle = (
                            user_obj.get("username")
                            or user_obj.get("screen_name")
                            or user_obj.get("handle")
                            or item.get("username")
                            or item.get("screen_name")
                            or ""
                        ).strip().lstrip("@").lower()
                        loc_str = str(user_obj.get("location") or item.get("location") or "").strip().lower()
                        rel_counts = user_obj.get("relationship_counts") if isinstance(user_obj, dict) else {}
                        fc = (
                            user_obj.get("followers_count")
                            or user_obj.get("followers")
                            or (rel_counts.get("followers") if isinstance(rel_counts, dict) else None)
                        )
                        if fc is not None and max_followers and max_followers > 0:
                            try:
                                val = int(fc)
                                if not (min_followers <= val <= max_followers):
                                    continue
                            except (ValueError, TypeError):
                                pass
                        if handle and handle != target_user.lower():
                            candidate_items.append({"handle": handle, "bio_location": loc_str, "followers_count": fc})
                    elif isinstance(item, str):
                        handle = item.strip().lstrip("@").lower()
                        if handle and handle != target_user.lower():
                            candidate_items.append({"handle": handle, "bio_location": "", "followers_count": None})
            if candidate_items:
                break

        handles: List[str] = []
        scrape_account = pool_accounts[(scrape_round - 1) % len(pool_accounts)]
        if country_keywords:
            unprocessed_candidates = [c for c in candidate_items if c["handle"] not in checked_candidates_set]
            if unprocessed_candidates:
                from concurrent.futures import ThreadPoolExecutor, as_completed

                scrape_auth = scrape_account["auth_token"]
                scrape_ct0 = scrape_account["ct0"]
                scrape_proxy = scrape_account.get("proxy")

                def check_candidate(cand):
                    h = cand["handle"]
                    bio_loc = cand["bio_location"]
                    cand_fc = cand.get("followers_count")

                    if cand_fc is None and (min_followers > 0 or (max_followers and max_followers < 1000000)):
                        try:
                            from poster import get_profile_info
                            pinfo = get_profile_info(scrape_auth, scrape_ct0, h, proxy=scrape_proxy)
                            if pinfo and pinfo.get("followers_count") is not None:
                                cand_fc = pinfo.get("followers_count")
                        except Exception:
                            pass

                    if cand_fc is not None and max_followers and max_followers > 0:
                        if not (min_followers <= cand_fc <= max_followers):
                            return h, bio_loc, None, False, cand_fc

                    cntry = fetch_account_based_in(scrape_auth, scrape_ct0, h, proxy=scrape_proxy, timeout=6, accounts_pool=pool_accounts)
                    return h, bio_loc, cntry, True, cand_fc

                with ThreadPoolExecutor(max_workers=3) as executor:
                    futures = [executor.submit(check_candidate, item) for item in unprocessed_candidates]
                    checked_count = 0
                    for future in as_completed(futures):
                        checked_count += 1
                        handle, bio_loc, account_country, passed_fc, actual_fc = future.result()
                        checked_candidates_set.add(handle)
                        if not passed_fc:
                            fc_str = f"{actual_fc}" if actual_fc is not None else "unknown"
                            log_fn("DEBUG", f"  [{checked_count}/{len(unprocessed_candidates)}] @{handle}: Followers ({fc_str}) outside range {min_followers}-{max_followers} — skip")
                            continue
                        if account_country == "RATE_LIMITED":
                            account_country = bio_loc
                        if account_country:
                            if any(ck in account_country.lower() for ck in country_keywords):
                                handles.append(handle)
                                log_fn("INFO", f"  [{checked_count}/{len(unprocessed_candidates)}] @{handle}: Location '{account_country}' ✓ MATCH")
                            else:
                                log_fn("DEBUG", f"  [{checked_count}/{len(unprocessed_candidates)}] @{handle}: Location '{account_country}' — skip")
                        else:
                            log_fn("DEBUG", f"  [{checked_count}/{len(unprocessed_candidates)}] @{handle}: Location unavailable — skip")
        else:
            handles = [c["handle"] for c in candidate_items if c["handle"] not in checked_candidates_set]

        unique_handles = list(dict.fromkeys(handles))
        log_fn("INFO", f"Scraped {raw_count} commenter handles for target {target_clean}; {len(unique_handles)} matched criteria")
        return unique_handles, True, raw_count

    except Exception as exc:
        err_type = classify_account_error(exc)
        scrape_acc_id = accounts[0].get("id")
        if err_type == "BLOCKED" and scrape_acc_id:
            log_fn("ERROR", f"🚨 Scraper Account #{scrape_acc_id} was BLOCKED/SUSPENDED ({exc}). Automatically removing from database!")
            _delete_account_from_db(scrape_acc_id)
            accounts.pop(0)
        elif err_type == "RATE_LIMIT":
            log_fn("WARNING", f"⏳ Scraper Account encountered RATE LIMIT / RESTRICTION ({exc}). Leaving account to cool down.")
            if accounts:
                accounts[0]["cooldown_until"] = time.time() + 900
        else:
            log_fn("ERROR", f"Commenter scraping failed: {exc}")
        logger.exception("_scrape_tweet_commenters failed")
        return [], False, 0


def _scrape_target_tweets_commenters(
    source_profiles: List[str],
    accounts: List[dict],
    limit: int,
    log_fn,
    min_followers: int = 0,
    max_followers: int = 1000,
    country_filter: str = "",
    scrape_round: int = 1,
    checked_candidates_set: set = None,
) -> Tuple[List[str], bool, int]:
    """
    Scrape commenters/repliers from recent top tweets originally posted by target profiles.
    Prioritizes recent comments under the target's top tweets.
    """
    if checked_candidates_set is None:
        checked_candidates_set = set()

    try:
        conn = _db()
        all_db_accs = conn.execute("SELECT id, auth_token, ct0, proxy FROM accounts").fetchall()
        conn.close()
        pool_accounts = [dict(a) for a in all_db_accs] if all_db_accs else accounts
    except Exception:
        pool_accounts = accounts if accounts else []

    if not pool_accounts:
        log_fn("ERROR", "No accounts available for scraping.")
        return [], False, 0

    try:
        from Scweet import Scweet, ScweetConfig  # type: ignore

        cookies_pool_list = []
        for acc in pool_accounts:
            entry = {"auth_token": acc["auth_token"], "ct0": acc["ct0"]}
            if acc.get("proxy"): entry["proxy"] = acc["proxy"]
            cookies_pool_list.append(entry)

        start_idx = (scrape_round - 1) % len(cookies_pool_list)
        rotated_cookies = cookies_pool_list[start_idx:] + cookies_pool_list[:start_idx]

        cfg = ScweetConfig(daily_requests_limit=100000, daily_tweets_limit=100000)
        s = Scweet(
            cookies=rotated_cookies if len(rotated_cookies) > 1 else rotated_cookies[0],
            config=cfg,
        )

        country_keywords = [
            alias.strip().lower()
            for alias in (country_filter or "").split(",")
            if alias.strip()
        ]

        candidate_items: List[dict] = []
        raw_count = 0

        for profile in source_profiles:
            clean_user = profile.strip().lstrip("@")
            if not clean_user:
                continue

            log_fn("INFO", f"Scraping recent active commenters replying to @{clean_user} (1-call search)...")
            comment_results = None
            try:
                comment_results = s.search(f"to:{clean_user}", limit=max(limit * 2, 60), save=False)
            except Exception as search_err:
                log_fn("WARNING", f"Direct commenter search to:{clean_user} paused: {search_err}")

            if not comment_results:
                try:
                    tweet_results = s.search(f"from:{clean_user} -is:retweet", limit=2, save=False)
                    if tweet_results:
                        tweet_ids = [
                            str(tr.get("id") or tr.get("tweet_id") or tr.get("id_str") or "").strip()
                            for tr in tweet_results if isinstance(tr, dict)
                        ]
                        for tid in tweet_ids[:2]:
                            if tid:
                                res = s.search(f"conversation_id:{tid}", limit=max(limit, 30), save=False)
                                if res:
                                    comment_results = (comment_results or []) + res
                except Exception as search_err:
                    log_fn("WARNING", f"Top tweet conversation search for @{clean_user} paused: {search_err}")

            if comment_results:
                raw_count += len(comment_results)
                for item in comment_results:
                    if isinstance(item, dict):
                        user_obj = item.get("user") or item.get("author") or item
                        if isinstance(user_obj, str): user_obj = item
                        handle = (
                            user_obj.get("username")
                            or user_obj.get("screen_name")
                            or user_obj.get("handle")
                            or item.get("username")
                            or item.get("screen_name")
                            or ""
                        ).strip().lstrip("@").lower()
                        loc_str = str(user_obj.get("location") or item.get("location") or "").strip().lower()
                        rel_counts = user_obj.get("relationship_counts") if isinstance(user_obj, dict) else {}
                        fc = (
                            user_obj.get("followers_count")
                            or user_obj.get("followers")
                            or (rel_counts.get("followers") if isinstance(rel_counts, dict) else None)
                        )
                        if fc is not None and max_followers and max_followers > 0:
                            try:
                                val = int(fc)
                                if not (min_followers <= val <= max_followers):
                                    continue
                            except (ValueError, TypeError):
                                pass
                        if handle and handle != clean_user.lower():
                            candidate_items.append({"handle": handle, "bio_location": loc_str, "followers_count": fc})
                    elif isinstance(item, str):
                        handle = item.strip().lstrip("@").lower()
                        if handle and handle != clean_user.lower():
                            candidate_items.append({"handle": handle, "bio_location": "", "followers_count": None})

        handles: List[str] = []
        scrape_account = pool_accounts[(scrape_round - 1) % len(pool_accounts)]
        if country_keywords:
            unprocessed_candidates = [c for c in candidate_items if c["handle"] not in checked_candidates_set]
            if unprocessed_candidates:
                from concurrent.futures import ThreadPoolExecutor, as_completed

                scrape_auth = scrape_account["auth_token"]
                scrape_ct0 = scrape_account["ct0"]
                scrape_proxy = scrape_account.get("proxy")

                def check_candidate(cand):
                    h = cand["handle"]
                    bio_loc = cand["bio_location"]
                    cand_fc = cand.get("followers_count")

                    if cand_fc is None and (min_followers > 0 or (max_followers and max_followers < 1000000)):
                        try:
                            from poster import get_profile_info
                            pinfo = get_profile_info(scrape_auth, scrape_ct0, h, proxy=scrape_proxy)
                            if pinfo and pinfo.get("followers_count") is not None:
                                cand_fc = pinfo.get("followers_count")
                        except Exception:
                            pass

                    if cand_fc is not None and max_followers and max_followers > 0:
                        if not (min_followers <= cand_fc <= max_followers):
                            return h, bio_loc, None, False, cand_fc

                    cntry = fetch_account_based_in(scrape_auth, scrape_ct0, h, proxy=scrape_proxy, timeout=6, accounts_pool=pool_accounts)
                    return h, bio_loc, cntry, True, cand_fc

                with ThreadPoolExecutor(max_workers=3) as executor:
                    futures = [executor.submit(check_candidate, item) for item in unprocessed_candidates]
                    checked_count = 0
                    for future in as_completed(futures):
                        checked_count += 1
                        handle, bio_loc, account_country, passed_fc, actual_fc = future.result()
                        checked_candidates_set.add(handle)
                        if not passed_fc:
                            fc_str = f"{actual_fc}" if actual_fc is not None else "unknown"
                            log_fn("DEBUG", f"  [{checked_count}/{len(unprocessed_candidates)}] @{handle}: Followers ({fc_str}) outside range {min_followers}-{max_followers} — skip")
                            continue
                        if account_country == "RATE_LIMITED":
                            account_country = bio_loc
                        if account_country:
                            if any(ck in account_country.lower() for ck in country_keywords):
                                handles.append(handle)
                                log_fn("INFO", f"  [{checked_count}/{len(unprocessed_candidates)}] @{handle}: Location '{account_country}' ✓ MATCH")
                            else:
                                log_fn("DEBUG", f"  [{checked_count}/{len(unprocessed_candidates)}] @{handle}: Location '{account_country}' — skip")
                        else:
                            log_fn("DEBUG", f"  [{checked_count}/{len(unprocessed_candidates)}] @{handle}: Location unavailable — skip")
        else:
            handles = [c["handle"] for c in candidate_items if c["handle"] not in checked_candidates_set]

        unique_handles = list(dict.fromkeys(handles))
        log_fn("INFO", f"Scraped {raw_count} total comments from recent top tweets; {len(unique_handles)} matched criteria")
        return unique_handles, True, raw_count

    except Exception as exc:
        err_type = classify_account_error(exc)
        scrape_acc_id = accounts[0].get("id") if accounts else None
        if err_type == "BLOCKED" and scrape_acc_id:
            log_fn("ERROR", f"🚨 Scraper Account #{scrape_acc_id} was BLOCKED/SUSPENDED ({exc}). Automatically removing from database!")
            _delete_account_from_db(scrape_acc_id)
            if accounts: accounts.pop(0)
        elif err_type == "RATE_LIMIT":
            log_fn("WARNING", f"⏳ Scraper Account encountered RATE LIMIT / RESTRICTION ({exc}). Leaving account to cool down.")
            if accounts: accounts[0]["cooldown_until"] = time.time() + 900
        else:
            log_fn("ERROR", f"Target tweet commenter scraping failed: {exc}")
        logger.exception("_scrape_target_tweets_commenters failed")
        return [], False, 0


def _get_or_create_account_list(
    acc: dict, list_name: str, list_desc: str, poster, log_fn, campaign_id: int = 0
) -> tuple[str, str]:
    """
    Dynamic List Rotation:
    Gets or creates a list owned by `acc`.
    Rotates to a fresh list per campaign run session (or when list reaches 10 posts)
    to bypass Twitter's link preview card caching and force native List Card widget updates.
    Recycles existing lists if create_list rate limit is hit.
    Returns (list_id, list_url).
    """
    acc_id = acc.get("id", 0)
    acc_label = f"Account #{acc_id}" if acc_id else "Account"
    session_list_name = f"{list_name}_{campaign_id}" if campaign_id else list_name

    # 1. Check DB for active list for this session that has posted < 10 times
    try:
        conn = sqlite3.connect(DASH_DB)
        row = conn.execute(
            """SELECT list_id, list_url, post_count FROM lists 
               WHERE account_id=? AND list_name=? 
               ORDER BY id DESC LIMIT 1""",
            (acc_id, session_list_name),
        ).fetchone()
        conn.close()
        if row and row[0]:
            pcount = row[2] if len(row) > 2 and row[2] is not None else 0
            if pcount < 10:
                conn = sqlite3.connect(DASH_DB)
                conn.execute("UPDATE lists SET post_count = COALESCE(post_count, 0) + 1 WHERE list_id=?", (row[0],))
                conn.commit()
                conn.close()
                return str(row[0]), str(row[1])
    except Exception:
        pass

    # 2. Try creating a fresh list for this session/batch
    fresh_title = f"{list_name[:20]}"
    try:
        log_fn("INFO", f"Creating fresh session list '{fresh_title}' for {acc_label} to ensure fresh List Card preview...")
        list_info = poster.create_list(
            acc["auth_token"], acc["ct0"], fresh_title, list_desc,
            proxy=acc.get("proxy"),
        )
        lid = list_info["list_id"]
        lurl = list_info["list_url"]
        _save_list_to_db(acc_id, lid, lurl, session_list_name)
        log_fn("SUCCESS", f"Fresh campaign list created for {acc_label}: {lurl}")
        return lid, lurl
    except Exception as exc:
        log_fn("WARNING", f"Could not create new list for {acc_label} ({exc}). Recycling existing list fallback...")

    # 3. Fallback: Recycle existing list from DB or Twitter account if create_list rate limit hit
    try:
        conn = sqlite3.connect(DASH_DB)
        row = conn.execute(
            "SELECT list_id, list_url FROM lists WHERE account_id=? ORDER BY id DESC LIMIT 1",
            (acc_id,),
        ).fetchone()
        conn.close()
        if row and row[0]:
            return str(row[0]), str(row[1])
    except Exception:
        pass

    try:
        owned_lists = poster.get_user_lists(
            acc["auth_token"], acc["ct0"], proxy=acc.get("proxy")
        )
        if owned_lists:
            ol = owned_lists[0]
            lid = ol["list_id"]
            lurl = ol["list_url"]
            _save_list_to_db(acc_id, lid, lurl, list_name)
            return lid, lurl
    except Exception as exc:
        logger.warning("Failed to query owned lists for fallback %s: %s", acc_label, exc)

    return "", ""


# ── Campaign thread ────────────────────────────────────────────────────────────
class _Campaign:
    def __init__(self, campaign_id: int, config: dict):
        self.campaign_id = campaign_id
        self.config = config
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"campaign-{self.campaign_id}"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── Main loop ─────────────────────────────────────────────────────────────
    def _run(self) -> None:
        cfg = self.config
        campaign_id = self.campaign_id

        _set_status(campaign_id, "running")
        self._log("INFO", "Campaign started")

        # ── Load accounts ─────────────────────────────────────────────────────
        accounts: list[dict] = cfg.get("accounts", [])
        if not accounts:
            self._log("ERROR", "No accounts configured — aborting")
            _set_status(campaign_id, "error")
            return

        target_type: str = cfg.get("target_type", "followers")
        source_profiles_raw: str = cfg.get("source_profiles", "")
        source_profiles: list[str] = [
            p.strip().lstrip("@")
            for p in source_profiles_raw.split(",")
            if p.strip()
        ]
        if not source_profiles and not source_profiles_raw.strip():
            self._log("ERROR", "No target profile or tweet configured — aborting.")
            _set_status(campaign_id, "error")
            return

        self._log("INFO", f"Target type: {target_type} | Target: {source_profiles_raw}")

        min_followers: int = int(cfg.get("min_followers", 0))
        max_followers: int = int(cfg.get("max_followers", 1000))
        country_filter: str = cfg.get("country_filter", "")
        self._log("INFO", f"Follower range filter: {min_followers} to {max_followers} followers")
        if country_filter:
            self._log("INFO", f"Country/Location filter active: '{country_filter}'")

        posting_mode: str = cfg.get("posting_mode", "list")
        self._log("INFO", f"Campaign Posting Mode: {posting_mode.upper()} POST")

        normal_media_data: Optional[str] = cfg.get("normal_media_data")
        normal_media_bytes: Optional[bytes] = None

        if normal_media_data:
            try:
                import base64
                if "," in normal_media_data:
                    b64_str = normal_media_data.split(",", 1)[1]
                else:
                    b64_str = normal_media_data
                normal_media_bytes = base64.b64decode(b64_str)
                self._log("INFO", "Loaded normal post media image from campaign config")
            except Exception as exc:
                self._log("WARNING", f"Could not decode normal post media image: {exc}")

        tags_per_post: int = max(1, min(5, int(cfg.get("tags_per_post", 3))))
        post_template: str = cfg.get("post_template", "Hello {taggings}")
        min_delay: int = int(cfg.get("min_delay_minutes", 8)) * 60
        max_delay: int = int(cfg.get("max_delay_minutes", 20)) * 60
        max_posts_per_account: int = int(cfg.get("max_posts_per_account", 30))

        display_name: str = cfg.get("display_name", "")
        body_text_tpl: str = cfg.get("body_text", "")
        username: str = cfg.get("username", "")
        update_list_banner: bool = cfg.get("update_list_banner", True)
        list_name: str = cfg.get("list_name", "Official Notice")
        list_desc: str = cfg.get("list_description", "")
        avatar_bytes: Optional[bytes] = None

        avatar_path = cfg.get("avatar_path")
        if avatar_path and os.path.isfile(avatar_path):
            try:
                with open(avatar_path, "rb") as f:
                    avatar_bytes = f.read()
            except Exception as exc:
                self._log("WARNING", f"Could not read avatar image: {exc}")

        already_tagged: set = _load_already_tagged(campaign_id)
        checked_candidates_set: set = set()
        self._log("INFO", f"Previously tagged usernames in this campaign: {len(already_tagged)}")

        # ── Posting loop with live-scraping and deduplication ─────────────────
        account_post_counts: dict[int, int] = {i: 0 for i in range(len(accounts))}
        account_index = 0
        total_posts = 0
        scrape_round = 0
        queue: list[str] = []   # buffer of fresh, un-tagged usernames

        while not self._stop_event.is_set():
            posts_in_this_round = 0
            n_accounts = len(accounts)
            if n_accounts == 0:
                self._log("ERROR", "No posting accounts available.")
                break

            # ── Run a posting pass over ALL active accounts in this round ──────
            account_i = 0
            while account_i < len(accounts) and not self._stop_event.is_set():
                acc = accounts[account_i]
                acc_id = acc.get("id")
                acc_key = acc_id or account_i

                cooldown_until = get_account_cooldown(acc_id) if acc_id else acc.get("cooldown_until", 0)
                if cooldown_until > time.time():
                    account_i += 1
                    continue

                if account_post_counts.get(acc_key, 0) >= max_posts_per_account:
                    account_i += 1
                    continue

                # ── Fill batch up to tags_per_post with verified handles ─────
                batch = []
                while len(batch) < tags_per_post and not self._stop_event.is_set():
                    if not queue:
                        scrape_round += 1
                        limit_this_round = max(tags_per_post * 10, 50) * scrape_round
                        self._log(
                            "INFO",
                            f"Scrape round {scrape_round} ({target_type}): fetching up to {limit_this_round} users "
                            f"for target {source_profiles_raw}…"
                        )
                        if target_type == "tweet_commenters":
                            raw_handles, ok, raw_count = _scrape_tweet_commenters(
                                source_profiles_raw, accounts, limit_this_round, self._log,
                                min_followers=min_followers, max_followers=max_followers,
                                country_filter=country_filter, scrape_round=scrape_round,
                                checked_candidates_set=checked_candidates_set,
                            )
                        elif target_type == "target_tweets_commenters":
                            raw_handles, ok, raw_count = _scrape_target_tweets_commenters(
                                source_profiles, accounts, limit_this_round, self._log,
                                min_followers=min_followers, max_followers=max_followers,
                                country_filter=country_filter, scrape_round=scrape_round,
                                checked_candidates_set=checked_candidates_set,
                            )
                        else:
                            raw_handles, ok, raw_count = _scrape_followers(
                                source_profiles, accounts, limit_this_round, self._log,
                                min_followers=min_followers, max_followers=max_followers,
                                country_filter=country_filter, scrape_round=scrape_round,
                                checked_handles_set=checked_candidates_set,
                            )

                        if not ok:
                            if not accounts:
                                self._log("ERROR", "No active accounts left for scraping — campaign stopping.")
                                _set_status(campaign_id, "error")
                                break
                            self._log(
                                "WARNING",
                                f"Scrape round {scrape_round} encountered an issue or restriction. Waiting 60s before retrying…"
                            )
                            time.sleep(60)
                            scrape_round -= 1
                            break

                        fresh = [h for h in raw_handles if h.lower() not in already_tagged and h.lower() not in batch]
                        random.shuffle(fresh)

                        self._log(
                            "INFO",
                            f"Round {scrape_round}: {raw_count} total scraped from Twitter, {len(raw_handles)} matched criteria ({min_followers}-{max_followers}), {len(fresh)} new (not yet tagged)"
                        )

                        if not fresh:
                            if raw_count == 0:
                                self._log(
                                    "WARNING",
                                    f"Round {scrape_round}: 0 candidates returned from Twitter (scraper accounts may be in cooldown or target exhausted). Waiting 60s before retrying…"
                                )
                                time.sleep(60)
                                scrape_round = max(0, scrape_round - 1)
                                break
                            else:
                                self._log(
                                    "INFO",
                                    f"Round {scrape_round}: {raw_count} profiles scraped, 0 passed criteria/new. Fetching deeper in next round…"
                                )
                                continue

                        queue = fresh

                    if not queue:
                        break

                    h = queue.pop(0)

                    # Pre-Tag Safety Shield: Verify handle matches follower range before adding to post!
                    if max_followers and max_followers > 0:
                        try:
                            from poster import get_profile_info
                            pinfo = get_profile_info(acc.get("auth_token", ""), acc.get("ct0", ""), h, proxy=acc.get("proxy"))
                            fc = pinfo.get("followers_count") if (pinfo and isinstance(pinfo, dict)) else None
                            if fc is None or (isinstance(pinfo, dict) and pinfo.get("error")):
                                self._log("WARNING", f"Pre-tag safety shield: Could not verify follower count for @{h} — dropping")
                                continue
                            if not (min_followers <= int(fc) <= max_followers):
                                self._log("WARNING", f"Pre-tag safety shield: @{h} has {fc} followers (outside range {min_followers}-{max_followers}) — dropping")
                                continue
                        except Exception as p_err:
                            self._log("WARNING", f"Pre-tag safety check error for @{h}: {p_err}")
                            continue

                    batch.append(h)

                if len(batch) < tags_per_post:
                    self._log("INFO", f"Batch has {len(batch)}/{tags_per_post} verified candidates. Waiting to refill full batch before posting…")
                    time.sleep(15)
                    continue

                acc_label = f"Account #{account_i + 1}" + (f" (ID {acc_id})" if acc_id else "")
                taggings = " ".join(f"@{h}" for h in batch)

                media_id = None
                acc_list_url = ""

                # Handle legacy posting_mode names for backwards compatibility
                if posting_mode == "normal":
                    if normal_media_bytes:
                        posting_mode_effective = "normal_custom"
                    elif display_name and body_text_tpl:
                        posting_mode_effective = "normal_card"
                    else:
                        posting_mode_effective = "normal_text"
                elif posting_mode == "list":
                    posting_mode_effective = "list_card" if update_list_banner else "list_static"
                else:
                    posting_mode_effective = posting_mode

                if posting_mode_effective == "normal_card":
                    card_bytes = None
                    if display_name and body_text_tpl:
                        card_body = body_text_tpl.replace("{taggings}", taggings) if "{taggings}" in body_text_tpl else f"{body_text_tpl}\n\n{taggings}"
                        try:
                            card_bytes = image_editor.generate_tweet_card_screenshot(
                                name=display_name,
                                username=username or display_name.lower().replace(" ", ""),
                                body_text=card_body,
                                avatar_bytes=avatar_bytes,
                                timestamp=cfg.get("timestamp") or "3:51 PM · 8/4/26",
                                views=cfg.get("views") or "3M",
                                replies=cfg.get("replies") or "2.8K",
                                retweets=cfg.get("retweets") or "4.2K",
                                likes=cfg.get("likes") or "54K",
                            )
                        except Exception as exc:
                            self._log("WARNING", f"Could not generate card image for {acc_label}: {exc}")

                    if card_bytes:
                        try:
                            up_res = poster.upload_media(acc["auth_token"], acc["ct0"], card_bytes, proxy=acc.get("proxy"))
                            media_id = up_res.get("media_id")
                            if media_id:
                                self._log("INFO", f"Uploaded generated card image for {acc_label} (Media ID: {media_id})")
                        except Exception as exc:
                            self._log("WARNING", f"Media upload failed for {acc_label}: {exc}")

                    tweet_text = post_template.replace("{taggings}", taggings).strip()[:280]
                    self._log("POST", f"{acc_label} (Normal Post + Generated Card) → {taggings[:80]}")
                    result = poster.post_tweet(acc["auth_token"], acc["ct0"], tweet_text, media_id=media_id, proxy=acc.get("proxy"))

                elif posting_mode_effective == "normal_custom":
                    if normal_media_bytes:
                        try:
                            up_res = poster.upload_media(
                                acc["auth_token"], acc["ct0"], normal_media_bytes, proxy=acc.get("proxy")
                            )
                            media_id = up_res.get("media_id")
                            if not media_id:
                                self._log("WARNING", f"Could not upload custom media image for {acc_label}: {up_res.get('error')}")
                            else:
                                self._log("INFO", f"Uploaded custom media image for {acc_label} (Media ID: {media_id})")
                        except Exception as exc:
                            self._log("WARNING", f"Media upload exception for {acc_label}: {exc}")

                    tweet_text = post_template.replace("{taggings}", taggings).strip()[:280]
                    self._log("POST", f"{acc_label} (Normal Post + Custom Media) → {taggings[:80]}")
                    result = poster.post_tweet(acc["auth_token"], acc["ct0"], tweet_text, media_id=media_id, proxy=acc.get("proxy"))

                elif posting_mode_effective == "normal_text":
                    tweet_text = post_template.replace("{taggings}", taggings).strip()[:280]
                    self._log("POST", f"{acc_label} (Normal Post Text Only) → {taggings[:80]}")
                    result = poster.post_tweet(acc["auth_token"], acc["ct0"], tweet_text, proxy=acc.get("proxy"))

                else:
                    # ── List Post Mode (list_card or list_static) ────────────────
                    acc_list_id, acc_list_url = _get_or_create_account_list(
                        acc, list_name, list_desc, poster, self._log, campaign_id=campaign_id
                    )

                    should_update_banner = (posting_mode_effective == "list_card")

                    # ── Update list banner image ─────────────────────────────
                    if should_update_banner and display_name and body_text_tpl:
                        if "{taggings}" in body_text_tpl:
                            card_body = body_text_tpl.replace("{taggings}", taggings)
                        else:
                            card_body = f"{body_text_tpl}\n\n{taggings}"

                        try:
                            batch_image_bytes = image_editor.generate_tweet_card_screenshot(
                                name=display_name,
                                username=username or display_name.lower().replace(" ", ""),
                                body_text=card_body,
                                avatar_bytes=avatar_bytes,
                                timestamp=cfg.get("timestamp") or "3:51 PM · 8/4/26",
                                views=cfg.get("views") or "3M",
                                replies=cfg.get("replies") or "2.8K",
                                retweets=cfg.get("retweets") or "4.2K",
                                likes=cfg.get("likes") or "54K",
                            )
                            if acc_list_id and batch_image_bytes:
                                poster.set_list_banner(
                                    acc["auth_token"], acc["ct0"],
                                    acc_list_id, batch_image_bytes,
                                    proxy=acc.get("proxy"),
                                )
                        except Exception as exc:
                            self._log("WARNING", f"Could not generate card image for {acc_label}: {exc}")

                    # ── Build tweet text & post ───────────────────────────────
                    tweet_text = post_template.replace("{taggings}", taggings)
                    for placeholder in ("{link}", "{list_url}", "{list}"):
                        if placeholder in tweet_text:
                            tweet_text = tweet_text.replace(placeholder, acc_list_url)
                            break
                    else:
                        if acc_list_url and acc_list_url not in tweet_text:
                            tweet_text = f"{tweet_text}\n{acc_list_url}"

                    tweet_text = tweet_text[:280]
                    self._log("POST", f"{acc_label} (List Post) → {taggings[:80]}")
                    result = poster.post_tweet(acc["auth_token"], acc["ct0"], tweet_text, proxy=acc.get("proxy"))

                if result.get("error") or not result.get("tweet_id"):
                    err_msg = result.get("error") or "Post verification failed: No tweet ID returned"
                    err_type = result.get("error_type") or poster.classify_account_error(err_msg, result.get("status_code"))

                    if err_type == "BLOCKED":
                        self._log("ERROR", f"🚨 {acc_label} has been BLOCKED/SUSPENDED ({err_msg}). Automatically removing account from database!")
                        if acc_id:
                            _delete_account_from_db(acc_id)
                        accounts.pop(account_i)
                        queue = batch + queue
                        if not accounts:
                            self._log("ERROR", "All campaign accounts have been removed due to blocks/suspensions. Campaign aborted.")
                            _set_status(campaign_id, "error")
                            return
                        continue
                    elif err_type == "PROXY_ERROR":
                        proxy_str = acc.get("proxy") or "configured proxy"
                        new_proxy = None
                        if acc_id:
                            new_proxy = _auto_heal_account_proxy(acc_id, self._log)

                        if new_proxy:
                            acc["proxy"] = new_proxy
                            self._log("INFO", f"🔄 Retrying post for {acc_label} using newly healed proxy ({new_proxy})!")
                            queue = batch + queue
                            continue

                        cooldown_mins = int(cfg.get("cooldown_minutes", 30))
                        self._log("ERROR", f"🔌 {acc_label} PROXY FAILURE: Could not connect via proxy '{proxy_str}' ({err_msg}). Please check or update this account's proxy in the Accounts tab.")
                        if acc_id:
                            set_account_cooldown(acc_id, cooldown_mins * 60)
                        acc["cooldown_until"] = time.time() + (cooldown_mins * 60)
                        queue = queue + batch  # end of queue — next account gets fresh handles
                        account_i += 1
                        continue
                    else:
                        cooldown_mins = int(cfg.get("cooldown_minutes", 30))
                        self._log("WARNING", f"⏳ {acc_label} post did not complete ({err_msg}). Cooling down account for {cooldown_mins} minutes to protect account safety.")
                        if acc_id:
                            set_account_cooldown(acc_id, cooldown_mins * 60)
                        acc["cooldown_until"] = time.time() + (cooldown_mins * 60)
                        # Put failed batch at END so the next account gets fresh handles
                        # (prevents Twitter duplicate-text error 187 within the same round)
                        queue = queue + batch
                        account_i += 1
                        continue
                else:
                    _mark_tagged(campaign_id, batch)
                    already_tagged.update(h.lower() for h in batch)
                    account_post_counts[acc_key] = account_post_counts.get(acc_key, 0) + 1
                    total_posts += 1
                    posts_in_this_round += 1
                    self._log("SUCCESS", f"Tweet Verified & Posted: {result.get('tweet_url', 'URL verified')}")

                account_i += 1

                # Brief 12s pause between accounts in the same round
                if account_i < len(accounts) and not self._stop_event.is_set():
                    time.sleep(12)

            # ── Check overall account statuses after round ─────────────────────
            if not accounts:
                self._log("ERROR", "🚨 All campaign accounts have been removed due to blocks/suspensions. Stopping campaign automatically.")
                _set_status(campaign_id, "stopped")
                break

            all_cooling = all(
                is_account_cooling(a.get("id")) if a.get("id") else (a.get("cooldown_until", 0) > time.time())
                for a in accounts
            )
            all_maxed = all(account_post_counts.get(a.get("id") or idx, 0) >= max_posts_per_account for idx, a in enumerate(accounts))

            if all_maxed:
                self._log("WARNING", "All accounts have reached their daily post limit. Campaign complete.")
                _set_status(campaign_id, "stopped")
                break

            if all_cooling:
                # Find when the soonest account wakes up and wait it out instead of stopping
                soonest = min(
                    (
                        get_account_cooldown(a["id"]) if a.get("id") else a.get("cooldown_until", 0)
                    )
                    for a in accounts
                )
                wait_secs = max(0, soonest - time.time())
                wait_mins = int(wait_secs // 60)
                wait_sec_rem = int(wait_secs % 60)
                self._log(
                    "WARNING",
                    f"⏳ All accounts are cooling down. Resuming automatically in {wait_mins}m {wait_sec_rem}s when the first account becomes available…",
                )
                elapsed = 0
                while elapsed < wait_secs and not self._stop_event.is_set():
                    time.sleep(5)
                    elapsed += 5
                if self._stop_event.is_set():
                    break
                self._log("INFO", "Account cooldown expired — resuming campaign.")
                continue

            # ── Main interval delay between posting rounds ────────────────────
            if not self._stop_event.is_set():
                delay = random.randint(min_delay, max_delay)
                self._log("INFO", f"Posting round complete ({posts_in_this_round} post(s)). Waiting {delay // 60}m {delay % 60}s before next round…")
                elapsed = 0
                while elapsed < delay and not self._stop_event.is_set():
                    time.sleep(5)
                    elapsed += 5

        status = "stopped" if self._stop_event.is_set() else "done"
        _set_status(campaign_id, status)
        self._log("INFO", f"Campaign finished. Total posts this session: {total_posts}")

    def _log(self, level: str, message: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = {"ts": ts, "level": level, "msg": message}
        logger.info("[campaign-%d] [%s] %s", self.campaign_id, level, message)
        _log_to_db(self.campaign_id, entry)


# ── Public API ─────────────────────────────────────────────────────────────────
def launch_campaign(campaign_id: int, config: dict) -> None:
    """Create and start a _Campaign background thread."""
    with _lock:
        existing = _campaigns.get(campaign_id)
        if existing and existing.is_running():
            logger.info("Campaign %d is already running in active thread. Ignoring duplicate launch request.", campaign_id)
            return
        # Clear stale cooldowns for this campaign's accounts so a resume starts fresh
        for acc in config.get("accounts", []):
            acc_id = acc.get("id")
            if acc_id and acc_id in _GLOBAL_ACCOUNT_COOLDOWNS:
                _GLOBAL_ACCOUNT_COOLDOWNS.pop(acc_id, None)
        c = _Campaign(campaign_id, config)
        _campaigns[campaign_id] = c
        c.start()
