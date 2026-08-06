from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, abort, flash, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "bbs.db"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_UPLOAD = 5 * 1024 * 1024

app = Flask(__name__)
app.secret_key = os.environ.get("BBS_SECRET_KEY", secrets.token_hex(24))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                bumped_at TEXT NOT NULL,
                views INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id INTEGER NOT NULL,
                body TEXT NOT NULL,
                author_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                image_name TEXT,
                likes INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE CASCADE
            );
            """
        )


def now_text() -> str:
    return datetime.now().strftime("%Y/%m/%d %H:%M:%S")


def anon_id() -> str:
    raw = f"{request.headers.get('X-Forwarded-For', request.remote_addr)}|{datetime.now():%Y-%m-%d}|{app.secret_key}".encode()
    return hashlib.sha256(raw).hexdigest()[:7]


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_upload() -> str | None:
    image = request.files.get("image")
    if not image or not image.filename:
        return None
    if not allowed_file(image.filename):
        raise ValueError("画像は PNG/JPG/GIF/WebP のみ対応しています。")
    ext = secure_filename(image.filename).rsplit(".", 1)[1].lower()
    name = f"{secrets.token_hex(12)}.{ext}"
    image.save(UPLOAD_DIR / name)
    return name


@app.context_processor
def inject_helpers() -> dict[str, Any]:
    return {"site_name": os.environ.get("BBS_SITE_NAME", "キメキメBBS")}


@app.get("/")
def index():
    query = request.args.get("q", "").strip()
    sort = request.args.get("sort", "updated")
    order_map = {
        "new": "t.created_at DESC",
        "popular": "post_count DESC, t.views DESC",
        "updated": "t.bumped_at DESC",
    }
    order = order_map.get(sort, order_map["updated"])
    sql = """
        SELECT t.*, COUNT(p.id) AS post_count
        FROM threads t
        LEFT JOIN posts p ON p.thread_id = t.id
    """
    params: list[Any] = []
    if query:
        sql += " WHERE t.title LIKE ? OR EXISTS (SELECT 1 FROM posts px WHERE px.thread_id=t.id AND px.body LIKE ?)"
        params.extend([f"%{query}%", f"%{query}%"])
    sql += f" GROUP BY t.id ORDER BY {order}"
    with connect_db() as conn:
        threads = conn.execute(sql, params).fetchall()
    return render_template("index.html", threads=threads, query=query, sort=sort)


@app.route("/thread/new", methods=["GET", "POST"])
def new_thread():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        if not title or not body:
            flash("タイトルと本文を入力してください。", "error")
            return render_template("new_thread.html", title=title, body=body)
        if len(title) > 120 or len(body) > 5000:
            flash("入力が長すぎます。", "error")
            return render_template("new_thread.html", title=title, body=body)
        try:
            image_name = save_upload()
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("new_thread.html", title=title, body=body)
        created = now_text()
        with connect_db() as conn:
            cur = conn.execute(
                "INSERT INTO threads(title, created_at, bumped_at) VALUES (?, ?, ?)",
                (title, created, created),
            )
            thread_id = cur.lastrowid
            conn.execute(
                "INSERT INTO posts(thread_id, body, author_id, created_at, image_name) VALUES (?, ?, ?, ?, ?)",
                (thread_id, body, anon_id(), created, image_name),
            )
        return redirect(url_for("thread", thread_id=thread_id))
    return render_template("new_thread.html")


@app.get("/thread/<int:thread_id>")
def thread(thread_id: int):
    with connect_db() as conn:
        conn.execute("UPDATE threads SET views = views + 1 WHERE id = ?", (thread_id,))
        thread_row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        if not thread_row:
            abort(404)
        posts = conn.execute("SELECT * FROM posts WHERE thread_id = ? ORDER BY id", (thread_id,)).fetchall()
        side_threads = conn.execute(
            """
            SELECT t.*, COUNT(p.id) post_count
            FROM threads t LEFT JOIN posts p ON p.thread_id=t.id
            GROUP BY t.id ORDER BY t.bumped_at DESC LIMIT 12
            """
        ).fetchall()
    return render_template("thread.html", thread=thread_row, posts=posts, side_threads=side_threads)


@app.post("/thread/<int:thread_id>/reply")
def reply(thread_id: int):
    body = request.form.get("body", "").strip()
    if not body:
        flash("本文を入力してください。", "error")
        return redirect(url_for("thread", thread_id=thread_id) + "#reply")
    if len(body) > 5000:
        flash("本文は5000文字以内です。", "error")
        return redirect(url_for("thread", thread_id=thread_id) + "#reply")
    try:
        image_name = save_upload()
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("thread", thread_id=thread_id) + "#reply")
    created = now_text()
    with connect_db() as conn:
        exists = conn.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone()
        if not exists:
            abort(404)
        conn.execute(
            "INSERT INTO posts(thread_id, body, author_id, created_at, image_name) VALUES (?, ?, ?, ?, ?)",
            (thread_id, body, anon_id(), created, image_name),
        )
        conn.execute("UPDATE threads SET bumped_at=? WHERE id=?", (created, thread_id))
    return redirect(url_for("thread", thread_id=thread_id) + "#latest")


@app.post("/post/<int:post_id>/like")
def like_post(post_id: int):
    with connect_db() as conn:
        row = conn.execute("SELECT thread_id FROM posts WHERE id=?", (post_id,)).fetchone()
        if not row:
            abort(404)
        conn.execute("UPDATE posts SET likes=likes+1 WHERE id=?", (post_id,))
    return redirect(url_for("thread", thread_id=row["thread_id"]) + f"#post-{post_id}")


@app.get("/uploads/<path:filename>")
def uploaded_file(filename: str):
    return send_from_directory(UPLOAD_DIR, filename)


@app.errorhandler(413)
def too_large(_):
    return "画像サイズは5MB以下にしてください。", 413


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
