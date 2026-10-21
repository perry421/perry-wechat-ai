"""观察 → 去重 → 路由 → 草稿 的管线测试，以及安全门测试。"""

from __future__ import annotations

import pytest

from pwa.models import (
    ChatIdentity,
    ChatKind,
    DraftStatus,
    ObservedMessage,
    ObserverError,
    ProviderError,
)
from pwa.observer.wechat_macos import MockObserver
from pwa.pipeline import Pipeline
from pwa.providers.openai_compat import MockProvider
from pwa.router import Router
from pwa.sender import SendGate, SendBlocked
from pwa.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.sqlite3")
    yield s
    s.close()


def chat(name: str, *, group: bool = False) -> ChatIdentity:
    """构造会话身份。chat_id 派生规则与真实观察器保持一致。"""
    prefix = "g:" if group else "name:"
    return ChatIdentity(
        chat_id=prefix + name.lower(),
        display_name=name,
        kind=ChatKind.GROUP if group else ChatKind.CONTACT,
        is_group=group,
    )


def msg(c: ChatIdentity, text: str, **kw) -> ObservedMessage:
    return ObservedMessage(chat=c, text=text, timestamp=kw.pop("timestamp", 1000.0), **kw)


def make_pipeline(store, observer, provider=None, allowed=("妈妈", "项目组", "小明")):
    provider = provider or MockProvider("（模拟）收到啦", endpoint_name="primary")
    router = Router(providers={"primary": provider}, prices={})
    pipeline = Pipeline(
        observer=observer,
        router=router,
        store=store,
        allowed_chats=allowed,
    )
    pipeline._ocr_min_confidence = 0.0  # noqa: SLF001
    return pipeline, provider


# ------------------------------------------------------------------ 正常路径
def test_normal_message_produces_draft(store):
    c = chat("妈妈")
    obs = MockObserver(chats=[c], messages={c.chat_id: [msg(c, "周末回家吃饭吗")]})
    pipeline, _ = make_pipeline(store, obs)
    drafts = pipeline.poll_once()
    assert len(drafts) == 1
    assert drafts[0]["chat_name"] == "妈妈"
    assert drafts[0]["status"] == DraftStatus.PENDING.value
    assert drafts[0]["reply_text"] == "（模拟）收到啦"


def test_empty_message_is_skipped(store):
    c = chat("妈妈")
    obs = MockObserver(chats=[c], messages={c.chat_id: [msg(c, "   ")]})
    pipeline, provider = make_pipeline(store, obs)
    drafts = pipeline.poll_once()
    assert drafts == []
    assert provider.calls == []


def test_own_message_is_skipped(store):
    c = chat("妈妈")
    obs = MockObserver(
        chats=[c], messages={c.chat_id: [msg(c, "我刚发的", is_from_me=True)]}
    )
    pipeline, provider = make_pipeline(store, obs)
    assert pipeline.poll_once() == []
    assert provider.calls == []


# ------------------------------------------------------------------ 重复消息
def test_duplicate_message_only_produces_one_draft(store):
    c = chat("妈妈")
    m = msg(c, "周末回家吃饭吗")
    obs = MockObserver(chats=[c], messages={c.chat_id: [m]})
    pipeline, provider = make_pipeline(store, obs)
    assert len(pipeline.poll_once()) == 1
    assert pipeline.poll_once() == []
    assert len(provider.calls) == 1


def test_dedup_ignores_whitespace_difference(store):
    c = chat("妈妈")
    obs = MockObserver(chats=[c], messages={c.chat_id: [msg(c, "周末 回家吃饭吗")]})
    pipeline, _ = make_pipeline(store, obs)
    assert len(pipeline.poll_once()) == 1
    obs.set_messages(c.chat_id, [msg(c, "周末  回家吃饭吗")])
    assert pipeline.poll_once() == []


def test_different_chats_are_not_deduped(store):
    a, b = chat("妈妈"), chat("小明")
    obs = MockObserver(
        chats=[a, b],
        messages={
            a.chat_id: [msg(a, "在吗")],
            b.chat_id: [msg(b, "在吗")],
        },
    )
    pipeline, _ = make_pipeline(store, obs)
    assert len(pipeline.poll_once()) == 2


# ------------------------------------------------------- 相似联系人与群聊
def test_similar_contact_names_are_distinct(store):
    a, b = chat("张伟"), chat("张伟民")
    obs = MockObserver(
        chats=[a, b],
        messages={a.chat_id: [msg(a, "在吗")], b.chat_id: [msg(b, "在吗")]},
    )
    pipeline, _ = make_pipeline(store, obs, allowed=("张伟", "张伟民"))
    drafts = pipeline.poll_once()
    assert len(drafts) == 2
    assert {d["chat_name"] for d in drafts} == {"张伟", "张伟民"}


def test_group_chat_is_distinguished_from_contact(store):
    """同名群聊与同名联系人是两个不同的会话，不能混淆。"""
    group = chat("项目组", group=True)
    contact = chat("项目组")
    # chat_id 由 name + kind 派生，确保两者不碰撞
    assert group.chat_id != contact.chat_id
    obs = MockObserver(
        chats=[group, contact],
        messages={
            group.chat_id: [msg(group, "在吗", sender="张三")],
            contact.chat_id: [msg(contact, "在吗")],
        },
    )
    pipeline, _ = make_pipeline(store, obs, allowed=("项目组",))
    drafts = pipeline.poll_once()
    assert len(drafts) == 2
    assert sum(1 for d in drafts if d["is_group"]) == 1


def test_unauthorized_chat_is_ignored(store):
    c = chat("陌生人")
    obs = MockObserver(chats=[c], messages={c.chat_id: [msg(c, "借点钱")]})
    pipeline, provider = make_pipeline(store, obs, allowed=("妈妈",))
    assert pipeline.poll_once() == []
    assert provider.calls == []


def test_no_allowed_chats_means_no_observation(store):
    c = chat("妈妈")
    obs = MockObserver(chats=[c], messages={c.chat_id: [msg(c, "在吗")]})
    pipeline, _ = make_pipeline(store, obs, allowed=())
    assert pipeline.poll_once() == []


# ------------------------------------------------------------ 窗口切换 / 身份
def test_identity_change_stops_processing(store):
    c = chat("妈妈")
    obs = MockObserver(chats=[c], messages={c.chat_id: [msg(c, "在吗")]})
    pipeline, _ = make_pipeline(store, obs)
    pipeline.poll_once()
    # 同名会话被换成另一个身份（指纹变化）→ 立即停止
    changed = ChatIdentity(chat_id="name:妈妈", display_name="妈妈", kind=ChatKind.GROUP)
    obs.set_chats([changed])
    with pytest.raises(ObserverError) as exc:
        pipeline.poll_once()
    assert exc.value.kind == "identity_unknown"
    assert pipeline.state.paused is True


# ------------------------------------------------------------- 登录失效
def test_login_expired_stops_processing(store):
    c = chat("妈妈")
    obs = MockObserver(chats=[c], messages={c.chat_id: [msg(c, "在吗")]})
    obs.set_login_valid(False)
    pipeline, _ = make_pipeline(store, obs)
    with pytest.raises(ObserverError) as exc:
        pipeline.poll_once()
    assert exc.value.kind == "login_expired"
    assert pipeline.state.paused is True


# ------------------------------------------------------------------ OCR 失败
def test_ocr_failure_stops_processing(store):
    c = chat("妈妈")
    obs = MockObserver(chats=[c])
    obs.set_failure(ObserverError("识别不可靠", kind="ocr_unreliable"))
    pipeline, _ = make_pipeline(store, obs)
    with pytest.raises(ObserverError) as exc:
        pipeline.poll_once()
    assert exc.value.kind == "ocr_unreliable"
    assert exc.value.fatal is True


def test_low_confidence_message_is_skipped(store):
    c = chat("妈妈")
    obs = MockObserver(
        chats=[c], messages={c.chat_id: [msg(c, "模糊内容", confidence=0.1)]}
    )
    pipeline, provider = make_pipeline(store, obs)
    pipeline._ocr_min_confidence = 0.45  # noqa: SLF001
    assert pipeline.poll_once() == []
    assert provider.calls == []


# ------------------------------------------------------------------ 模型异常
def test_model_error_records_event_and_no_draft(store):
    c = chat("妈妈")
    obs = MockObserver(chats=[c], messages={c.chat_id: [msg(c, "在吗")]})
    provider = MockProvider(
        fail_with=ProviderError("超时", kind="timeout"), endpoint_name="primary"
    )
    pipeline, _ = make_pipeline(store, obs, provider=provider)
    assert pipeline.poll_once() == []
    kinds = [e["kind"] for e in store.list_recent_events()]
    assert any(k.startswith("model_error") for k in kinds)


# ---------------------------------------------------------------- 暂停/停止
def test_pause_prevents_observation(store):
    c = chat("妈妈")
    obs = MockObserver(chats=[c], messages={c.chat_id: [msg(c, "在吗")]})
    pipeline, provider = make_pipeline(store, obs)
    pipeline.pause()
    assert pipeline.poll_once() == []
    assert provider.calls == []
    pipeline.resume()
    assert len(pipeline.poll_once()) == 1


def test_emergency_stop_prevents_observation(store):
    c = chat("妈妈")
    obs = MockObserver(chats=[c], messages={c.chat_id: [msg(c, "在吗")]})
    pipeline, provider = make_pipeline(store, obs)
    pipeline.emergency_stop("测试")
    assert pipeline.poll_once() == []
    assert provider.calls == []
    assert pipeline.state.emergency_stop is True


def test_reset_stop_allows_resume(store):
    c = chat("妈妈")
    obs = MockObserver(chats=[c], messages={c.chat_id: [msg(c, "在吗")]})
    pipeline, _ = make_pipeline(store, obs)
    pipeline.emergency_stop()
    pipeline.reset_stop()
    assert pipeline.state.emergency_stop is False
    assert len(pipeline.poll_once()) == 1


def test_restart_recovers_from_persisted_state(store):
    """重启后：去重表仍然生效，不会重复生成草稿。"""
    c = chat("妈妈")
    obs = MockObserver(chats=[c], messages={c.chat_id: [msg(c, "在吗")]})
    pipeline, _ = make_pipeline(store, obs)
    assert len(pipeline.poll_once()) == 1

    # 模拟重启：新的 Pipeline，共用同一个 Store
    pipeline2, provider2 = make_pipeline(store, obs)
    assert pipeline2.poll_once() == []
    assert provider2.calls == []


# ================================================================ 发送安全门
def build_send_gate(
    store, observer, *, enable_send=True, sender=None, allow_chats=("妈妈",)
):
    # allow_chats 默认为"妈妈"——白名单为空会拒绝所有发送
    return SendGate(
        store=store,
        observer=observer,
        enable_send=enable_send,
        sender=sender,
        allow_chats=allow_chats,
    )


def make_draft(store, chat_obj, text="在吗", reply="好的"):
    from pwa.store import build_draft_row

    row = build_draft_row(
        chat=chat_obj,
        trigger=msg(chat_obj, text),
        context=[],
        reply_text=reply,
        model="m",
        endpoint="primary",
        difficulty="simple",
        prompt_tokens=10,
        completion_tokens=5,
        cost=0.0,
    )
    store.insert_draft(row)
    return row["id"]


class FakeObserver(MockObserver):
    """带 current_chat_id 的观察器。"""

    def __init__(self, current=None, **kw):
        super().__init__(**kw)
        self._current = current
        self.login = True

    def current_chat_id(self):
        if self._current is None:
            raise ObserverError("无法确定", kind="identity_unknown")
        return self._current

    def is_login_valid(self):
        return self.login


def test_send_disabled_by_default(store):
    c = chat("妈妈")
    obs = FakeObserver(current=c.chat_id, chats=[c], messages={c.chat_id: [msg(c, "在吗")]})
    gate = build_send_gate(store, obs, enable_send=False, sender=lambda *a: True)
    draft_id = make_draft(store, c)
    store.update_draft(draft_id, status=DraftStatus.APPROVED.value)
    with pytest.raises(SendBlocked) as exc:
        gate.send(draft_id)
    assert "未启用" in str(exc.value)


def test_send_requires_explicit_approval(store):
    c = chat("妈妈")
    obs = FakeObserver(current=c.chat_id, chats=[c], messages={c.chat_id: [msg(c, "在吗")]})
    gate = build_send_gate(store, obs, sender=lambda *a: True)
    draft_id = make_draft(store, c)  # 状态是 pending
    with pytest.raises(SendBlocked):
        gate.send(draft_id)


def test_send_blocked_when_no_sender_implementation(store):
    c = chat("妈妈")
    obs = FakeObserver(current=c.chat_id, chats=[c], messages={c.chat_id: [msg(c, "在吗")]})
    gate = build_send_gate(store, obs, sender=None)
    draft_id = make_draft(store, c)
    store.update_draft(draft_id, status=DraftStatus.APPROVED.value)
    with pytest.raises(SendBlocked) as exc:
        gate.send(draft_id)
    assert "复制" in str(exc.value)


def test_send_blocked_when_chat_changed(store):
    """发送前聊天对象变化 → 必须拦截。"""
    c = chat("妈妈")
    other = chat("小明")
    obs = FakeObserver(current=other.chat_id, chats=[c], messages={c.chat_id: [msg(c, "在吗")]})
    gate = build_send_gate(store, obs, sender=lambda *a: True)
    draft_id = make_draft(store, c)
    store.update_draft(draft_id, status=DraftStatus.APPROVED.value)
    with pytest.raises(SendBlocked) as exc:
        gate.send(draft_id)
    assert "切换" in str(exc.value)


def test_send_blocked_when_login_expired(store):
    c = chat("妈妈")
    obs = FakeObserver(current=c.chat_id, chats=[c], messages={c.chat_id: [msg(c, "在吗")]})
    obs.login = False
    gate = build_send_gate(store, obs, sender=lambda *a: True)
    draft_id = make_draft(store, c)
    store.update_draft(draft_id, status=DraftStatus.APPROVED.value)
    with pytest.raises(SendBlocked) as exc:
        gate.send(draft_id)
    assert "登录" in str(exc.value)


def test_send_blocked_when_trigger_no_longer_present(store):
    """触发消息已不在当前会话 → 上下文可能已变化 → 拦截。"""
    c = chat("妈妈")
    obs = FakeObserver(current=c.chat_id, chats=[c], messages={c.chat_id: [msg(c, "在吗")]})
    gate = build_send_gate(store, obs, sender=lambda *a: True)
    draft_id = make_draft(store, c)
    store.update_draft(draft_id, status=DraftStatus.APPROVED.value)
    obs.set_messages(c.chat_id, [msg(c, "完全不同的消息")])
    with pytest.raises(SendBlocked) as exc:
        gate.send(draft_id)
    assert "触发消息" in str(exc.value)


def test_send_succeeds_and_marks_sent(store):
    c = chat("妈妈")
    obs = FakeObserver(current=c.chat_id, chats=[c], messages={c.chat_id: [msg(c, "在吗")]})
    sent: list[tuple] = []

    def sender(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    gate = build_send_gate(store, obs, sender=sender)
    draft_id = make_draft(store, c, reply="好的，我在")
    store.update_draft(draft_id, status=DraftStatus.APPROVED.value)
    result = gate.send(draft_id)
    assert result["status"] == DraftStatus.SENT.value
    assert sent == [(c.chat_id, "好的，我在")]
    assert store.get_draft(draft_id)["status"] == DraftStatus.SENT.value


def test_uncertain_send_result_marks_unknown_not_retry(store):
    c = chat("妈妈")
    obs = FakeObserver(current=c.chat_id, chats=[c], messages={c.chat_id: [msg(c, "在吗")]})
    calls: list[int] = []

    def sender(chat_id, text):
        calls.append(1)
        return {"ok": False, "note": "不确定"}

    gate = build_send_gate(store, obs, sender=sender)
    draft_id = make_draft(store, c)
    store.update_draft(draft_id, status=DraftStatus.APPROVED.value)
    result = gate.send(draft_id)
    assert result["status"] == DraftStatus.UNKNOWN.value
    assert len(calls) == 1  # 只尝试一次，不自动重试
    assert store.get_draft(draft_id)["status"] == DraftStatus.UNKNOWN.value


def test_send_exception_does_not_retry(store):
    c = chat("妈妈")
    obs = FakeObserver(current=c.chat_id, chats=[c], messages={c.chat_id: [msg(c, "在吗")]})
    calls: list[int] = []

    def sender(chat_id, text):
        calls.append(1)
        raise RuntimeError("粘贴失败")

    gate = build_send_gate(store, obs, sender=sender)
    draft_id = make_draft(store, c)
    store.update_draft(draft_id, status=DraftStatus.APPROVED.value)
    with pytest.raises(SendBlocked):
        gate.send(draft_id)
    assert len(calls) == 1
    assert store.get_draft(draft_id)["status"] == DraftStatus.FAILED.value


def test_approval_does_not_carry_to_next_draft(store):
    """批准一条不代表批准后续所有消息。"""
    c = chat("妈妈")
    obs = FakeObserver(current=c.chat_id, chats=[c], messages={c.chat_id: [msg(c, "在吗")]})
    gate = build_send_gate(store, obs, sender=lambda *a: True)
    first = make_draft(store, c)
    second = make_draft(store, c, text="第二条")
    store.update_draft(first, status=DraftStatus.APPROVED.value)
    gate.send(first)
    # 第二条仍然是 pending，发送应当被拦截
    with pytest.raises(SendBlocked):
        gate.send(second)


def test_cannot_guess_current_chat(store):
    """无法确定当前会话时必须拒绝发送。"""
    c = chat("妈妈")
    obs = FakeObserver(current=None, chats=[c], messages={c.chat_id: [msg(c, "在吗")]})
    gate = build_send_gate(store, obs, sender=lambda *a: True)
    draft_id = make_draft(store, c)
    store.update_draft(draft_id, status=DraftStatus.APPROVED.value)
    with pytest.raises(SendBlocked) as exc:
        gate.send(draft_id)
    assert "会话" in str(exc.value)
