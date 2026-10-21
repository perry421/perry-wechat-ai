"""发送闸门（白名单 / 限流 / 紧急停止）的测试。"""

from __future__ import annotations

import pytest

from pwa.auto_reply import AutoReplyRunner
from pwa.guard import RateLimiter, SendGatekeeper, SendNotAllowed
from pwa.models import ChatIdentity, ChatKind, ObservedMessage
from pwa.observer.wechat_macos import MockObserver
from pwa.persona import AUTO_REPLY_SYSTEM_PROMPT
from pwa.providers.openai_compat import MockProvider
from pwa.router import Router
from pwa.store import Store


# ------------------------------------------------------------------ 限流器
def test_rate_limiter_allows_up_to_limit():
    limiter = RateLimiter(max_sends=3, window_seconds=60)
    assert limiter.allow(now=100.0) is True
    assert limiter.allow(now=100.0) is True
    assert limiter.allow(now=100.0) is True
    assert limiter.allow(now=100.0) is False


def test_rate_limiter_window_slides():
    """窗口滑出后重新允许。"""
    limiter = RateLimiter(max_sends=2, window_seconds=60)
    limiter.allow(now=100.0)
    limiter.allow(now=110.0)
    assert limiter.allow(now=130.0) is False
    assert limiter.allow(now=161.0) is True  # 第一条滑出窗口


def test_rate_limiter_check_does_not_consume():
    """check 只检查不占用配额。"""
    limiter = RateLimiter(max_sends=1, window_seconds=60)
    assert limiter.check(now=100.0) is True
    assert limiter.check(now=100.0) is True
    limiter.allow(now=100.0)
    assert limiter.check(now=100.0) is False


def test_rate_limiter_reset_clears():
    limiter = RateLimiter(max_sends=1, window_seconds=60)
    limiter.allow(now=100.0)
    assert limiter.check(now=100.0) is False
    limiter.reset()
    assert limiter.check(now=100.0) is True


# ------------------------------------------------------------------ 闸门
def test_empty_allowlist_blocks_everything():
    """白名单为空 → 一律拒绝（默认安全）。"""
    gate = SendGatekeeper(allow_chats=())
    with pytest.raises(SendNotAllowed) as exc:
        gate.check("示例联系人")
    assert exc.value.kind == "no_allowlist"


def test_allowlist_rejects_other_chat():
    gate = SendGatekeeper(allow_chats=("示例联系人",))
    gate.check("示例联系人")  # 不抛
    with pytest.raises(SendNotAllowed) as exc:
        gate.check("张三")
    assert exc.value.kind == "not_allowlisted"


def test_allowlist_is_case_and_space_insensitive():
    gate = SendGatekeeper(allow_chats=("Perry",))
    gate.check("perry")
    gate.check("  PERRY  ")


def test_emergency_stop_blocks_sends():
    gate = SendGatekeeper(allow_chats=("示例联系人",))
    gate.trip()
    with pytest.raises(SendNotAllowed) as exc:
        gate.check("示例联系人")
    assert exc.value.kind == "emergency_stop"


def test_reset_clears_emergency_stop():
    gate = SendGatekeeper(allow_chats=("示例联系人",))
    gate.trip()
    gate.reset()
    gate.check("示例联系人")  # 不抛


def test_rate_limit_blocks_after_limit():
    gate = SendGatekeeper(
        allow_chats=("示例联系人",), rate_limiter=RateLimiter(max_sends=1, window_seconds=60)
    )
    gate.record_send()
    with pytest.raises(SendNotAllowed) as exc:
        gate.check("示例联系人")
    assert exc.value.kind == "rate_limited"


# ------------------------------------------------------------------ 集成
@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "guard.sqlite3")
    yield s
    s.close()


def _runner(store, observer, gatekeeper, sender=None):
    provider = MockProvider("在，咋了", endpoint_name="primary")
    runner = AutoReplyRunner(
        observer=observer,
        router=Router(providers={"primary": provider}, prices={}),
        store=store,
        chat_name="示例联系人",
        system_prompt=AUTO_REPLY_SYSTEM_PROMPT,
        sender=sender,
        gatekeeper=gatekeeper,
    )
    runner.start()
    return runner


def _observer(text="在吗"):
    chat = ChatIdentity(chat_id="name:示例联系人", display_name="示例联系人", kind=ChatKind.CONTACT)
    obs = MockObserver(
        chats=[chat],
        messages={
            chat.chat_id: [
                ObservedMessage(chat=chat, text=text, timestamp=1000.0, confidence=1.0)
            ]
        },
        current_chat=chat.chat_id,
    )
    return chat, obs


def test_auto_reply_blocked_by_empty_allowlist(store):
    """白名单为空 → 回复生成了但不发送。"""
    chat, obs = _observer()
    sent: list[str] = []
    gate = SendGatekeeper(allow_chats=())
    runner = _runner(store, obs, gate, sender=lambda cid, t: sent.append(t) or {"ok": True})

    results = runner.poll_once()
    assert len(results) == 1
    assert results[0]["sent"] is False
    assert results[0]["blocked"] == "no_allowlist"
    assert sent == []


def test_auto_reply_blocked_by_rate_limit(store):
    """超过频率上限 → 停止发送，但回复仍被记录。"""
    chat, obs = _observer()
    sent: list[str] = []
    gate = SendGatekeeper(
        allow_chats=("示例联系人",), rate_limiter=RateLimiter(max_sends=1, window_seconds=60)
    )
    runner = _runner(store, obs, gate, sender=lambda cid, t: sent.append(t) or {"ok": True})

    first = runner.poll_once()
    assert first[0]["sent"] is True
    assert len(sent) == 1

    # 换一条新消息再轮一次 → 触发限流
    obs.set_messages(
        chat.chat_id,
        [ObservedMessage(chat=chat, text="在吗在吗", timestamp=2000.0, confidence=1.0)],
    )
    second = runner.poll_once()
    assert second[0]["sent"] is False
    assert second[0]["blocked"] == "rate_limited"
    assert len(sent) == 1


def test_auto_reply_emergency_stop_blocks_and_halts(store):
    """紧急停止后：不再发送，且整个运行器停掉。"""
    chat, obs = _observer()
    sent: list[str] = []
    gate = SendGatekeeper(allow_chats=("示例联系人",))
    runner = _runner(store, obs, gate, sender=lambda cid, t: sent.append(t) or {"ok": True})

    runner.emergency_stop("测试")
    assert runner.state.running is False

    results = runner.poll_once()
    assert results == []
    assert sent == []

    kinds = [e["kind"] for e in store.list_recent_events()]
    assert "auto_reply_emergency_stop" in kinds


def test_emergency_stop_event_recorded(store):
    chat, obs = _observer()
    gate = SendGatekeeper(allow_chats=("示例联系人",))
    runner = _runner(store, obs, gate)
    runner.emergency_stop("用户按了停止")
    events = store.list_recent_events()
    assert any(e["kind"] == "auto_reply_emergency_stop" for e in events)
    assert any("用户按了停止" in e["detail"] for e in events)
