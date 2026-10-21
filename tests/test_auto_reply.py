"""自动回复模式与真实发送实现的测试。"""

from __future__ import annotations

import pytest

from pwa.auto_reply import AutoReplyRunner
from pwa.models import ChatIdentity, ChatKind, ObservedMessage, ObserverError
from pwa.observer.wechat_macos import MockObserver
from pwa.persona import AUTO_REPLY_SYSTEM_PROMPT, PERRY_PERSONA
from pwa.providers.openai_compat import MockProvider
from pwa.router import Router
from pwa.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "auto.sqlite3")
    yield s
    s.close()


def chat(name="示例联系人") -> ChatIdentity:
    return ChatIdentity(
        chat_id=f"name:{name.lower()}",
        display_name=name,
        kind=ChatKind.CONTACT,
    )


def msg(text: str, *, is_from_me=False, chat_obj=None, confidence=1.0):
    c = chat_obj or chat()
    return ObservedMessage(
        chat=c, text=text, timestamp=1000.0, is_from_me=is_from_me, confidence=confidence
    )


def make_runner(store, observer, *, provider=None, sender=None, chat_name="示例联系人"):
    provider = provider or MockProvider("行 那你忙", endpoint_name="primary")
    router = Router(providers={"primary": provider}, prices={})
    runner = AutoReplyRunner(
        observer=observer,
        router=router,
        store=store,
        chat_name=chat_name,
        system_prompt=AUTO_REPLY_SYSTEM_PROMPT,
        interval=0.01,
        duration=60.0,
        sender=sender,
    )
    runner.start()
    return runner, provider


# ------------------------------------------------------------------ 基本行为
def test_replies_to_new_message(store):
    c = chat()
    obs = MockObserver(
        chats=[c], messages={c.chat_id: [msg("在吗")]}, current_chat=c.chat_id
    )
    runner, provider = make_runner(store, obs)
    results = runner.poll_once()
    assert len(results) == 1
    assert results[0]["reply_text"] == "行 那你忙"
    assert results[0]["sent"] is False  # 没有 sender → 只生成


def test_own_message_is_ignored(store):
    c = chat()
    obs = MockObserver(
        chats=[c],
        messages={c.chat_id: [msg("我刚发的", is_from_me=True)]},
        current_chat=c.chat_id,
    )
    runner, provider = make_runner(store, obs)
    assert runner.poll_once() == []
    assert provider.calls == []


def test_duplicate_message_only_replied_once(store):
    c = chat()
    obs = MockObserver(
        chats=[c], messages={c.chat_id: [msg("在吗")]}, current_chat=c.chat_id
    )
    runner, provider = make_runner(store, obs)
    assert len(runner.poll_once()) == 1
    assert runner.poll_once() == []
    assert len(provider.calls) == 1


def test_low_confidence_is_skipped(store):
    c = chat()
    obs = MockObserver(
        chats=[c],
        messages={c.chat_id: [msg("模糊", confidence=0.1)]},
        current_chat=c.chat_id,
    )
    runner, provider = make_runner(store, obs)
    assert runner.poll_once() == []
    assert provider.calls == []


# ------------------------------------------------------------------ 身份安全
def test_stops_when_current_chat_switched(store):
    c = chat()
    other = ChatIdentity(chat_id="name:小明", display_name="小明")
    obs = MockObserver(
        chats=[c], messages={c.chat_id: [msg("在吗")]}, current_chat=other.chat_id
    )
    runner, provider = make_runner(store, obs)
    assert runner.poll_once() == []
    assert provider.calls == []
    assert runner.state.running is False  # 身份变了 → 整个自动回复停掉


def test_stops_when_login_expired(store):
    c = chat()
    obs = MockObserver(
        chats=[c], messages={c.chat_id: [msg("在吗")]}, current_chat=c.chat_id
    )
    obs.set_login_valid(False)
    runner, provider = make_runner(store, obs)
    assert runner.poll_once() == []
    assert provider.calls == []
    assert runner.state.running is False


def test_stops_when_observer_fails(store):
    c = chat()
    obs = MockObserver(chats=[c], current_chat=c.chat_id)
    obs.set_failure(ObserverError("识别不可靠", kind="ocr_unreliable"))
    runner, provider = make_runner(store, obs)
    assert runner.poll_once() == []
    assert provider.calls == []
    assert runner.state.running is False


def test_cannot_reply_without_confirming_current_chat(store):
    """观察器无法确认当前会话 → 拒绝回复。"""
    c = chat()
    obs = MockObserver(chats=[c], messages={c.chat_id: [msg("在吗")]}, current_chat=None)
    runner, provider = make_runner(store, obs)
    assert runner.poll_once() == []
    assert provider.calls == []


# ------------------------------------------------------------------ 发送
def test_sends_when_sender_configured(store):
    c = chat()
    obs = MockObserver(
        chats=[c], messages={c.chat_id: [msg("在吗")]}, current_chat=c.chat_id
    )
    sent: list[tuple] = []

    def sender(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    runner, _ = make_runner(store, obs, sender=sender)
    results = runner.poll_once()
    assert results[0]["sent"] is True
    assert sent == [(c.chat_id, "行 那你忙")]


def test_send_failure_is_recorded_not_retried(store):
    c = chat()
    obs = MockObserver(
        chats=[c], messages={c.chat_id: [msg("在吗")]}, current_chat=c.chat_id
    )
    calls: list[int] = []

    def sender(chat_id, text):
        calls.append(1)
        raise RuntimeError("粘贴失败")

    runner, _ = make_runner(store, obs, sender=sender)
    results = runner.poll_once()
    assert results[0]["sent"] is False
    assert len(calls) == 1  # 不重试
    kinds = [e["kind"] for e in store.list_recent_events()]
    assert "auto_reply_send_error" in kinds


def test_send_blocked_when_chat_switched_between_poll_and_send(store):
    """生成回复期间会话被切走 → 不发送，并停止。"""
    c = chat()
    other = ChatIdentity(chat_id="name:小明", display_name="小明")
    obs = MockObserver(
        chats=[c], messages={c.chat_id: [msg("在吗")]}, current_chat=c.chat_id
    )

    sent: list[tuple] = []

    def tracking_sender(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    class SwitchingProvider(MockProvider):
        """模型调用期间用户把会话切走了。"""

        def complete(self, system_prompt, context, *, temperature=0.6):
            obs.set_current_chat(other.chat_id)
            return super().complete(system_prompt, context, temperature=temperature)

    runner, _ = make_runner(
        store, obs, provider=SwitchingProvider("行 那你忙", endpoint_name="primary"),
        sender=tracking_sender,
    )
    runner.poll_once()
    assert sent == []
    assert runner.state.running is False


def test_model_error_does_not_stop_the_loop(store):
    """模型偶发失败不应终止整个自动回复。"""
    from pwa.models import ProviderError

    c = chat()
    obs = MockObserver(
        chats=[c], messages={c.chat_id: [msg("在吗")]}, current_chat=c.chat_id
    )
    provider = MockProvider(
        fail_with=ProviderError("超时", kind="timeout"), endpoint_name="primary"
    )
    runner, _ = make_runner(store, obs, provider=provider)
    assert runner.poll_once() == []
    assert runner.state.running is True


def test_no_reply_marker_is_skipped(store):
    c = chat()
    obs = MockObserver(
        chats=[c], messages={c.chat_id: [msg("表情") ]}, current_chat=c.chat_id
    )
    provider = MockProvider("[无需回复]", endpoint_name="primary")
    runner, _ = make_runner(store, obs, provider=provider)
    assert runner.poll_once() == []


# ------------------------------------------------------------------ 网络异常
def test_network_failure_does_not_kill_the_loop(store):
    """网络断开（模型端点不可达）不应终止整个自动回复。"""
    from pwa.models import ProviderError

    c = chat()
    obs = MockObserver(
        chats=[c], messages={c.chat_id: [msg("在吗")]}, current_chat=c.chat_id
    )
    provider = MockProvider(
        fail_with=ProviderError("连接失败", kind="network"), endpoint_name="primary"
    )
    runner, _ = make_runner(store, obs, provider=provider)
    assert runner.poll_once() == []
    assert runner.state.running is True
    assert runner.state.last_error


def test_run_loop_survives_a_failing_poll(store):
    """单轮抛出未预期异常时，主循环继续跑，不退出。"""
    c = chat()
    obs = MockObserver(
        chats=[c], messages={c.chat_id: [msg("在吗")]}, current_chat=c.chat_id
    )
    runner, _ = make_runner(store, obs)
    runner.interval = 0.0

    calls = {"n": 0}
    original = runner.poll_once

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟未知故障")
        return original()

    runner.poll_once = flaky
    runner.run(max_polls=3)
    # 第 1 轮抛异常，第 2、3 轮照常执行 → 循环没有中断
    assert calls["n"] == 3
    assert runner.state.poll_count == 2
    kinds = [e["kind"] for e in store.list_recent_events()]
    assert "auto_reply_poll_error" in kinds


# ------------------------------------------------------------------ 时长
def test_run_stops_at_deadline(store):
    c = chat()
    obs = MockObserver(
        chats=[c], messages={c.chat_id: [msg("在吗")]}, current_chat=c.chat_id
    )
    runner, _ = make_runner(store, obs)
    runner.duration = 0.0
    runner.state.deadline = runner.state.started_at  # 已到期
    runner.run(max_polls=3)
    assert runner.state.running is False


def test_max_polls_limits_run(store):
    c = chat()
    obs = MockObserver(
        chats=[c], messages={c.chat_id: [msg("在吗")]}, current_chat=c.chat_id
    )
    runner, _ = make_runner(store, obs)
    runner.run(max_polls=2)
    assert runner.state.poll_count == 2


# ------------------------------------------------------------------ 人格提示词
def test_persona_prompt_forbids_execution_and_keeps_untrusted():
    assert "不可信数据" in AUTO_REPLY_SYSTEM_PROMPT
    assert "你没有发送消息的能力" in AUTO_REPLY_SYSTEM_PROMPT
    assert PERRY_PERSONA in AUTO_REPLY_SYSTEM_PROMPT


def test_persona_contains_key_style_rules():
    assert "彻底消除 AI 感" in PERRY_PERSONA
    assert "像真人一样思考" in PERRY_PERSONA
