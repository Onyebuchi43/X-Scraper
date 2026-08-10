"""
poster.py — Direct Twitter/X API engine for the Scweet Dashboard.
Handles: list creation, media upload, list banner, tweet posting.
This module is intentionally standalone — it does NOT touch Scweet internals.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DASH_DB = os.path.join(os.path.dirname(__file__), "dashboard.db")

# ── Constants ─────────────────────────────────────────────────────────────────
BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
CREATE_TWEET_QUERY_ID = "lYrkzD_-rtW5H3wDiwlcWA"
CREATE_LIST_URL        = "https://api.twitter.com/1.1/lists/create.json"
GET_USER_LISTS_URL     = "https://api.twitter.com/1.1/lists/ownerships.json"
UPLOAD_MEDIA_URL       = "https://upload.twitter.com/i/media/upload.json"
CREATE_TWEET_URL       = f"https://x.com/i/api/graphql/{CREATE_TWEET_QUERY_ID}/CreateTweet"
GQL_API                = "https://x.com/i/api/graphql"
EDIT_LIST_BANNER_QID   = "Uk0ZwKSMYng56aQdeJD1yw"
USER_LOOKUP_URL        = "https://x.com/i/api/graphql/Gb-d6r0vxPOADdG62OEBpQ/UserByScreenName"
# AboutAccountQuery — returns data.user_result_by_screen_name.result.about_profile.account_based_in
# This is the same data shown in X's "About this account" panel (verified by phone/app country)
ABOUT_ACCOUNT_QUERY_ID = "zs_jFPFT78rBpXv9Z3U2YQ"
ABOUT_ACCOUNT_URL      = f"https://x.com/i/api/graphql/{ABOUT_ACCOUNT_QUERY_ID}/AboutAccountQuery"


# ── Auth helpers ───────────────────────────────────────────────────────────────
def _headers(ct0: str, extra: Optional[dict] = None) -> dict:
    h = {
        "Authorization": f"Bearer {BEARER_TOKEN}",
        "X-Csrf-Token": ct0,
        "X-Twitter-Auth-Type": "OAuth2Session",
        "X-Twitter-Active-User": "yes",
        "X-Twitter-Client-Language": "en",
        "Referer": "https://x.com/",
        "Origin": "https://x.com",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if extra:
        h.update(extra)
    return h


def _cookies(auth_token: str, ct0: str) -> dict:
    return {"auth_token": auth_token, "ct0": ct0}


def classify_account_error(error: Exception | str, status_code: Optional[int] = None) -> str:
    """
    Categorize account errors:
      - "PROXY_ERROR": Proxy connection, authentication, or protocol failure -> report specific proxy error
      - "BLOCKED": Account is blocked, suspended, locked, unauthorized (401, 403) -> auto-remove from DB
      - "RATE_LIMIT": Account is rate-limited or restricted temporarily (429, 226, daily limit) -> cool down, do not remove
      - "OTHER": General error
    """
    err_str = str(error).lower()
    code = status_code
    if isinstance(error, httpx.HTTPStatusError):
        code = error.response.status_code
        try:
            err_str += " " + (error.response.text or "").lower()
        except Exception:
            pass

    proxy_keywords = [
        "proxyerror", "proxyconnectionerror", "proxy connection", "proxy timeout",
        "cannot connect to proxy", "proxy authentication", "407 proxy", "proxy auth",
        "socks", "connecterror", "connection refused", "proxy fail", "unexpected_eof_while_reading",
        "tunnel connection failed", "proxy connection refused", "invalidurl", "nonnumeric port"
    ]
    if isinstance(error, (httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout)) or any(k in err_str for k in proxy_keywords) or code == 407:
        return "PROXY_ERROR"

    if code in (429, 226) or any(k in err_str for k in ("rate limit", "too many requests", "daily limit", "throttled", "over daily", "226", "automated", "spam", "protect our users")):
        return "RATE_LIMIT"

    blocked_keywords = [
        "suspended", "account is locked", "account locked", "user has been suspended",
        "could not authenticate", "authenticity_token", "desktop applications",
        "access denied", "unauthorized", "login_flow", "account_disabled", "flow_disabled",
        "326", "141", "64", "32"
    ]
    if code in (401, 403) or any(k in err_str for k in blocked_keywords):
        return "BLOCKED"

    return "OTHER"


def _get_httpx_proxy_url(proxy: Optional[str | dict]) -> Optional[str]:
    if not proxy:
        return None
    url_str = ""
    if isinstance(proxy, dict):
        url_str = proxy.get("http") or proxy.get("https") or ""
    elif isinstance(proxy, str):
        url_str = proxy.strip()

    if not url_str:
        return None

    if not url_str.startswith("http://") and not url_str.startswith("https://") and not url_str.startswith("socks5://"):
        parts = url_str.split(":")
        if len(parts) == 4:
            return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        elif len(parts) == 2:
            return f"http://{url_str}"
        return f"http://{url_str}"

    if url_str.startswith("https://"):
        return "http://" + url_str[8:]

    return url_str


# ── Profile lookup ─────────────────────────────────────────────────────────────
def get_profile_info(auth_token: str, ct0: str, handle: str, proxy: Optional[str | dict] = None) -> dict:
    """Fetch basic profile data for *handle* (name, avatar URL, etc.)."""
    handle = handle.lstrip("@")
    params = {
        "variables": json.dumps({"screen_name": handle, "withSafetyModeUserFields": True}),
        "features": json.dumps({
            "hidden_profile_subscriptions_enabled": True,
            "rweb_tipjar_consumption_enabled": False,
            "responsive_web_graphql_exclude_directive_enabled": True,
            "verified_phone_label_enabled": False,
            "subscriptions_verification_info_is_identity_verified_enabled": True,
            "subscriptions_verification_info_verified_since_enabled": True,
            "highlights_tweets_tab_ui_enabled": True,
            "responsive_web_twitter_article_notes_tab_enabled": True,
            "creator_subscriptions_tweet_preview_api_enabled": True,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "responsive_web_graphql_timeline_navigation_enabled": True,
        }),
        "fieldToggles": json.dumps({"withAuxiliaryUserLabels": False}),
    }
    try:
        proxy_url = _get_httpx_proxy_url(proxy)
        httpx_kwargs = {"proxy": proxy_url} if proxy_url else {}
        resp = httpx.get(
            USER_LOOKUP_URL,
            params=params,
            headers=_headers(ct0),
            cookies=_cookies(auth_token, ct0),
            timeout=20,
            **httpx_kwargs,
        )
        resp.raise_for_status()
        legacy = (
            resp.json()
            .get("data", {})
            .get("user", {})
            .get("result", {})
            .get("legacy", {})
        )
        avatar_url = (
            legacy.get("profile_image_url_https", "")
            .replace("_normal", "_400x400")
        )
        return {
            "name": legacy.get("name", handle),
            "handle": legacy.get("screen_name", handle),
            "followers_count": legacy.get("followers_count", 0),
            "description": legacy.get("description", ""),
            "avatar_url": avatar_url,
            "verified": legacy.get("verified", False),
        }
    except Exception as exc:
        logger.warning("Profile lookup failed for @%s: %s", handle, exc)
        return {"name": handle, "handle": handle, "avatar_url": "", "followers_count": 0, "description": ""}


_ACCOUNT_LOCATION_CACHE: dict[str, Optional[str]] = {}


def _get_cached_location_from_db(username: str) -> Optional[str]:
    try:
        conn = sqlite3.connect(DASH_DB)
        row = conn.execute(
            "SELECT country FROM account_locations WHERE username=? AND country != '' AND country IS NOT NULL",
            (username.lower(),)
        ).fetchone()
        conn.close()
        if row and row[0]:
            return str(row[0]).strip()
    except Exception:
        pass
    return None


def _save_location_to_db(username: str, country: Optional[str]) -> None:
    if not country or not country.strip():
        return  # NEVER save empty or null locations to DB! Only save valid verified countries!
    try:
        conn = sqlite3.connect(DASH_DB)
        conn.execute(
            "INSERT OR REPLACE INTO account_locations (username, country) VALUES (?, ?)",
            (username.lower(), country.strip())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def fetch_account_based_in(
    auth_token: str,
    ct0: str,
    username: str,
    proxy: Optional[str | dict] = None,
    timeout: float = 10.0,
    accounts_pool: Optional[list[dict]] = None,
) -> Optional[str]:
    """
    Fetch the 'Account based in' country for *username* using X's internal
    AboutAccountQuery GraphQL endpoint — the same data shown in X's
    'About this account' panel. This is based on the phone/app country used
    to register the account, NOT the user-typed profile location.

    Returns:
    - Country string (e.g. "Nigeria", "United States")
    - "RATE_LIMITED" if Twitter rate-limited all accounts in the pool
    - None if query succeeded but no country data is present on account
    """
    clean_username = username.lstrip("@").lower()
    if clean_username in _ACCOUNT_LOCATION_CACHE and _ACCOUNT_LOCATION_CACHE[clean_username] is not None:
        return _ACCOUNT_LOCATION_CACHE[clean_username]

    db_loc = _get_cached_location_from_db(clean_username)
    if db_loc:
        _ACCOUNT_LOCATION_CACHE[clean_username] = db_loc
        return db_loc

    params = {
        "variables": json.dumps({"screenName": clean_username}),
    }

    pool = accounts_pool if accounts_pool else [{"auth_token": auth_token, "ct0": ct0, "proxy": proxy}]

    hit_429 = False
    for acc in pool:
        at = acc.get("auth_token", auth_token)
        c0 = acc.get("ct0", ct0)
        px = acc.get("proxy", proxy)
        proxy_url = _get_httpx_proxy_url(px)
        httpx_kwargs = {"proxy": proxy_url} if proxy_url else {}

        try:
            resp = httpx.get(
                ABOUT_ACCOUNT_URL,
                params=params,
                headers=_headers(c0),
                cookies=_cookies(at, c0),
                timeout=timeout,
                **httpx_kwargs,
            )
            if resp.status_code == 429:
                hit_429 = True
                logger.debug("AboutAccountQuery for @%s returned 429 — rotating account credential...", clean_username)
                time.sleep(0.3)
                continue
            if resp.status_code != 200:
                logger.debug("AboutAccountQuery for @%s returned HTTP %s", clean_username, resp.status_code)
                return None
            data = resp.json()
            country = (
                data.get("data", {})
                    .get("user_result_by_screen_name", {})
                    .get("result", {})
                    .get("about_profile", {})
                    .get("account_based_in")
            )
            res = str(country).strip() if country else None
            if res:
                _ACCOUNT_LOCATION_CACHE[clean_username] = res
                _save_location_to_db(clean_username, res)
            return res
        except Exception as exc:
            logger.debug("fetch_account_based_in failed for @%s: %s", clean_username, exc)
            continue

    if hit_429:
        return "RATE_LIMITED"

    return None


def get_user_lists(auth_token: str, ct0: str, proxy: Optional[str | dict] = None) -> list[dict]:
    """Fetch all lists owned by the account. Returns list of dicts: [{list_id, list_name, list_url}]."""
    try:
        proxy_url = _get_httpx_proxy_url(proxy)
        httpx_kwargs = {"proxy": proxy_url} if proxy_url else {}
        resp = httpx.get(
            GET_USER_LISTS_URL,
            headers=_headers(ct0),
            cookies=_cookies(auth_token, ct0),
            timeout=20,
            **httpx_kwargs,
        )
        if resp.status_code == 200:
            raw_lists = resp.json().get("lists", [])
            out = []
            for l in raw_lists:
                lid = str(l.get("id_str") or l.get("id") or "")
                lname = str(l.get("name") or "")
                if lid:
                    out.append({
                        "list_id": lid,
                        "list_name": lname,
                        "list_url": f"https://x.com/i/lists/{lid}",
                    })
            return out
    except Exception as exc:
        logger.warning("get_user_lists failed: %s", exc)
    return []


# ── List management ────────────────────────────────────────────────────────────
def create_list(auth_token: str, ct0: str, name: str, description: str = "", proxy: Optional[str | dict] = None) -> dict:
    """Create a public Twitter/X list. Returns {list_id, list_url, slug, owner}."""
    proxy_url = _get_httpx_proxy_url(proxy)
    httpx_kwargs = {"proxy": proxy_url} if proxy_url else {}
    resp = httpx.post(
        CREATE_LIST_URL,
        data={"name": name[:25], "mode": "public", "description": description[:100]},
        headers=_headers(ct0, {"Content-Type": "application/x-www-form-urlencoded"}),
        cookies=_cookies(auth_token, ct0),
        timeout=30,
        **httpx_kwargs,
    )
    resp.raise_for_status()
    data = resp.json()
    list_id = str(data["id"])
    slug = data.get("slug", "")
    owner = data.get("user", {}).get("screen_name", "")
    # Use numeric list_id URL (slug-based URLs may not be accessible)
    list_url = f"https://x.com/i/lists/{list_id}"
    return {
        "list_id": list_id,
        "list_url": list_url,
        "slug": slug,
        "owner": owner,
    }


def upload_media(auth_token: str, ct0: str, image_bytes: bytes, proxy: Optional[str | dict] = None) -> str:
    """
    Upload image bytes using the INIT/APPEND/FINALIZE chunked protocol.
    Returns media_id_string.
    """
    import random
    from string import ascii_letters

    CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB chunks
    total_bytes = len(image_bytes)
    cookies = _cookies(auth_token, ct0)
    headers = _headers(ct0)
    proxy_url = _get_httpx_proxy_url(proxy)
    httpx_kwargs = {"proxy": proxy_url} if proxy_url else {}

    # INIT
    init_resp = httpx.post(
        UPLOAD_MEDIA_URL,
        params={
            "command": "INIT",
            "media_type": "image/png",
            "total_bytes": total_bytes,
            "media_category": "tweet_image",
        },
        headers=headers,
        cookies=cookies,
        timeout=30,
        **httpx_kwargs,
    )
    init_resp.raise_for_status()
    media_id = init_resp.json()["media_id"]

    # APPEND — send in chunks
    offset = 0
    segment_index = 0
    while offset < total_bytes:
        chunk = image_bytes[offset: offset + CHUNK_SIZE]
        pad = bytes(
            "".join(random.choices(ascii_letters, k=16)), encoding="utf-8"
        )
        boundary = b"------WebKitFormBoundary" + pad
        body = (
            boundary + b"\r\n"
            + b'Content-Disposition: form-data; name="media"; filename="blob"\r\n'
            + b"Content-Type: application/octet-stream\r\n\r\n"
            + chunk
            + b"\r\n" + boundary + b"--\r\n"
        )
        append_headers = dict(headers)
        append_headers["content-type"] = (
            "multipart/form-data; boundary=----WebKitFormBoundary" + pad.decode()
        )
        httpx.post(
            UPLOAD_MEDIA_URL,
            params={"command": "APPEND", "media_id": media_id, "segment_index": segment_index},
            headers=append_headers,
            cookies=cookies,
            content=body,
            timeout=60,
            **httpx_kwargs,
        )
        offset += CHUNK_SIZE
        segment_index += 1

    # FINALIZE
    final_resp = httpx.post(
        UPLOAD_MEDIA_URL,
        params={"command": "FINALIZE", "media_id": media_id, "allow_async": "true"},
        headers=headers,
        cookies=cookies,
        timeout=30,
        **httpx_kwargs,
    )
    final_resp.raise_for_status()
    return str(media_id)


DEFAULT_GQL_VARIABLES = {
    'count': 1000,
    'withSafetyModeUserFields': True,
    'includePromotedContent': True,
    'withQuickPromoteEligibilityTweetFields': True,
    'withVoice': True,
    'withV2Timeline': True,
    'withDownvotePerspective': False,
    'withBirdwatchNotes': True,
    'withCommunity': True,
    'withSuperFollowsUserFields': True,
    'withReactionsMetadata': False,
    'withReactionsPerspective': False,
    'withSuperFollowsTweetFields': True,
    'isMetatagsQuery': False,
    'withReplays': True,
    'withClientEventToken': False,
    'withAttachments': True,
    'withConversationQueryHighlights': True,
    'withMessageQueryHighlights': True,
    'withMessages': True,
}

DEFAULT_GQL_FEATURES = {
    'c9s_tweet_anatomy_moderator_badge_enabled': True,
    'responsive_web_home_pinned_timelines_enabled': True,
    'blue_business_profile_image_shape_enabled': True,
    'creator_subscriptions_tweet_preview_api_enabled': True,
    'freedom_of_speech_not_reach_fetch_enabled': True,
    'graphql_is_translatable_rweb_tweet_is_translatable_enabled': True,
    'graphql_timeline_v2_bookmark_timeline': True,
    'hidden_profile_likes_enabled': True,
    'highlights_tweets_tab_ui_enabled': True,
    'interactive_text_enabled': True,
    'longform_notetweets_consumption_enabled': True,
    'longform_notetweets_inline_media_enabled': True,
    'longform_notetweets_rich_text_read_enabled': True,
    'longform_notetweets_richtext_consumption_enabled': True,
    'profile_foundations_tweet_stats_enabled': True,
    'profile_foundations_tweet_stats_tweet_frequency': True,
    'responsive_web_birdwatch_note_limit_enabled': True,
    'responsive_web_edit_tweet_api_enabled': True,
    'responsive_web_enhance_cards_enabled': False,
    'responsive_web_graphql_exclude_directive_enabled': True,
    'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
    'responsive_web_graphql_timeline_navigation_enabled': True,
    'responsive_web_media_download_video_enabled': False,
    'responsive_web_text_conversations_enabled': False,
    'responsive_web_twitter_article_data_v2_enabled': True,
    'responsive_web_twitter_article_tweet_consumption_enabled': False,
    'responsive_web_twitter_blue_verified_badge_is_enabled': True,
    'rweb_lists_timeline_redesign_enabled': True,
    'spaces_2022_h2_clipping': True,
    'spaces_2022_h2_spaces_communities': True,
    'standardized_nudges_misinfo': True,
    'subscriptions_verification_info_verified_since_enabled': True,
    'tweet_awards_web_tipping_enabled': False,
    'tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled': True,
    'tweetypie_unmention_optimization_enabled': True,
    'verified_phone_label_enabled': False,
    'vibe_api_enabled': True,
    'view_counts_everywhere_api_enabled': True,
}


def set_list_banner(auth_token: str, ct0: str, list_id: str, image_bytes: bytes, proxy: Optional[str | dict] = None) -> bool:
    """
    Upload image and set it as the Twitter/X list banner via the internal
    GraphQL EditListBanner mutation. Returns True on success.
    """
    try:
        media_id = upload_media(auth_token, ct0, image_bytes, proxy=proxy)
        variables = DEFAULT_GQL_VARIABLES.copy()
        variables.update({"listId": int(list_id), "mediaId": int(media_id)})
        payload = {
            "variables": variables,
            "features": DEFAULT_GQL_FEATURES,
            "queryId": EDIT_LIST_BANNER_QID,
        }
        proxy_url = _get_httpx_proxy_url(proxy)
        httpx_kwargs = {"proxy": proxy_url} if proxy_url else {}
        resp = httpx.post(
            f"{GQL_API}/{EDIT_LIST_BANNER_QID}/EditListBanner",
            json=payload,
            headers=_headers(ct0, {"Content-Type": "application/json"}),
            cookies=_cookies(auth_token, ct0),
            timeout=30,
            **httpx_kwargs,
        )
        if resp.status_code not in (200, 204):
            logger.warning(
                "EditListBanner returned %d: %s",
                resp.status_code, resp.text[:300],
            )
            return False
        data = resp.json()
        # GraphQL returns data.list when successful (even if backend soft-warns in errors)
        if data.get("data", {}).get("list"):
            return True
        if data.get("errors"):
            logger.warning("EditListBanner GQL errors: %s", data["errors"][:2])
            return False
        return True
    except Exception as exc:
        logger.warning("set_list_banner failed: %s", exc)
        return False


# ── Tweet posting ──────────────────────────────────────────────────────────────
TWEET_FEATURES = {
    "tweetypie_unmention_optimization_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_create_tweet_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "interactive_text_enabled": True,
    "responsive_web_text_conversations_enabled": False,
    "responsive_web_enhance_cards_enabled": False,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
}


def post_tweet(
    auth_token: str,
    ct0: str,
    text: str,
    media_id: Optional[str] = None,
    attachment_url: Optional[str] = None,
    proxy: Optional[str | dict] = None,
) -> dict:
    """
    Post a tweet with optional image and/or quote-tweet attachment.

    Args:
        auth_token:      Account auth token.
        ct0:             CSRF token.
        text:            Tweet body text.
        media_id:        Optional media_id_string from upload_media().
        attachment_url:  Optional URL of a tweet to quote. When set, every
                         post becomes a quote-tweet embedding that tweet.
        proxy:           Optional proxy (URL string or dict).

    Returns:
        dict with keys: tweet_id, tweet_url, error.
    """
    variables: dict = {
        "tweet_text": text,
        "dark_request": False,
        "semantic_annotation_ids": [],
    }
    # Only include media when we have a media_id — empty array causes 422
    if media_id:
        variables["media"] = {"media_ids": [media_id], "tagged_users": []}
    # Quote tweet: attach the anchor tweet URL
    if attachment_url:
        variables["attachment_url"] = attachment_url
    payload = {
        "variables": variables,
        "features": TWEET_FEATURES,
        "queryId": CREATE_TWEET_QUERY_ID,
    }
    try:
        proxy_url = _get_httpx_proxy_url(proxy)
        httpx_kwargs = {"proxy": proxy_url} if proxy_url else {}
        resp = httpx.post(
            CREATE_TWEET_URL,
            json=payload,
            headers=_headers(ct0, {"Content-Type": "application/json"}),
            cookies=_cookies(auth_token, ct0),
            timeout=30,
            **httpx_kwargs,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            first_err = data["errors"][0]
            err_msg = first_err.get("message", str(first_err))
            logger.warning("post_tweet GraphQL error: %s", err_msg)
            err_type = classify_account_error(err_msg, resp.status_code)
            return {"tweet_id": "", "tweet_url": "", "error": f"GraphQL Error: {err_msg}", "error_type": err_type, "status_code": resp.status_code}

        result = (
            data.get("data", {})
            .get("create_tweet", {})
            .get("tweet_results", {})
            .get("result", {})
        )
        tweet_id = result.get("rest_id", "")
        if not tweet_id:
            logger.warning("post_tweet response missing rest_id: %s", str(data)[:300])
            return {"tweet_id": "", "tweet_url": "", "error": "Post Verification Failed: No tweet ID returned by Twitter/X API", "error_type": "OTHER", "status_code": resp.status_code}

        screen_name = (
            result.get("core", {})
            .get("user_results", {})
            .get("result", {})
            .get("legacy", {})
            .get("screen_name", "")
        )
        if screen_name and screen_name.lower() != "unknown":
            tweet_url = f"https://x.com/{screen_name}/status/{tweet_id}"
        else:
            tweet_url = f"https://x.com/i/status/{tweet_id}"
        return {"tweet_id": tweet_id, "tweet_url": tweet_url, "error": None, "error_type": None, "status_code": 200}
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        err_text = exc.response.text[:300]
        logger.error("post_tweet HTTP error %s: %s", status_code, err_text)
        err_type = classify_account_error(exc, status_code)
        return {"tweet_id": "", "tweet_url": "", "error": f"HTTP {status_code}: {err_text}", "error_type": err_type, "status_code": status_code}
    except Exception as exc:
        logger.error("post_tweet failed: %s", exc)
        err_type = classify_account_error(exc)
        return {"tweet_id": "", "tweet_url": "", "error": str(exc), "error_type": err_type, "status_code": None}
