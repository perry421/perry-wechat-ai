"""Web 界面与发送安全门的测试。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from pwa.models import ChatIdentity, DraftStatus, ObservedMessage
from pwa.observer.wechat_macos import MockObserver
from pwa.pipeline import Pipeline
from pwa.providers.openai_compat import MockProvider
from pwa.router import Router
from pwa.sender import SendGate
from pwa.store import Store
from pwa.web.app import create_app


@pytest.fixture()
def env(tmp_path):
    chat = ChatIdentity(chat_id="name:示例联系人", display_name="示例联系人")
    obs = MockObserver(
        chats=[chat],
        messages={
            chat.chat_id: [
                ObservedMessage(chat=chat, text="周末回家吃饭吗", timestamp=1000.0)
            ]
        },
        current_chat=chat.chat_id,
    )
    store = Store(tmp_path / "web.sqlite3")
    router = Router(
        providers={"primary": MockProvider("（模拟）好的，周末我回去。", endpoint_name="primary")},
        prices={},
    )
    pipeline = Pipeline(
        observer=obs, router=router, store=store, allowed_chats=("示例联系人",)
    )
    yield chat, obs, store, router, pipeline
    store.close()


def _client(env, *, sender=None, enable_send=True, allow_chats=("示例联系人",)):
    chat, obs, store, router, pipeline = env
    gate = SendGate(
        store=store,
        observer=obs,
        enable_send=enable_send,
        sender=sender,
        allow_chats=allow_chats,
    )
    app = create_app(
        config=None, store=store, router=router, pipeline=pipeline, send_gate=gate
    )
    return TestClient(app)


def test_index_page_loads(env):
    with _client(env) as c:
        assert c.get("/").status_code == 200


def test_poll_creates_draft(env):
    with _client(env) as c:
        result = c.post("/api/observe/poll").json()
        assert result["ok"] is True
        assert result["created"] == 1
        drafts = c.get("/api/drafts").json()
        assert len(drafts) == 1
        assert drafts[0]["status"] == DraftStatus.PENDING.value


def test_edit_then_approve_then_send(env):
    with _client(env, sender=lambda cid, text: {"ok": True}) as c:
        c.post("/api/observe/poll")
        draft_id = c.get("/api/drafts").json()[0]["id"]

        assert c.post(f"/api/drafts/{draft_id}/edit", json={"reply_text": "好的 我回去"}).status_code == 200
        assert c.post(f"/api/drafts/{draft_id}/approve").json()["ok"] is True

        res = c.post(f"/api/drafts/{draft_id}/send", json={"draft_id": draft_id}).json()
        assert res["ok"] is True
        assert res["status"] == DraftStatus.SENT.value

        final = c.get(f"/api/drafts/{draft_id}").json()
        assert final["status"] == DraftStatus.SENT.value
        assert final["reply_text"] == "好的 我回去"


def test_dry_run_is_not_reported_as_sent(env):
    """dry-run 只填输入框，绝不能显示成"已发送"。"""
    with _client(env, sender=lambda cid, text: {"ok": True, "typed_only": True}) as c:
        c.post("/api/observe/poll")
        draft_id = c.get("/api/drafts").json()[0]["id"]
        c.post(f"/api/drafts/{draft_id}/approve")

        res = c.post(f"/api/drafts/{draft_id}/send", json={"draft_id": draft_id}).json()
        assert res["typed_only"] is True
        assert res["status"] == DraftStatus.APPROVED.value

        final = c.get(f"/api/drafts/{draft_id}").json()
        assert final["status"] == DraftStatus.APPROVED.value
        assert final["status"] != DraftStatus.SENT.value
        assert "dry-run" in final["error"]


def test_send_without_approval_is_blocked(env):
    """没批准就发送 → 拦截。"""
    with _client(env, sender=lambda cid, text: {"ok": True}) as c:
        c.post("/api/observe/poll")
        draft_id = c.get("/api/drafts").json()[0]["id"]
        res = c.post(f"/api/drafts/{draft_id}/send", json={"draft_id": draft_id})
        assert res.status_code == 409
        assert res.json()["ok"] is False
        assert res.json()["kind"] == "not_approved"


def test_send_disabled_blocks(env):
    """PWA_ENABLE_SEND=false → 一律拦截。"""
    with _client(env, sender=lambda cid, text: {"ok": True}, enable_send=False) as c:
        c.post("/api/observe/poll")
        draft_id = c.get("/api/drafts").json()[0]["id"]
        c.post(f"/api/drafts/{draft_id}/approve")
        res = c.post(f"/api/drafts/{draft_id}/send", json={"draft_id": draft_id})
        assert res.status_code == 409
        assert "未启用" in res.json()["error"]


def test_send_blocked_when_chat_switched(env):
    """发送前会话被切走 → 拦截。"""
    chat, obs, store, router, pipeline = env
    other = ChatIdentity(chat_id="name:小明", display_name="小明")
    with _client(env, sender=lambda cid, text: {"ok": True}) as c:
        c.post("/api/observe/poll")
        draft_id = c.get("/api/drafts").json()[0]["id"]
        c.post(f"/api/drafts/{draft_id}/approve")
        obs.set_current_chat(other.chat_id)  # 模拟用户切走
        res = c.post(f"/api/drafts/{draft_id}/send", json={"draft_id": draft_id})
        assert res.status_code == 409
        assert "切换" in res.json()["error"]


def test_cancel_draft(env):
    with _client(env) as c:
        c.post("/api/observe/poll")
        draft_id = c.get("/api/drafts").json()[0]["id"]
        assert c.post(f"/api/drafts/{draft_id}/cancel").json()["ok"] is True
        assert c.get(f"/api/drafts/{draft_id}").json()["status"] == DraftStatus.CANCELLED.value


def test_status_exposes_send_switches(env):
    with _client(env) as c:
        status = c.get("/api/status").json()
        assert "send_enabled" in status
        assert "send_dry_run" in status
        assert "send_rate_max" in status


def test_status_exposes_draft_counts_by_state(env):
    """状态接口能区分待审核 / 已批准 / 已发送 / 失败。"""
    with _client(env, sender=lambda cid, text: {"ok": True}) as c:
        c.post("/api/observe/poll")
        draft_id = c.get("/api/drafts").json()[0]["id"]
        c.post(f"/api/drafts/{draft_id}/approve")
        c.post(f"/api/drafts/{draft_id}/send", json={"draft_id": draft_id})

        counts = c.get("/api/status").json()["draft_counts"]
        assert counts.get("sent") == 1
        assert counts.get("approved") == 0
        assert counts.get("pending") == 0


def test_auto_reply_stop_endpoint_reports_not_running(env):
    """没跑自动回复时点停止 → 501，不应假装成功。"""
    with _client(env) as c:
        res = c.post("/api/auto-reply/stop")
        assert res.status_code == 501


def test_auto_reply_stop_trips_the_gate(env):
    """网页上的"停止自动回复"必须同时拒绝之后的发送。"""
    chat, obs, store, router, pipeline = env
    from pwa.guard import SendGatekeeper
    from pwa.auto_reply import AutoReplyRunner

    runner = AutoReplyRunner(
        observer=obs,
        router=router,
        store=store,
        chat_name="示例联系人",
        gatekeeper=SendGatekeeper(allow_chats=("示例联系人",)),
    )
    runner.start()

    gate = SendGate(
        store=store,
        observer=obs,
        enable_send=True,
        sender=lambda cid, text: {"ok": True},
        allow_chats=("示例联系人",),
    )
    app = create_app(
        config=None, store=store, router=router, pipeline=pipeline, send_gate=gate,
        auto_reply=runner,
    )
    with TestClient(app) as c:
        res = c.post("/api/auto-reply/stop").json()
        assert res["ok"] is True
        assert res["stopped"] is True
        assert runner.state.running is False
        assert runner.gatekeeper.emergency_stopped is True


# ------------------------------------------------------------------ 白名单
def test_send_blocked_when_chat_not_allowlisted(env):
    """草稿会话不在白名单 → 拦截。"""
    with _client(env, sender=lambda cid, text: {"ok": True}, allow_chats=("别人",)) as c:
        c.post("/api/observe/poll")
        draft_id = c.get("/api/drafts").json()[0]["id"]
        c.post(f"/api/drafts/{draft_id}/approve")
        res = c.post(f"/api/drafts/{draft_id}/send", json={"draft_id": draft_id})
        assert res.status_code == 409
        assert "白名单" in res.json()["error"]


def test_send_blocked_when_allowlist_empty(env):
    """白名单为空 → 拒绝一切发送。"""
    with _client(env, sender=lambda cid, text: {"ok": True}, allow_chats=()) as c:
        c.post("/api/observe/poll")
        draft_id = c.get("/api/drafts").json()[0]["id"]
        c.post(f"/api/drafts/{draft_id}/approve")
        res = c.post(f"/api/drafts/{draft_id}/send", json={"draft_id": draft_id})
        assert res.status_code == 409
        assert "白名单为空" in res.json()["error"]


def test_send_blocked_when_rate_limited(env):
    """超过发送频率上限 → 拦截。"""
    from pwa.guard import RateLimiter

    chat, obs, store, router, pipeline = env
    gate = SendGate(
        store=store,
        observer=obs,
        enable_send=True,
        sender=lambda cid, text: {"ok": True},
        allow_chats=("示例联系人",),
        rate_limiter=RateLimiter(max_sends=1, window_seconds=60),
    )
    app = create_app(
        config=None, store=store, router=router, pipeline=pipeline, send_gate=gate
    )
    with TestClient(app) as c:
        c.post("/api/observe/poll")
        drafts = c.get("/api/drafts").json()
        first_id = drafts[0]["id"]
        c.post(f"/api/drafts/{first_id}/approve")
        assert c.post(f"/api/drafts/{first_id}/send", json={"draft_id": first_id}).status_code == 200

        # 第二条草稿：频率已超限
        obs.set_messages(
            chat.chat_id,
            [
                ObservedMessage(
                    chat=chat, text="还在吗", timestamp=2000.0, confidence=1.0
                )
            ],
        )
        c.post("/api/observe/poll")
        second_id = [
            d["id"]
            for d in c.get("/api/drafts").json()
            if d["id"] != first_id
        ][0]
        c.post(f"/api/drafts/{second_id}/approve")
        res = c.post(f"/api/drafts/{second_id}/send", json={"draft_id": second_id})
        assert res.status_code == 409
        assert "频率" in res.json()["error"]
        assert res.json()["kind"] == "rate_limited"


# ------------------------------------------------------------ 错误信息不外泄
def test_send_error_response_does_not_leak_internal_details(env):
    """接口返回的文案必须是固定文案，不能带内部细节。

    CodeQL 的 py/stack-trace-exposure 指出异常原文会流向 HTTP 响应。
    这里验证：发送器抛出带本机路径的异常时，响应里看不到那些内容。
    """
    secret = "/Users/someone/Library/PrivateFolder/secret-token"

    def exploding_sender(cid, text):
        raise RuntimeError(f"失败细节 {secret}")

    with _client(env, sender=exploding_sender) as c:
        c.post("/api/observe/poll")
        draft_id = c.get("/api/drafts").json()[0]["id"]
        c.post(f"/api/drafts/{draft_id}/approve")
        res = c.post(f"/api/drafts/{draft_id}/send", json={"draft_id": draft_id})
        assert res.status_code == 409
        body = res.json()
        assert body["kind"] == "send_error"
        assert secret not in json.dumps(body)
        assert "RuntimeError" not in json.dumps(body)
        # 详细原因仍然留在本地事件表里，便于排查
        detail = c.get(f"/api/drafts/{draft_id}").json()
        assert detail["status"] == DraftStatus.FAILED.value


def test_allowlist_error_does_not_reveal_whitelist_contents(env):
    """不在白名单时，响应不能列出白名单里都有谁。"""
    with _client(env, sender=lambda cid, text: {"ok": True}, allow_chats=("别人",)) as c:
        c.post("/api/observe/poll")
        draft_id = c.get("/api/drafts").json()[0]["id"]
        c.post(f"/api/drafts/{draft_id}/approve")
        res = c.post(f"/api/drafts/{draft_id}/send", json={"draft_id": draft_id})
        assert res.status_code == 409
        body = res.json()
        assert body["kind"] == "not_allowlisted"
        assert "别人" not in json.dumps(body)
