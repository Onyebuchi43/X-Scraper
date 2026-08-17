"""
app.py — Scweet Dashboard Flask backend.
Serves the dashboard UI and exposes REST API endpoints for:
  - Account management
  - Scraping (followers, search, profile tweets)
  - Image generation
  - Campaign setup & control
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import uuid
from typing import Any

from flask import (
    Flask, jsonify, redirect, render_template,
    request, send_file, send_from_directory, url_for
)
from werkzeug.utils import secure_filename

# ── Ensure parent dir (Scweet package) is on path ────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["UPLOAD_FOLDER"] = os.path.join(os.path.dirname(__file__), "uploads")
app.config["GENERATED_FOLDER"] = os.path.join(os.path.dirname(__file__), "generated_images")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024


@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/health")
@app.route("/api/health")
def health_check():
    return jsonify({
        "status": "healthy",
        "service": "X-Scraper 24/7 Engine",
        "time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    }), 200


for _d in [app.config["UPLOAD_FOLDER"], app.config["GENERATED_FOLDER"], "outputs"]:
    os.makedirs(_d, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── Dashboard SQLite ───────────────────────────────────────────────────────────
DASH_DB = os.path.join(os.path.dirname(__file__), "dashboard.db")


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DASH_DB, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    return conn


def _init_db() -> None:
    conn = _db()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            auth_token  TEXT NOT NULL,
            ct0         TEXT NOT NULL,
            proxy       TEXT,
            label       TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id          TEXT PRIMARY KEY,
            type        TEXT,
            status      TEXT DEFAULT 'pending',
            params      TEXT,
            result_file TEXT,
            log         TEXT DEFAULT '[]',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS campaigns (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT,
            status      TEXT DEFAULT 'idle',
            config      TEXT DEFAULT '{}',
            log         TEXT DEFAULT '[]',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS lists (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id  INTEGER,
            list_id     TEXT,
            list_url    TEXT,
            list_name   TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS campaign_tagged (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id  INTEGER NOT NULL,
            username     TEXT    NOT NULL,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS vps_settings (
            id           INTEGER PRIMARY KEY CHECK (id = 1),
            vps_url      TEXT,
            vps_api_key  TEXT,
            is_active    INTEGER DEFAULT 0,
            updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS account_locations (
            username    TEXT PRIMARY KEY,
            country     TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_camp_tagged ON campaign_tagged(campaign_id, username);
    """)
    try:
        conn.execute("ALTER TABLE lists ADD COLUMN post_count INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        conn.execute("DELETE FROM account_locations WHERE country IS NULL OR country = ''")
    except Exception:
        pass
    conn.commit()
    conn.close()


_init_db()


def _resume_running_campaigns():
    try:
        conn = _db()
        rows = conn.execute("SELECT id, config FROM campaigns WHERE status='running'").fetchall()
        conn.close()
        if not rows:
            return
        from dashboard.app import _get_scheduler_engine
        engine = _get_scheduler_engine()
        for r in rows:
            cid = r["id"]
            cfg = json.loads(r["config"] or "{}")
            account_ids = cfg.get("account_ids", [])
            conn2 = _db()
            accounts = []
            for aid in account_ids:
                a = conn2.execute("SELECT id, auth_token, ct0, proxy FROM accounts WHERE id=?", (aid,)).fetchone()
                if a:
                    accounts.append(dict(a))
            conn2.close()
            cfg["accounts"] = accounts
            engine.launch_campaign(cid, cfg)
            logger.info("Automatically resumed active campaign %d on app startup", cid)
    except Exception as exc:
        logger.error("Failed to auto-resume campaigns on startup: %s", exc)


_resume_running_campaigns()
_jobs: dict[str, threading.Thread] = {}


def _update_job(job_id: str, **kwargs):
    conn = _db()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [job_id]
    conn.execute(f"UPDATE jobs SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?", vals)
    conn.commit()
    conn.close()


def _append_job_log(job_id: str, msg: str):
    conn = _db()
    row = conn.execute("SELECT log FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row:
        log = json.loads(row["log"] or "[]")
        log.append({"ts": time.strftime("%H:%M:%S"), "msg": msg})
        conn.execute("UPDATE jobs SET log=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                     (json.dumps(log[-200:]), job_id))
        conn.commit()
    conn.close()


def _delete_account_from_db(account_id: int) -> None:
    try:
        conn = _db()
        conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        conn.commit()
        conn.close()
        logger.info("Automatically deleted blocked account %d from database", account_id)
    except Exception as exc:
        logger.error("Failed to delete account %d: %s", account_id, exc)


# ── ──────────────────────────────────────────────────────────────────────────
# Scraping job runner (background thread)
# ────────────────────────────────────────────────────────────────────────────
def _run_scrape_job(job_id: str, job_type: str, params: dict) -> None:
    try:
        from Scweet import Scweet, ScweetConfig  # type: ignore
        from dashboard.poster import classify_account_error  # type: ignore

        _update_job(job_id, status="running")
        _append_job_log(job_id, "Initializing Scweet…")

        # Build cookies list from selected account IDs
        account_ids: list[int] = params.get("account_ids", [])
        conn = _db()
        cookies_list = []
        for aid in account_ids:
            row = conn.execute("SELECT auth_token, ct0, proxy FROM accounts WHERE id=?", (aid,)).fetchone()
            if row:
                entry: dict[str, Any] = {"auth_token": row["auth_token"], "ct0": row["ct0"]}
                if row["proxy"]:
                    entry["proxy"] = row["proxy"]
                cookies_list.append(entry)
        conn.close()

        if not cookies_list:
            _update_job(job_id, status="error")
            _append_job_log(job_id, "ERROR: No valid accounts selected")
            return

        cfg = ScweetConfig(daily_requests_limit=10000, daily_tweets_limit=10000)
        s = Scweet(
            cookies=cookies_list if len(cookies_list) > 1 else cookies_list[0],
            config=cfg,
        )
        _append_job_log(job_id, f"Scweet initialized with {len(cookies_list)} account(s)")

        results = []
        save_name = f"job_{job_id[:8]}"

        if job_type == "followers":
            targets = params.get("targets", [])
            limit = int(params.get("limit", 100))
            country_filter = params.get("country_filter", "")
            min_followers = int(params.get("min_followers", 0))
            max_followers = int(params.get("max_followers", 1000))
            target_type = params.get("target_type", "followers")

            if target_type == "tweet_commenters":
                from scheduler_engine import _scrape_tweet_commenters
                _append_job_log(job_id, f"Scraping commenters of tweet/URL {targets} (limit={limit}, range: {min_followers}-{max_followers}, country: '{country_filter}')")
                tweet_target = targets[0] if isinstance(targets, list) and targets else str(targets)
                handles, ok, raw_count = _scrape_tweet_commenters(
                    tweet_target, cookies_list, limit, lambda level, msg: _append_job_log(job_id, f"[{level}] {msg}"),
                    min_followers=min_followers, max_followers=max_followers,
                    country_filter=country_filter
                )
                results = [{"username": h} for h in handles]

            elif target_type == "target_tweets_commenters":
                from scheduler_engine import _scrape_target_tweets_commenters
                _append_job_log(job_id, f"Scraping recent top tweets commenters of {targets} (limit={limit}, range: {min_followers}-{max_followers}, country: '{country_filter}')")
                handles, ok, raw_count = _scrape_target_tweets_commenters(
                    targets if isinstance(targets, list) else [str(targets)],
                    cookies_list, limit,
                    lambda level, msg: _append_job_log(job_id, f"[{level}] {msg}"),
                    min_followers=min_followers, max_followers=max_followers,
                    country_filter=country_filter
                )
                results = [{"username": h} for h in handles]

            else:
                _append_job_log(job_id, f"Scraping followers of {targets} (limit={limit}, range: {min_followers}-{max_followers})")
                results = s.get_followers(targets, limit=limit, save=True, save_name=save_name)

                if country_filter and results:
                    import time as _time
                    from poster import fetch_account_based_in  # type: ignore
                    country_keywords = [c.strip().lower() for c in country_filter.split(",") if c.strip()]
                    _append_job_log(job_id, f"Country filter '{country_filter}' — fetching 'Account based in' for {len(results)} profiles via AboutAccountQuery...")
                    scrape_cookies = cookies_list[0] if cookies_list else {}
                    scrape_auth  = scrape_cookies.get("auth_token", "")
                    scrape_ct0   = scrape_cookies.get("ct0", "")
                    scrape_proxy = scrape_cookies.get("proxy")
                    filtered = []
                    for r in results:
                        username = ""
                        if isinstance(r, dict):
                            username = (r.get("username") or r.get("screen_name") or "").strip().lstrip("@")
                        elif isinstance(r, str):
                            username = r.strip().lstrip("@")
                        if not username:
                            continue
                        account_country = fetch_account_based_in(scrape_auth, scrape_ct0, username, proxy=scrape_proxy)
                        if account_country and any(ck in account_country.lower() for ck in country_keywords):
                            filtered.append(r)
                        _time.sleep(0.3)
                    _append_job_log(job_id, f"Filtered by 'Account based in' '{country_filter}': {len(filtered)} / {len(results)} matches")
                    results = filtered

        elif job_type == "search":
            _append_job_log(job_id, "Running tweet search…")
            results = s.search(
                params.get("query", ""),
                since=params.get("since") or None,
                until=params.get("until") or None,
                from_users=params.get("from_users") or None,
                lang=params.get("lang") or None,
                min_likes=int(params["min_likes"]) if params.get("min_likes") else None,
                min_retweets=int(params["min_retweets"]) if params.get("min_retweets") else None,
                has_images=params.get("has_images") or None,
                display_type=params.get("display_type", "Top"),
                limit=int(params.get("limit", 100)),
                save=True,
                save_name=save_name,
            )

        elif job_type == "profile":
            targets = params.get("targets", [])
            limit = int(params.get("limit", 100))
            _append_job_log(job_id, f"Fetching profile tweets for {targets}")
            results = s.get_profile_tweets(targets, limit=limit, save=True, save_name=save_name)

        result_file = os.path.join("outputs", f"{save_name}.csv")
        _update_job(job_id, status="done", result_file=result_file)
        _append_job_log(job_id, f"Done — {len(results)} results → {result_file}")

    except Exception as exc:
        logger.exception("Scrape job %s failed", job_id)
        from dashboard.poster import classify_account_error  # type: ignore
        err_type = classify_account_error(exc)
        
        if err_type == "BLOCKED":
            account_ids = params.get("account_ids", [])
            for aid in account_ids:
                _delete_account_from_db(aid)
                _append_job_log(job_id, f"🚨 Account #{aid} was BLOCKED/SUSPENDED. Automatically removed from database!")
        elif err_type == "RATE_LIMIT":
            _append_job_log(job_id, "⏳ Scraping hit a RATE LIMIT / RESTRICTION. Leaving account to cool down.")

        _update_job(job_id, status="error")
        _append_job_log(job_id, f"ERROR: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


# ── Accounts ───────────────────────────────────────────────────────────────────
@app.route("/api/accounts", methods=["GET"])
def list_accounts():
    conn = _db()
    rows = conn.execute("SELECT id, label, ct0, proxy, created_at, substr(auth_token,1,10)||'…' as token_preview FROM accounts ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/accounts", methods=["POST"])
def add_account():
    data = request.json or {}
    auth_token = (data.get("auth_token") or "").strip()
    ct0 = (data.get("ct0") or "").strip()
    proxy = (data.get("proxy") or "").strip() or None
    label = (data.get("label") or "").strip() or None
    if not auth_token or not ct0:
        return jsonify({"error": "auth_token and ct0 are required"}), 400
    conn = _db()
    cur = conn.execute(
        "INSERT INTO accounts (auth_token, ct0, proxy, label) VALUES (?,?,?,?)",
        (auth_token, ct0, proxy, label),
    )
    conn.commit()
    account_id = cur.lastrowid
    conn.close()
    return jsonify({"id": account_id, "msg": "Account added"})


@app.route("/api/accounts/<int:account_id>", methods=["GET", "PUT", "DELETE"])
def handle_account(account_id: int):
    if request.method == "GET":
        conn = _db()
        row = conn.execute("SELECT id, auth_token, ct0, proxy, label, created_at FROM accounts WHERE id=?", (account_id,)).fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "Account not found"}), 404
        return jsonify(dict(row))

    elif request.method == "PUT":
        data = request.json or {}
        auth_token = (data.get("auth_token") or "").strip()
        ct0 = (data.get("ct0") or "").strip()
        proxy = (data.get("proxy") or "").strip() or None
        label = (data.get("label") or "").strip() or None

        conn = _db()
        existing = conn.execute("SELECT auth_token, ct0, proxy, label FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not existing:
            conn.close()
            return jsonify({"error": "Account not found"}), 404

        final_auth = auth_token if auth_token else existing["auth_token"]
        final_ct0 = ct0 if ct0 else existing["ct0"]
        final_proxy = proxy if "proxy" in data else existing["proxy"]
        final_label = label if "label" in data else existing["label"]

        if not final_auth or not final_ct0:
            conn.close()
            return jsonify({"error": "auth_token and ct0 are required"}), 400

        conn.execute(
            "UPDATE accounts SET auth_token=?, ct0=?, proxy=?, label=? WHERE id=?",
            (final_auth, final_ct0, final_proxy, final_label, account_id),
        )
        conn.commit()
        conn.close()
        return jsonify({"msg": "Account updated successfully"})

    elif request.method == "DELETE":
        conn = _db()
        conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        conn.commit()
        conn.close()
        return jsonify({"msg": "Deleted"})


# ── Scrape jobs ────────────────────────────────────────────────────────────────
def _start_job(job_type: str, params: dict) -> str:
    job_id = str(uuid.uuid4())
    conn = _db()
    conn.execute(
        "INSERT INTO jobs (id, type, params) VALUES (?,?,?)",
        (job_id, job_type, json.dumps(params)),
    )
    conn.commit()
    conn.close()
    t = threading.Thread(target=_run_scrape_job, args=(job_id, job_type, params), daemon=True)
    _jobs[job_id] = t
    t.start()
    return job_id


@app.route("/api/scrape/followers", methods=["POST"])
def scrape_followers():
    data = request.json or {}
    targets = [t.strip().lstrip("@") for t in (data.get("targets") or "").split(",") if t.strip()]
    if not targets:
        return jsonify({"error": "targets required"}), 400
    params = {
        "targets": targets,
        "limit": data.get("limit", 100),
        "account_ids": data.get("account_ids", []),
        "min_followers": data.get("min_followers", 0),
        "max_followers": data.get("max_followers", 1000),
    }
    job_id = _start_job("followers", params)
    return jsonify({"job_id": job_id})


@app.route("/api/scrape/search", methods=["POST"])
def scrape_search():
    data = request.json or {}
    params = {
        "query": data.get("query", ""),
        "since": data.get("since", ""),
        "until": data.get("until", ""),
        "from_users": [u.strip().lstrip("@") for u in (data.get("from_users") or "").split(",") if u.strip()],
        "lang": data.get("lang", ""),
        "min_likes": data.get("min_likes", ""),
        "min_retweets": data.get("min_retweets", ""),
        "has_images": data.get("has_images", False),
        "display_type": data.get("display_type", "Top"),
        "limit": data.get("limit", 100),
        "account_ids": data.get("account_ids", []),
    }
    job_id = _start_job("search", params)
    return jsonify({"job_id": job_id})


@app.route("/api/scrape/profile", methods=["POST"])
def scrape_profile():
    data = request.json or {}
    targets = [t.strip().lstrip("@") for t in (data.get("targets") or "").split(",") if t.strip()]
    params = {
        "targets": targets,
        "limit": data.get("limit", 100),
        "account_ids": data.get("account_ids", []),
    }
    job_id = _start_job("profile", params)
    return jsonify({"job_id": job_id})


# ── Jobs ───────────────────────────────────────────────────────────────────────
@app.route("/api/jobs")
def list_jobs():
    conn = _db()
    rows = conn.execute(
        "SELECT id, type, status, params, result_file, created_at, updated_at FROM jobs ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        # Extract human-readable label from params
        try:
            p = json.loads(d.get("params") or "{}")
            targets = p.get("targets") or p.get("from_users") or []
            query   = p.get("query", "")
            if targets:
                d["label"] = ", ".join(f"@{t}" for t in targets[:3])
            elif query:
                d["label"] = query[:40]
            else:
                d["label"] = d["type"]
        except Exception:
            d["label"] = d["type"]
        del d["params"]
        result.append(d)
    return jsonify(result)


@app.route("/api/jobs/all", methods=["DELETE"])
def delete_all_jobs():
    conn = _db()
    rows = conn.execute("SELECT result_file FROM jobs WHERE result_file IS NOT NULL").fetchall()
    for row in rows:
        path = row["result_file"]
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except Exception:
                pass
    conn.execute("DELETE FROM jobs")
    conn.commit()
    conn.close()
    return jsonify({"msg": "All jobs deleted"})


@app.route("/api/jobs/<job_id>", methods=["GET", "DELETE"])
def job_status(job_id: str):
    conn = _db()
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404

    if request.method == "DELETE":
        result_file = row["result_file"]
        if result_file and os.path.isfile(result_file):
            try:
                os.remove(result_file)
            except Exception:
                pass
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        conn.commit()
        conn.close()
        return jsonify({"msg": "Job deleted"})

    # GET
    conn.close()
    d = dict(row)
    d["log"] = json.loads(d.get("log") or "[]")
    return jsonify(d)


@app.route("/api/jobs/<job_id>/download")
def download_job(job_id: str):
    conn = _db()
    row = conn.execute("SELECT result_file FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row or not row["result_file"]:
        return jsonify({"error": "No file"}), 404
    path = row["result_file"]
    if not os.path.isfile(path):
        return jsonify({"error": "File not found on disk"}), 404
    return send_file(os.path.abspath(path), as_attachment=True)


# ── CSV files listing ──────────────────────────────────────────────────────────
@app.route("/api/csv-files")
def csv_files():
    files = []
    for folder in ["outputs", app.config["UPLOAD_FOLDER"]]:
        if os.path.isdir(folder):
            for f in os.listdir(folder):
                if f.endswith(".csv"):
                    full = os.path.join(folder, f)
                    files.append({
                        "name": f,
                        "path": full,
                        "size_kb": round(os.path.getsize(full) / 1024, 1),
                        "folder": folder,
                    })
    return jsonify(files)


@app.route("/api/upload-csv", methods=["POST"])
def upload_csv():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    filename = secure_filename(f.filename)
    dest = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    f.save(dest)
    return jsonify({"path": dest, "name": filename})


# ── Image generation (local template — no Twitter API needed) ─────────────────
@app.route("/api/image/generate-card", methods=["POST"])
def generate_card():
    """
    Generate a tweet-card image locally from typed fields & optional avatar picture.
    Supports multipart/form-data or application/json.
    """
    if request.is_json:
        data = request.json or {}
        avatar_bytes = None
    else:
        data = request.form.to_dict()
        avatar_file = request.files.get("avatar")
        avatar_bytes = avatar_file.read() if avatar_file else None

    display_name = (data.get("display_name") or data.get("name") or "").strip()
    username     = (data.get("username") or "").strip().lstrip("@")
    body_text    = (data.get("body_text") or data.get("body") or "").strip()

    if not display_name:
        return jsonify({"error": "display_name (Name) is required"}), 400
    if not body_text:
        return jsonify({"error": "body_text (Body) is required"}), 400
    if not username:
        username = display_name.lower().replace(" ", "")

    try:
        import image_editor  # type: ignore

        img_bytes = image_editor.generate_tweet_card_screenshot(
            name          = display_name,
            username      = username,
            body_text     = body_text,
            avatar_bytes  = avatar_bytes,
            timestamp     = (data.get("timestamp") or "3:51 PM · 8/4/26").strip(),
            views         = str(data.get("views") or "3M"),
            replies       = str(data.get("replies") or "2.8K"),
            retweets      = str(data.get("retweets") or "4.2K"),
            likes         = str(data.get("likes") or "54K"),
            bookmarks     = str(data.get("bookmarks") or "1.4K"),
        )

        filename = f"card_{username}_{int(time.time())}.png"
        out_path = os.path.join(app.config["GENERATED_FOLDER"], filename)
        with open(out_path, "wb") as fp:
            fp.write(img_bytes)

        return jsonify({
            "filename": filename,
            "preview_url": f"/api/image/preview/{filename}",
        })
    except Exception as exc:
        logger.exception("Card generation failed")
        return jsonify({"error": str(exc)}), 500


# Keep old route for backward compat (falls back to local card)
@app.route("/api/image/generate", methods=["POST"])
def generate_image():
    data = request.json or {}
    # If manual card fields are supplied use them directly
    if data.get("display_name"):
        return generate_card()
    # Fallback: use handle as display name with placeholder body
    handle = (data.get("handle") or "user").strip().lstrip("@")
    image_text = (data.get("image_text") or "Official announcement.").strip()
    try:
        import image_editor  # type: ignore
        img_bytes = image_editor.generate_tweet_card(
            display_name=handle.title(),
            username=handle,
            body_text=image_text,
        )
        filename = f"card_{handle}_{int(time.time())}.png"
        out_path = os.path.join(app.config["GENERATED_FOLDER"], filename)
        with open(out_path, "wb") as fp:
            fp.write(img_bytes)
        return jsonify({"filename": filename, "preview_url": f"/api/image/preview/{filename}"})
    except Exception as exc:
        logger.exception("Image generation failed")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/image/preview/<filename>")
def image_preview(filename: str):
    return send_from_directory(app.config["GENERATED_FOLDER"], filename)


# ── Campaigns ──────────────────────────────────────────────────────────────────
@app.route("/api/campaigns", methods=["GET"])
def list_campaigns():
    conn = _db()
    rows = conn.execute(
        "SELECT id, name, status, created_at, updated_at FROM campaigns ORDER BY id DESC LIMIT 50"
    ).fetchall()
    result = [dict(r) for r in rows]
    conn.close()
    return jsonify(result)


@app.route("/api/campaigns", methods=["POST"])
def create_campaign():
    data = request.json or {}
    name = (data.get("name") or "Campaign").strip()
    config = data.get("config", {})
    conn = _db()
    cur = conn.execute(
        "INSERT INTO campaigns (name, config) VALUES (?,?)",
        (name, json.dumps(config)),
    )
    conn.commit()
    cid = cur.lastrowid
    conn.close()
    return jsonify({"id": cid, "msg": "Campaign created"})


@app.route("/api/campaigns/all", methods=["DELETE"])
def delete_all_campaigns():
    try:
        import scheduler_engine  # type: ignore
        scheduler_engine.stop_all_campaigns()
    except Exception as exc:
        logger.error("stop_all_campaigns error: %s", exc)
    conn = _db()
    conn.execute("DELETE FROM campaign_tagged")
    conn.execute("DELETE FROM campaigns")
    conn.commit()
    conn.close()
    return jsonify({"msg": "All campaigns deleted"})


def _get_scheduler_engine():
    try:
        import scheduler_engine
        return scheduler_engine
    except ImportError:
        from dashboard import scheduler_engine  # type: ignore
        return scheduler_engine


@app.route("/api/campaigns/<int:cid>", methods=["GET", "PUT", "DELETE"])
def get_campaign(cid: int):
    conn = _db()
    row = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404

    if request.method == "DELETE":
        conn.close()
        try:
            _get_scheduler_engine().stop_campaign(cid)
        except Exception:
            pass
        conn2 = _db()
        conn2.execute("DELETE FROM campaign_tagged WHERE campaign_id=?", (cid,))
        conn2.execute("DELETE FROM campaigns WHERE id=?", (cid,))
        conn2.commit()
        conn2.close()
        return jsonify({"msg": f"Campaign {cid} deleted"})

    if request.method == "PUT":
        data = request.json or {}
        name = (data.get("name") or row["name"]).strip()
        new_config = data.get("config")
        if new_config is None:
            new_config = json.loads(row["config"] or "{}")
        conn.execute(
            "UPDATE campaigns SET name=?, config=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (name, json.dumps(new_config), cid),
        )
        conn.commit()
        conn.close()
        return jsonify({"id": cid, "msg": "Campaign updated"})

    # GET
    d = dict(row)
    conn.close()
    d["log"] = json.loads(d.get("log") or "[]")
    d["config"] = json.loads(d.get("config") or "{}")
    return jsonify(d)


@app.route("/api/campaigns/<int:cid>/start", methods=["POST"])
@app.route("/api/campaigns/<int:cid>/resume", methods=["POST"])
def start_campaign(cid: int):
    conn = _db()
    engine = _get_scheduler_engine()

    # Enforce maximum 3 active concurrent campaigns
    active_count = conn.execute("SELECT COUNT(*) FROM campaigns WHERE status='running' AND id != ?", (cid,)).fetchone()[0]
    if active_count >= 3:
        conn.close()
        return jsonify({
            "error": "Maximum limit of 3 concurrent active campaigns reached. Please stop an active campaign before starting a new one."
        }), 400

    row = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404

    # Immediately mark campaign as 'running' in database
    conn.execute("UPDATE campaigns SET status='running', updated_at=CURRENT_TIMESTAMP WHERE id=?", (cid,))
    conn.commit()
    conn.close()

    config = json.loads(row["config"] or "{}")

    # Attach full account credentials
    account_ids: list[int] = config.get("account_ids", [])
    conn = _db()
    accounts = []
    for aid in account_ids:
        a = conn.execute("SELECT id, auth_token, ct0, proxy FROM accounts WHERE id=?", (aid,)).fetchone()
        if a:
            accounts.append(dict(a))
    conn.close()
    config["accounts"] = accounts

    engine.launch_campaign(cid, config)

    return jsonify({"msg": "Campaign started"})


@app.route("/api/campaigns/<int:cid>/stop", methods=["POST"])
def stop_campaign(cid: int):
    ok = _get_scheduler_engine().stop_campaign(cid)
    conn = _db()
    conn.execute("UPDATE campaigns SET status='stopped', updated_at=CURRENT_TIMESTAMP WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"msg": "Stop signal sent" if ok else "Campaign not running"})


@app.route("/api/campaigns/<int:cid>/log")
def campaign_log(cid: int):
    conn = _db()
    row = conn.execute("SELECT status, log FROM campaigns WHERE id=?", (cid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404

    return jsonify({
        "status": row["status"],
        "log": json.loads(row["log"] or "[]"),
    })


# ── Lists ──────────────────────────────────────────────────────────────────────
@app.route("/api/lists")
def list_lists():
    conn = _db()
    rows = conn.execute("SELECT * FROM lists ORDER BY created_at DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ── Campaign deduplication (tagged usernames) ──────────────────────────────────
@app.route("/api/campaigns/<int:cid>/tagged-count")
def campaign_tagged_count(cid: int):
    conn = _db()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM campaign_tagged WHERE campaign_id=?", (cid,)
    ).fetchone()
    conn.close()
    return jsonify({"campaign_id": cid, "tagged_count": row["cnt"] if row else 0})


@app.route("/api/campaigns/<int:cid>/tagged", methods=["DELETE"])
def clear_campaign_tagged(cid: int):
    conn = _db()
    conn.execute("DELETE FROM campaign_tagged WHERE campaign_id=?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"msg": f"Cleared tagged usernames for campaign {cid}"})


@app.route("/api/campaigns/tagged/all", methods=["DELETE"])
def clear_all_tagged():
    conn = _db()
    conn.execute("DELETE FROM campaign_tagged")
    conn.commit()
    conn.close()
    return jsonify({"msg": "Cleared all tagged usernames across all campaigns"})


@app.route("/api/vps/config", methods=["GET", "POST"])
def vps_config():
    conn = _db()
    if request.method == "POST":
        data = request.json or {}
        vps_url = (data.get("vps_url") or "").strip()
        vps_api_key = (data.get("vps_api_key") or "").strip()
        conn.execute(
            """INSERT INTO vps_settings (id, vps_url, vps_api_key, is_active)
               VALUES (1, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET vps_url=excluded.vps_url, vps_api_key=excluded.vps_api_key, is_active=excluded.is_active""",
            (vps_url, vps_api_key, 1 if vps_url else 0),
        )
        conn.commit()

    row = conn.execute("SELECT * FROM vps_settings WHERE id=1").fetchone()
    conn.close()

    vps_url = row["vps_url"] if row else ""
    vps_api_key = row["vps_api_key"] if row else ""
    is_connected = bool(vps_url and len(vps_url) > 5)

    return jsonify({
        "vps_url": vps_url,
        "vps_api_key": vps_api_key,
        "is_connected": is_connected,
        "status_label": f"🟢 Connected to {vps_url}" if is_connected else "🔴 VPS Not Connected (Local Engine Active)",
    })


@app.route("/api/vps/status")
def vps_status():
    import platform
    conn = _db()
    active_camps = conn.execute("SELECT COUNT(*) as cnt FROM campaigns WHERE status='running'").fetchone()["cnt"]
    conn.close()

    return jsonify({
        "node_type": "24/7 Cloud Node",
        "database": "SQLite (dashboard.db)",
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "python_version": platform.python_version(),
        "active_campaigns": active_camps,
    })


# ── Proxy Management Endpoints ────────────────────────────────────────────────
@app.route("/api/proxy/settings", methods=["GET", "POST"])
def proxy_settings_api():
    try:
        from betasocks_client import get_proxy_settings, update_proxy_settings
    except ImportError:
        from dashboard.betasocks_client import get_proxy_settings, update_proxy_settings  # type: ignore

    if request.method == "POST":
        data = request.json or {}
        email = data.get("betasocks_email", "")
        password = data.get("betasocks_password", "")
        daily_limit = int(data.get("daily_limit", 50))
        update_proxy_settings(email, password, daily_limit)
        return jsonify({"msg": "Proxy settings updated successfully!"})

    return jsonify(get_proxy_settings())


@app.route("/api/proxy/test", methods=["POST"])
def proxy_test_api():
    data = request.json or {}
    email = data.get("betasocks_email", "")
    password = data.get("betasocks_password", "")
    try:
        from betasocks_client import test_betasocks_credentials
    except ImportError:
        from dashboard.betasocks_client import test_betasocks_credentials  # type: ignore

    res = test_betasocks_credentials(email, password)
    return jsonify(res)


@app.route("/api/proxy/fetch", methods=["POST"])
def proxy_fetch_api():
    data = request.json or {}
    country = data.get("country", "usa")
    limit = int(data.get("limit", 5))
    try:
        from betasocks_client import BetaSocksClient
    except ImportError:
        from dashboard.betasocks_client import BetaSocksClient  # type: ignore

    client = BetaSocksClient()
    proxies = client.fetch_available_proxies(country=country, limit=limit)
    return jsonify({"proxies": proxies, "count": len(proxies)})


# ── Account Creator & Bulk Importer Endpoints ───────────────────────────────
@app.route("/api/accounts/create", methods=["POST"])
def create_account_api():
    data = request.form if request.form else (request.json or {})
    name = (data.get("name") or "").strip()
    username = (data.get("username") or "").strip()
    auth_token = (data.get("auth_token") or "").strip()
    ct0 = (data.get("ct0") or "").strip()
    description = (data.get("description") or "").strip()
    location = (data.get("location") or "").strip()
    url = (data.get("url") or "").strip()

    avatar_file = request.files.get("avatar")
    banner_file = request.files.get("banner")

    avatar_bytes = avatar_file.read() if avatar_file else None
    banner_bytes = banner_file.read() if banner_file else None

    if not name and not username:
        return jsonify({"error": "Display Name or Username is required"}), 400

    if not name:
        name = username

    try:
        from account_creator import execute_automated_account_creation
    except ImportError:
        from dashboard.account_creator import execute_automated_account_creation  # type: ignore

    quantity = int(data.get("quantity", 1))

    res = execute_automated_account_creation(
        name=name,
        description=description,
        location=location,
        url=url,
        avatar_bytes=avatar_bytes,
        banner_bytes=banner_bytes,
        quantity=quantity,
        username=username if username else None,
        auth_token=auth_token if auth_token else None,
        ct0=ct0 if ct0 else None,
    )
    return jsonify(res)


@app.route("/api/accounts/bulk-import", methods=["POST"])
def bulk_import_accounts_api():
    data = request.json or {}
    raw_text = data.get("text", "").strip()
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]

    imported = 0
    errors = []

    try:
        from account_creator import register_email_account
    except ImportError:
        from dashboard.account_creator import register_email_account  # type: ignore

    for idx, line in enumerate(lines, 1):
        parts = line.split(":")
        if len(parts) >= 2:
            auth_token = parts[0].strip()
            ct0 = parts[1].strip()
            username = parts[2].strip() if len(parts) > 2 else f"user_{uuid.uuid4().hex[:6]}"
            proxy = parts[3].strip() if len(parts) > 3 else None
            try:
                register_email_account(
                    email="", name=username, password="",
                    auth_token=auth_token, ct0=ct0, proxy=proxy, username=username
                )
                imported += 1
            except Exception as e:
                errors.append(f"Line {idx}: {e}")
        else:
            errors.append(f"Line {idx}: Invalid format (expected auth_token:ct0)")

    return jsonify({"imported": imported, "total": len(lines), "errors": errors})


# ── Bulk Profile Editor Endpoint ─────────────────────────────────────────────
@app.route("/api/accounts/bulk-edit", methods=["POST"])
def bulk_edit_profiles_api():
    data = request.form if request.form else (request.json or {})
    account_ids_raw = data.get("account_ids", "[]")
    if isinstance(account_ids_raw, str):
        try:
            account_ids = json.loads(account_ids_raw)
        except Exception:
            account_ids = []
    else:
        account_ids = account_ids_raw

    name = data.get("name")
    description = data.get("description")
    location = data.get("location")
    url = data.get("url")

    avatar_file = request.files.get("avatar")
    banner_file = request.files.get("banner")

    avatar_bytes = avatar_file.read() if avatar_file else None
    banner_bytes = banner_file.read() if banner_file else None

    if not account_ids:
        return jsonify({"error": "No accounts selected for bulk update"}), 400

    conn = _db()
    accounts = []
    for aid in account_ids:
        a = conn.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
        if a: accounts.append(dict(a))
    conn.close()

    try:
        from poster import update_profile_text, update_profile_image, update_profile_banner
    except ImportError:
        from dashboard.poster import update_profile_text, update_profile_image, update_profile_banner  # type: ignore

    updated_count = 0
    results = []

    for acc in accounts:
        at = acc.get("auth_token", "")
        c0 = acc.get("ct0", "")
        px = acc.get("proxy")
        uname = acc.get("username", "")

        ok_text = update_profile_text(at, c0, name=name, description=description, location=location, url=url, proxy=px) if (name or description or location or url) else True
        ok_img = update_profile_image(at, c0, avatar_bytes, proxy=px) if avatar_bytes else True
        ok_bnr = update_profile_banner(at, c0, banner_bytes, proxy=px) if banner_bytes else True

        if ok_text and ok_img and ok_bnr:
            updated_count += 1
            results.append({"username": uname, "status": "success"})
        else:
            results.append({"username": uname, "status": "failed"})

        time.sleep(2)  # Safe delay between bulk profile edits

    return jsonify({"updated": updated_count, "total": len(accounts), "results": results})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
