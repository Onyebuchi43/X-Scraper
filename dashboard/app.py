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
        CREATE UNIQUE INDEX IF NOT EXISTS idx_camp_tagged ON campaign_tagged(campaign_id, username);
    """)
    conn.commit()
    conn.close()


_init_db()

# ── Background job registry ────────────────────────────────────────────────────
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
            _append_job_log(job_id, f"Scraping followers of {targets} (limit={limit})")
            results = s.get_followers(targets, limit=limit, save=True, save_name=save_name)

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


@app.route("/api/accounts/<int:account_id>", methods=["DELETE"])
def delete_account(account_id: int):
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
        from dashboard import image_editor  # type: ignore

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
        from dashboard import image_editor  # type: ignore
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
        "SELECT id, name, status, created_at, updated_at FROM campaigns ORDER BY id DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


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
        from dashboard import scheduler_engine  # type: ignore
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
    conn.close()
    d = dict(row)
    d["log"] = json.loads(d.get("log") or "[]")
    d["config"] = json.loads(d.get("config") or "{}")
    return jsonify(d)


@app.route("/api/campaigns/<int:cid>/start", methods=["POST"])
@app.route("/api/campaigns/<int:cid>/resume", methods=["POST"])
def start_campaign(cid: int):
    conn = _db()
    row = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404

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

    _get_scheduler_engine().launch_campaign(cid, config)

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
    import socket
    from dashboard import scheduler_engine  # type: ignore

    hostname = socket.gethostname()
    system_os = f"{platform.system()} {platform.release()}"
    python_ver = platform.python_version()
    db_url = os.environ.get("DATABASE_URL")
    db_backend = "PostgreSQL (Production Cloud DB)" if db_url else "SQLite (Local System File)"

    is_vps = os.environ.get("IS_VPS", "false").lower() in ("true", "1", "yes") or not (
        hostname.startswith("DESKTOP") or hostname.startswith("LAPTOP") or "win" in system_os.lower()
    )

    conn = _db()
    vps_row = conn.execute("SELECT * FROM vps_settings WHERE id=1").fetchone()
    conn.close()

    vps_url = vps_row["vps_url"] if vps_row else ""
    has_remote_vps = bool(vps_url and len(vps_url) > 5)

    active_campaign_count = 0
    try:
        with scheduler_engine._lock:
            active_campaign_count = len(scheduler_engine._campaigns)
    except Exception:
        pass

    return jsonify({
        "status": "online",
        "hostname": hostname,
        "os": system_os,
        "python_version": python_ver,
        "database": db_backend,
        "is_vps": is_vps,
        "has_remote_vps": has_remote_vps,
        "remote_vps_url": vps_url,
        "node_type": "24/7 Cloud VPS Server" if is_vps else "Local Development Instance",
        "active_campaigns": active_campaign_count,
        "time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    })


# ── Run ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n" + "=" * 60)
    print(f"  Scweet Dashboard  ->  http://localhost:{port}")
    print("=" * 60 + "\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
