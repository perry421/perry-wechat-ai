"""FastAPI 应用：本地审核队列界面。

只监听 127.0.0.1。所有接口都在本机，无鉴权（依赖本机边界）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..auto_reply import AutoReplyRunner
from ..config import Config, get_config
from ..models import DraftStatus
from ..pipeline import Pipeline
from ..router import Router
from ..sender import SendGate, SendBlocked
from ..store import Store

BASE_DIR = __import__("pathlib").Path(__file__).resolve().parent


class DraftUpdate(BaseModel):
    reply_text: str = Field(min_length=1, max_length=4000)


class DraftAction(BaseModel):
    draft_id: str
    text: str | None = None


def create_app(
    *,
    config: Config | None = None,
    store: Store | None = None,
    router: Router | None = None,
    pipeline: Pipeline | None = None,
    send_gate: SendGate | None = None,
    auto_reply: AutoReplyRunner | None = None,
) -> FastAPI:
    config = config or get_config()
    store = store or Store(config.db_path)
    app = FastAPI(title="Perry WeChat AI", docs_url="/api/docs", redoc_url=None)
    app.state.config = config
    app.state.store = store
    app.state.router = router
    app.state.pipeline = pipeline
    app.state.send_gate = send_gate
    app.state.auto_reply = auto_reply

    static_dir = BASE_DIR / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # -- 页面 ---------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        template = BASE_DIR / "templates" / "index.html"
        return template.read_text(encoding="utf-8")

    # -- 状态 ---------------------------------------------------------------
    @app.get("/api/status")
    def api_status() -> dict[str, Any]:
        pipe: Pipeline | None = app.state.pipeline
        runner: AutoReplyRunner | None = app.state.auto_reply
        counts = store.count_drafts_by_status()
        return {
            "running": bool(pipe and pipe.state.running),
            "paused": bool(pipe and pipe.state.paused),
            "emergency_stop": bool(pipe and pipe.state.emergency_stop),
            "last_error": pipe.state.last_error if pipe else "",
            "last_error_kind": pipe.state.last_error_kind if pipe else "",
            "observed_count": pipe.state.observed_count if pipe else 0,
            "draft_count": pipe.state.draft_count if pipe else 0,
            "total_cost": pipe.state.total_cost if pipe else 0.0,
            "draft_counts": counts,
            "allowed_chats": list(config.allowed_chats),
            "send_enabled": bool(config.enable_send),
            "send_dry_run": bool(config.send_dry_run),
            "send_rate_max": config.send_rate_max,
            "send_rate_window": config.send_rate_window,
            "primary_configured": config.primary.configured,
            "fallback_configured": config.fallback.configured,
            "local_configured": config.local.configured,
            "auto_reply_running": bool(runner and runner.state.running),
            "auto_reply_replied": runner.state.replied_count if runner else 0,
            "auto_reply_polls": runner.state.poll_count if runner else 0,
            "auto_reply_chat": runner.chat_name if runner else "",
            "auto_reply_error": runner.state.last_error if runner else "",
            "time": time.time(),
        }

    # -- 草稿 ---------------------------------------------------------------
    @app.get("/api/drafts")
    def api_drafts(status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        rows = store.list_drafts(status=status, limit=limit)
        for row in rows:
            row["context"] = _safe_json(row.get("context_json"))
            row.pop("context_json", None)
        return rows

    @app.get("/api/drafts/{draft_id}")
    def api_draft(draft_id: str) -> dict[str, Any]:
        draft = store.get_draft(draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="草稿不存在")
        draft["context"] = _safe_json(draft.get("context_json"))
        draft.pop("context_json", None)
        draft["events"] = store.list_events(draft_id)
        return draft

    @app.post("/api/drafts/{draft_id}/edit")
    def api_edit(draft_id: str, payload: DraftUpdate) -> dict[str, Any]:
        draft = store.get_draft(draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="草稿不存在")
        if draft["status"] not in {
            DraftStatus.PENDING.value,
            DraftStatus.APPROVED.value,
            DraftStatus.FAILED.value,
        }:
            raise HTTPException(status_code=409, detail="该状态不允许编辑")
        store.update_draft(draft_id, reply_text=payload.reply_text, edited=1)
        store.add_event(draft_id, "edited", "人工编辑回复内容")
        return {"ok": True}

    @app.post("/api/drafts/{draft_id}/approve")
    def api_approve(draft_id: str) -> dict[str, Any]:
        draft = store.get_draft(draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="草稿不存在")
        if draft["status"] not in {DraftStatus.PENDING.value, DraftStatus.FAILED.value}:
            raise HTTPException(status_code=409, detail="该状态不允许批准")
        store.update_draft(
            draft_id, status=DraftStatus.APPROVED.value, approved_at=time.time(), error=""
        )
        store.add_event(draft_id, "approved", "人工批准")
        return {"ok": True}

    @app.post("/api/drafts/{draft_id}/cancel")
    def api_cancel(draft_id: str) -> dict[str, Any]:
        draft = store.get_draft(draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="草稿不存在")
        store.update_draft(draft_id, status=DraftStatus.CANCELLED.value)
        store.add_event(draft_id, "cancelled", "人工取消")
        return {"ok": True}

    @app.post("/api/drafts/{draft_id}/send")
    def api_send(draft_id: str, payload: DraftAction | None = None) -> dict[str, Any]:
        gate: SendGate | None = app.state.send_gate
        if gate is None:
            raise HTTPException(status_code=501, detail="发送模块未配置")
        try:
            result = gate.send(draft_id, approved_text=(payload.text if payload else None))
        except SendBlocked as exc:
            return JSONResponse(status_code=409, content={"ok": False, "error": str(exc)})
        return {"ok": True, **result}
    # -- 观察控制 -----------------------------------------------------------
    @app.post("/api/observe/pause")
    def api_pause() -> dict[str, Any]:
        pipe: Pipeline | None = app.state.pipeline
        if pipe is None:
            raise HTTPException(status_code=501, detail="观察管线未配置")
        pipe.pause()
        return {"ok": True, "paused": True}

    @app.post("/api/observe/resume")
    def api_resume() -> dict[str, Any]:
        pipe: Pipeline | None = app.state.pipeline
        if pipe is None:
            raise HTTPException(status_code=501, detail="观察管线未配置")
        pipe.resume()
        return {"ok": True, "paused": False}

    @app.post("/api/observe/stop")
    def api_stop() -> dict[str, Any]:
        pipe: Pipeline | None = app.state.pipeline
        if pipe is None:
            raise HTTPException(status_code=501, detail="观察管线未配置")
        pipe.emergency_stop()
        return {"ok": True, "emergency_stop": True}

    @app.post("/api/observe/reset")
    def api_reset() -> dict[str, Any]:
        pipe: Pipeline | None = app.state.pipeline
        if pipe is None:
            raise HTTPException(status_code=501, detail="观察管线未配置")
        pipe.reset_stop()
        return {"ok": True, "emergency_stop": False}

    @app.post("/api/auto-reply/stop")
    def api_auto_reply_stop() -> dict[str, Any]:
        """紧急停止自动回复模式。

        同时 tripped 发送闸门——停止之后即使还有待发消息也不会发出去。
        """
        runner: AutoReplyRunner | None = app.state.auto_reply
        if runner is None:
            raise HTTPException(status_code=501, detail="自动回复未运行")
        runner.emergency_stop("网页端紧急停止")
        return {"ok": True, "stopped": True, "replied_count": runner.state.replied_count}

    @app.post("/api/observe/poll")
    def api_poll() -> dict[str, Any]:
        """手动触发一轮观察（用于调试与验证）。"""
        pipe: Pipeline | None = app.state.pipeline
        if pipe is None:
            raise HTTPException(status_code=501, detail="观察管线未配置")
        try:
            drafts = pipe.poll_once()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(status_code=409, content={"ok": False, "error": str(exc)})
        return {"ok": True, "created": len(drafts)}

    @app.get("/api/events")
    def api_events(limit: int = 100) -> list[dict[str, Any]]:
        return store.list_recent_events(limit=limit)

    return app


def _safe_json(raw: Any) -> Any:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return []
