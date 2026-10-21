"""本地存储：草稿队列、处理记录、去重表。

用 SQLite，纯本地。聊天正文默认不落盘持久化到"内容"表，只保留必要的
触发文本以供人工审核；原始观察数据默认不保存（受 no-store 策略控制）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .models import ChatIdentity, DraftStatus, ObservedMessage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    chat_name TEXT NOT NULL,
    is_group INTEGER NOT NULL DEFAULT 0,
    trigger_text TEXT NOT NULL,
    trigger_sender TEXT DEFAULT '',
    context_json TEXT DEFAULT '[]',
    reply_text TEXT NOT NULL,
    model TEXT DEFAULT '',
    endpoint TEXT DEFAULT '',
    difficulty TEXT DEFAULT '',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cost REAL DEFAULT 0,
    status TEXT NOT NULL,
    error TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    approved_at REAL,
    sent_at REAL,
    edited INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id TEXT,
    kind TEXT NOT NULL,
    detail TEXT DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS seen (
    dedup_key TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts_chat (
    chat_id TEXT NOT NULL,
    chat_name TEXT NOT NULL,
    is_group INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT NOT NULL,
    PRIMARY KEY (chat_id)
);

CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
CREATE INDEX IF NOT EXISTS idx_events_draft ON events(draft_id);
"""


class Store:
    """线程安全的 SQLite 封装。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- 草稿 ---------------------------------------------------------------
    def insert_draft(self, draft: dict[str, Any]) -> str:
        with self._lock:
            self._conn.execute(
                """INSERT INTO drafts (id, chat_id, chat_name, is_group, trigger_text,
                   trigger_sender, context_json, reply_text, model, endpoint, difficulty,
                   prompt_tokens, completion_tokens, cost, status, error, created_at, updated_at)
                   VALUES (:id,:chat_id,:chat_name,:is_group,:trigger_text,:trigger_sender,
                   :context_json,:reply_text,:model,:endpoint,:difficulty,:prompt_tokens,
                   :completion_tokens,:cost,:status,:error,:created_at,:updated_at)""",
                draft,
            )
            self._conn.commit()
        return draft["id"]

    def update_draft(self, draft_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{k} = :{k}" for k in fields)
        params = dict(fields)
        params["_id"] = draft_id
        with self._lock:
            self._conn.execute(
                f"UPDATE drafts SET {assignments} WHERE id = :_id", params
            )
            self._conn.commit()

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_drafts(self, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM drafts WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM drafts ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    def count_drafts_by_status(self) -> dict[str, int]:
        """按状态统计草稿数量。用于界面区分待审核 / 已批准 / 已发送 / 失败。

        返回所有已知状态，缺失的补 0——调用方不必判空。
        """
        counts = {status.value: 0 for status in DraftStatus}
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM drafts GROUP BY status"
            ).fetchall()
        for row in rows:
            counts[row["status"]] = row["n"]
        return counts

    # -- 事件 ---------------------------------------------------------------
    def add_event(self, draft_id: str | None, kind: str, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (draft_id, kind, detail, created_at) VALUES (?,?,?,?)",
                (draft_id, kind, detail, time.time()),
            )
            self._conn.commit()

    def list_events(self, draft_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE draft_id = ? ORDER BY created_at ASC", (draft_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def list_recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # -- 去重 ---------------------------------------------------------------
    def mark_seen(self, dedup_key: str) -> bool:
        """登记去重键。返回 True 表示是首次出现。"""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO seen (dedup_key, created_at) VALUES (?,?)",
                    (dedup_key, time.time()),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def is_seen(self, dedup_key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM seen WHERE dedup_key = ?", (dedup_key,)
            ).fetchone()
        return row is not None

    # -- 会话身份 -----------------------------------------------------------
    def upsert_chat(self, chat: ChatIdentity) -> bool:
        """记录会话身份。返回 True 表示身份发生变化（名称被改）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT fingerprint, chat_name FROM drafts_chat WHERE chat_id = ?",
                (chat.chat_id,),
            ).fetchone()
            changed = row is not None and row["fingerprint"] != chat.fingerprint
            with self._lock:
                self._conn.execute(
                    """INSERT INTO drafts_chat (chat_id, chat_name, is_group, fingerprint)
                       VALUES (?,?,?,?)
                       ON CONFLICT(chat_id) DO UPDATE SET
                         chat_name=excluded.chat_name,
                         is_group=excluded.is_group,
                         fingerprint=excluded.fingerprint""",
                    (
                        chat.chat_id,
                        chat.display_name,
                        int(chat.is_group),
                        chat.fingerprint,
                    ),
                )
                self._conn.commit()
        return changed

    def known_chat_name(self, chat_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT chat_name FROM drafts_chat WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return row["chat_name"] if row else None


def new_draft_id() -> str:
    return f"d_{int(time.time() * 1000):x}_{id(object()):x}"[-24:]


def build_draft_row(
    *,
    chat: ChatIdentity,
    trigger: ObservedMessage,
    context: list[ObservedMessage],
    reply_text: str,
    model: str,
    endpoint: str,
    difficulty: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost: float,
) -> dict[str, Any]:
    now = time.time()
    return {
        "id": new_draft_id(),
        "chat_id": chat.chat_id,
        "chat_name": chat.display_name,
        "is_group": int(chat.is_group),
        "trigger_text": trigger.text,
        "trigger_sender": trigger.sender,
        "context_json": json.dumps(
            [{"text": m.text, "sender": m.sender, "is_from_me": m.is_from_me} for m in context],
            ensure_ascii=False,
        ),
        "reply_text": reply_text,
        "model": model,
        "endpoint": endpoint,
        "difficulty": difficulty,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost": cost,
        "status": DraftStatus.PENDING.value,
        "error": "",
        "created_at": now,
        "updated_at": now,
    }
