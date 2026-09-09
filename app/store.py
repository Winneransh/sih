"""
Session and chat persistence.

SQLite for conversations, messages and plan records. Artifacts live on disk
and are referenced by path — the database holds pointers, not blobs.

Chat memory is kept per conversation: recent turns go into the planner's
context so a follow-up like "now make that a deck" resolves against what
came before.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from .config import APP_DIR

DB_PATH = APP_DIR / "sessions.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT,
    role TEXT,
    content TEXT,
    meta TEXT,
    created_at REAL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    request TEXT,
    plan TEXT,
    result TEXT,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)


init()


# ------------------------------------------------------------ conversations

def new_conversation(title: str = "New chat") -> str:
    cid = uuid.uuid4().hex[:16]
    now = time.time()
    with _conn() as c:
        c.execute("INSERT INTO conversations VALUES (?,?,?,?)",
                  (cid, title, now, now))
    return cid


def list_conversations(limit: int = 50) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def rename_conversation(cid: str, title: str) -> None:
    with _conn() as c:
        c.execute("UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                  (title, time.time(), cid))


def delete_conversation(cid: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
        c.execute("DELETE FROM runs WHERE conversation_id=?", (cid,))
        c.execute("DELETE FROM conversations WHERE id=?", (cid,))


# ---------------------------------------------------------------- messages

def add_message(cid: str, role: str, content: str, meta: dict | None = None) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO messages (conversation_id, role, content, meta, created_at) "
            "VALUES (?,?,?,?,?)",
            (cid, role, content, json.dumps(meta or {}, default=str), time.time()))
        c.execute("UPDATE conversations SET updated_at=? WHERE id=?",
                  (time.time(), cid))


def get_messages(cid: str, limit: int = 100) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM messages WHERE conversation_id=? "
            "ORDER BY id ASC LIMIT ?", (cid, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["meta"] = json.loads(d.get("meta") or "{}")
        out.append(d)
    return out


def recent_turns(cid: str, n: int = 6) -> list[str]:
    """Short history for the planner — enough to resolve a follow-up."""
    msgs = get_messages(cid)[-n:]
    return [f"{m['role']}: {m['content'][:300]}" for m in msgs]


# -------------------------------------------------------------------- runs

def save_run(run_id: str, cid: str, request: str, plan: dict, result: dict) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)",
                  (run_id, cid, request,
                   json.dumps(plan, default=str),
                   json.dumps(result, default=str),
                   time.time()))


def get_run(run_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["plan"] = json.loads(d["plan"])
    d["result"] = json.loads(d["result"])
    return d
