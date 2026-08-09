"""
scheduler_engine.py — Background campaign scheduler.
Runs posting campaigns in a background thread, rotating accounts,
enforcing safe inter-post delays, and logging all activity.

Followers are scraped live from source_profiles (specified per campaign).
Every tagged username is written to the campaign_tagged table so no
user is ever tagged twice for the same campaign, even across restarts.
"""
from __future__ import annotations

import json
import logging
import os
import random
import sqlite3
import sys
import threading
import time
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

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
                (json.dumps(log[-500:]), campaign_id),  # keep last 500 entries
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
) -> Tuple[List[str], bool]:
    """
    Scrape up to *limit* followers from *source_profiles* using Scweet in streaming mode (save=False).
    Returns (handles, ok) tuple.
    Uses the first account's credentials for scraping.
    """
    if not accounts:
        log_fn("ERROR", "No accounts available for scraping.")
        return [], False

    try:
        from Scweet import Scweet, ScweetConfig  # type: ignore

        scrape_account = accounts[0]
        cookies_entry = {
            "auth_token": scrape_account["auth_token"],
            "ct0": scrape_account["ct0"],
        }
        if scrape_account.get("proxy"):
            cookies_entry["proxy"] = scrape_account["proxy"]

        log_fn("INFO", f"Initialising streaming Scweet scraper for source profiles: {source_profiles} (followers range: {min_followers}-{max_followers})")
        cfg = ScweetConfig(daily_requests_limit=100000, daily_tweets_limit=100000)
        s = Scweet(
            cookies=cookies_entry,
            config=cfg,
        )

        handles: List[str] = []
        # save=False to stream results in memory without creating CSV files
        results = s.get_followers(source_profiles, limit=limit, save=False)

        if results:
            for item in results:
                if isinstance(item, dict):
                    handle = (
                        item.get("username")
                        or item.get("screen_name")
                        or item.get("handle")
                        or ""
                    ).strip().lstrip("@").lower()
                    fc = item.get("followers_count") or item.get("followers") or item.get("followers_cnt")
                    if fc is not None:
                        try:
                            val = int(fc)
                            if not (min_followers <= val <= max_followers):
                                continue
                        except (ValueError, TypeError):
                            pass
                elif isinstance(item, str):
                    handle = item.strip().lstrip("@").lower()
                else:
                    handle = ""
                if handle:
                    handles.append(handle)

        log_fn("INFO", f"Scraped {len(handles)} handles matching follower criteria from {source_profiles}")
        return handles, True

    except Exception as exc:
        from .poster import classify_account_error  # type: ignore
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
        return [], False


def _scrape_tweet_commenters(
    tweet_target: str,
    accounts: List[dict],
    limit: int,
    log_fn,
) -> Tuple[List[str], bool]:
    """
    Scrape handles of users who commented on / replied to a target tweet URL or ID.
    Returns (handles, ok) tuple.
    """
    if not accounts:
        log_fn("ERROR", "No accounts available for scraping.")
        return [], False

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

    try:
        from Scweet import Scweet, ScweetConfig  # type: ignore

        scrape_account = accounts[0]
        cookies_entry = {
            "auth_token": scrape_account["auth_token"],
            "ct0": scrape_account["ct0"],
        }
        if scrape_account.get("proxy"):
            cookies_entry["proxy"] = scrape_account["proxy"]

        log_fn("INFO", f"Initialising Scweet commenter scraper for target: {target_clean}")
        cfg = ScweetConfig(daily_requests_limit=100000, daily_tweets_limit=100000)
        s = Scweet(
            cookies=cookies_entry,
            config=cfg,
        )

        handles: List[str] = []
        for q in queries:
            results = s.search(q, limit=limit, save=False)
            if results:
                for item in results:
                    if isinstance(item, dict):
                        handle = (
                            item.get("username")
                            or item.get("screen_name")
                            or item.get("handle")
                            or ""
                        ).strip().lstrip("@").lower()
                    elif isinstance(item, str):
                        handle = item.strip().lstrip("@").lower()
                    else:
                        handle = ""
                    if handle and handle != target_user.lower():
                        handles.append(handle)
            if handles:
                break

        log_fn("INFO", f"Scraped {len(handles)} commenter handles for target {target_clean}")
        return handles, True

    except Exception as exc:
        from .poster import classify_account_error  # type: ignore
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
        return [], False


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
        try:
            from . import poster, image_editor  # type: ignore
        except ImportError:
            import poster, image_editor  # type: ignore

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
        # ── Source profiles or Tweet URL/ID to scrape from ─────────────────────
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

        # ── Follower range filtering ──────────────────────────────────────────
        min_followers: int = int(cfg.get("min_followers", 0))
        max_followers: int = int(cfg.get("max_followers", 1000))
        self._log("INFO", f"Follower range filter: {min_followers} to {max_followers} followers")

        # ── Posting & scraping settings ───────────────────────────────────────
        tags_per_post: int = max(1, min(5, int(cfg.get("tags_per_post", 3))))
        post_template: str = cfg.get("post_template", "Hello {taggings}")
        min_delay: int = int(cfg.get("min_delay_minutes", 8)) * 60
        max_delay: int = int(cfg.get("max_delay_minutes", 20)) * 60
        max_posts_per_account: int = int(cfg.get("max_posts_per_account", 30))

        display_name: str = cfg.get("display_name", "")
        body_text_tpl: str = cfg.get("body_text", "")
        username: str = cfg.get("username", "")
        update_list_banner: bool = cfg.get("update_list_banner", True)
        avatar_bytes: Optional[bytes] = None

        avatar_path = cfg.get("avatar_path")
        if avatar_path and os.path.isfile(avatar_path):
            try:
                with open(avatar_path, "rb") as f:
                    avatar_bytes = f.read()
            except Exception as exc:
                self._log("WARNING", f"Could not read avatar image: {exc}")

        # ── Load already-tagged usernames (deduplication set) ─────────────────
        already_tagged: set = _load_already_tagged(campaign_id)
        self._log("INFO", f"Previously tagged usernames in this campaign: {len(already_tagged)}")

        # ── Create or Reuse ONE shared list for the campaign ─────────────────
        list_name: str = cfg.get("list_name", "Official Notice")
        list_desc: str = cfg.get("list_description", "")
        shared_list_url: str = ""
        shared_list_id: str = ""

        first_account = accounts[0]
        first_acc_id = first_account.get("id", 0)

        # 1. Check DB first for an existing list for this account and name
        try:
            conn = sqlite3.connect(DASH_DB)
            row = conn.execute(
                "SELECT list_id, list_url FROM lists WHERE account_id=? AND list_name=? ORDER BY id DESC LIMIT 1",
                (first_acc_id, list_name),
            ).fetchone()
            conn.close()
            if row and row[0]:
                shared_list_id = row[0]
                shared_list_url = row[1]
        except Exception:
            pass

        # 2. Check Twitter account owned lists if not found in DB
        if not shared_list_id:
            try:
                owned_lists = poster.get_user_lists(
                    first_account["auth_token"], first_account["ct0"], proxy=first_account.get("proxy")
                )
                for ol in owned_lists:
                    if ol.get("list_name", "").strip().lower() == list_name.strip().lower():
                        shared_list_id = ol["list_id"]
                        shared_list_url = ol["list_url"]
                        _save_list_to_db(first_acc_id, shared_list_id, shared_list_url, list_name)
                        break
            except Exception as exc:
                logger.warning("Failed to query owned lists: %s", exc)

        if shared_list_id:
            self._log("INFO", f"Reusing existing campaign list '{list_name}' ({shared_list_url})")
        else:
            try:
                self._log("INFO", f"Creating campaign list '{list_name}'...")
                list_info = poster.create_list(
                    first_account["auth_token"], first_account["ct0"], list_name, list_desc,
                    proxy=first_account.get("proxy"),
                )
                shared_list_id  = list_info["list_id"]
                shared_list_url = list_info["list_url"]
                _save_list_to_db(
                    first_acc_id,
                    shared_list_id,
                    shared_list_url,
                    list_name,
                )
                self._log("SUCCESS", f"Campaign list created: {shared_list_url}")
            except Exception as exc:
                self._log("ERROR", f"List creation failed: {exc}")

        if shared_list_id and not update_list_banner and display_name and body_text_tpl:
            static_body = body_text_tpl.replace("{taggings}", "").strip()
            try:
                static_img = image_editor.generate_tweet_card_screenshot(
                    name=display_name,
                    username=username or display_name.lower().replace(" ", ""),
                    body_text=static_body,
                    avatar_bytes=avatar_bytes,
                    timestamp=cfg.get("timestamp") or "3:51 PM · 8/4/26",
                    views=cfg.get("views") or "3M",
                    replies=cfg.get("replies") or "2.8K",
                    retweets=cfg.get("retweets") or "4.2K",
                    likes=cfg.get("likes") or "54K",
                )
                self._log("INFO", "Setting initial static list profile picture...")
                ok = poster.set_list_banner(
                    first_account["auth_token"], first_account["ct0"],
                    shared_list_id, static_img,
                    proxy=first_account.get("proxy"),
                )
                if ok:
                    self._log("SUCCESS", "Initial static list profile picture set successfully")
            except Exception as exc:
                self._log("WARNING", f"Could not set static list profile picture: {exc}")

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

                # Skip account if cooling down or max limit reached
                cooldown_until = get_account_cooldown(acc_id) if acc_id else acc.get("cooldown_until", 0)
                if cooldown_until > time.time():
                    account_i += 1
                    continue

                if account_post_counts.get(acc_key, 0) >= max_posts_per_account:
                    account_i += 1
                    continue

                # ── Refill queue when empty ──────────────────────────────────
                if not queue:
                    scrape_round += 1
                    limit_this_round = max(tags_per_post * 5, 20) * scrape_round
                    self._log(
                        "INFO",
                        f"Scrape round {scrape_round} ({target_type}): fetching up to {limit_this_round} users "
                        f"for target {source_profiles_raw}…"
                    )
                    if target_type == "tweet_commenters":
                        raw_handles, ok = _scrape_tweet_commenters(
                            source_profiles_raw, accounts, limit_this_round, self._log
                        )
                    else:
                        raw_handles, ok = _scrape_followers(
                            source_profiles, accounts, limit_this_round, self._log,
                            min_followers=min_followers, max_followers=max_followers,
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
                        scrape_round -= 1  # retry this round
                        continue

                    fresh = [h for h in raw_handles if h.lower() not in already_tagged]
                    self._log(
                        "INFO",
                        f"Round {scrape_round}: {len(raw_handles)} scraped, {len(fresh)} new (not yet tagged)"
                    )

                    if not fresh:
                        if len(raw_handles) == 0:
                            self._log(
                                "WARNING",
                                "No followers returned from source profiles. Source profiles may be exhausted or private. Campaign complete."
                            )
                            break
                        else:
                            self._log(
                                "INFO",
                                f"All {len(raw_handles)} scraped followers in round {scrape_round} were already tagged. Fetching deeper in next round…"
                            )
                            continue

                    queue = fresh

                if not queue:
                    break

                # ── Pop a batch from the queue ────────────────────────────────
                batch = queue[:tags_per_post]
                queue = queue[tags_per_post:]

                acc_label = f"Account #{account_i + 1}" + (f" (ID {acc_id})" if acc_id else "")
                taggings = " ".join(f"@{h}" for h in batch)

                # ── Update list banner image ─────────────────────────────────
                if update_list_banner and display_name and body_text_tpl:
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
                        if shared_list_id and batch_image_bytes:
                            ok = poster.set_list_banner(
                                acc["auth_token"], acc["ct0"],
                                shared_list_id, batch_image_bytes,
                                proxy=acc.get("proxy"),
                            )
                            if not ok:
                                self._log("WARNING", "Could not update list profile picture")
                    except Exception as exc:
                        self._log("WARNING", f"Could not generate/update card image: {exc}")

                # ── Build tweet text & post ───────────────────────────────────
                tweet_text = post_template.replace("{taggings}", taggings)
                for placeholder in ("{link}", "{list_url}", "{list}"):
                    if placeholder in tweet_text:
                        tweet_text = tweet_text.replace(placeholder, shared_list_url)
                        break
                else:
                    if shared_list_url and shared_list_url not in tweet_text:
                        tweet_text = f"{tweet_text}\n{shared_list_url}"

                tweet_text = tweet_text[:280]

                self._log("POST", f"{acc_label} → {taggings[:80]}")
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
                    else:
                        cooldown_mins = int(cfg.get("cooldown_minutes", 30))
                        self._log("WARNING", f"⏳ {acc_label} post did not complete ({err_msg}). Cooling down account for {cooldown_mins} minutes to protect account safety.")
                        if acc_id:
                            set_account_cooldown(acc_id, cooldown_mins * 60)
                        acc["cooldown_until"] = time.time() + (cooldown_mins * 60)
                        queue = batch + queue
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
    # Clear stale cooldowns for this campaign's accounts so a resume starts fresh
    with _lock:
        for acc in config.get("accounts", []):
            acc_id = acc.get("id")
            if acc_id and acc_id in _GLOBAL_ACCOUNT_COOLDOWNS:
                _GLOBAL_ACCOUNT_COOLDOWNS.pop(acc_id, None)
    c = _Campaign(campaign_id, config)
    with _lock:
        _campaigns[campaign_id] = c
    c.start()
