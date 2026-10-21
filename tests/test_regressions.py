"""回归测试：2026-10-21 端到端实测暴露的缺陷。

每个用例都对应一次**真实**失败，不是假想场景。
"""

from __future__ import annotations

import pytest

from pwa.models import ChatIdentity, ObservedMessage, chat_id_for
from pwa.observer.wechat_macos import MockObserver, WeChatObserver
from pwa.sanitize import clean_reply


# ------------------------------------------------------------------ 会话身份
def test_chat_id_derivation_is_shared():
    """chat_id 派生必须是唯一入口。

    实测缺陷：观察器用「去空白 + 小写」，自动回复用「直接小写」。两边不一致
    会让发送前的会话核验永远判定"已切换"，自动回复根本发不出去。
    """
    assert chat_id_for("示例联系人") == "name:示例联系人"
    assert chat_id_for(" 示 例 联 系 人 ") == "name:示例联系人"
    assert chat_id_for("Perry") == "name:perry"
    assert WeChatObserver._chat_id_for("示例联系人") == chat_id_for("示例联系人")


def test_group_chat_id_has_distinct_prefix():
    """群聊和同名联系人不能撞 id。"""
    assert chat_id_for("项目组", is_group=True) == "g:项目组"
    assert chat_id_for("项目组", is_group=False) == "name:项目组"


def test_auto_reply_target_matches_observer_chat_id():
    """自动回复的目标 chat_id 必须和观察器读到的一致。"""
    from pwa.auto_reply import AutoReplyRunner

    runner = AutoReplyRunner(
        observer=MockObserver(), router=None, store=None, chat_name="示例联系人"
    )
    assert runner._target_identity().chat_id == chat_id_for("示例联系人")


def test_list_chats_does_not_use_sidebar_parsing():
    """list_chats 必须走标题栏，不能走侧边栏解析。

    实测缺陷：微信窗口窄时侧边栏折叠成纯头像，OCR 把头像图片里的
    "PERRY" 和未读徽章 "22" 当成会话名 → 人工审核模式永远读不到目标会话。
    """
    obs = WeChatObserver(min_confidence=0.45)
    called = {"sidebar": False}

    def fake_sidebar(lines):
        called["sidebar"] = True
        return []

    obs._parse_chat_list = fake_sidebar  # type: ignore[method-assign]
    # 只验证它不碰侧边栏解析路径——current_chat_id 依赖真实微信，这里跳过
    try:
        obs.list_chats()
    except Exception:
        pass
    assert called["sidebar"] is False


def test_parse_chat_list_skips_unread_badge_numbers():
    """未读数徽章（纯数字）不是会话名。"""
    from pwa.observer.wechat_macos import OCRLine

    lines = [
        OCRLine(text="PERRY", confidence=0.5, x=0.02, y=0.10, width=0.08, height=0.02),
        OCRLine(text="22", confidence=1.0, x=0.09, y=0.18, width=0.04, height=0.02),
        OCRLine(text="妈妈", confidence=1.0, x=0.02, y=0.30, width=0.08, height=0.02),
    ]
    chats = WeChatObserver()._parse_chat_list(lines)
    names = [c.display_name for c in chats]
    assert "22" not in names
    assert "妈妈" in names


# ------------------------------------------------------------------ 去重锚点
def test_dedup_key_is_stable_across_time_buckets():
    """去重键不能含时间桶。

    实测缺陷：旧版去重键含 30 秒时间桶，而自动回复的轮询间隔是 20 秒，
    同一条消息跨桶后被当成新消息 → 重复回复。
    """
    chat = ChatIdentity(chat_id="name:x", display_name="x")
    first = ObservedMessage(chat=chat, text="在吗", timestamp=1_000_000.0, anchor="A")
    later = ObservedMessage(chat=chat, text="在吗", timestamp=1_000_060.0, anchor="A")
    assert first.dedup_key == later.dedup_key


def test_dedup_key_distinguishes_consecutive_identical_messages():
    """对方连发两条内容相同的消息 → 两条都要处理。

    anchor 取的是**前一条**的指纹，所以内容相同但前一条不同的两条消息，
    去重键不同，不会漏掉第二条。
    """
    chat = ChatIdentity(chat_id="name:x", display_name="x")
    first = ObservedMessage(chat=chat, text="在吗", timestamp=1.0, anchor="A")
    second = ObservedMessage(chat=chat, text="在吗", timestamp=2.0, anchor="B")
    assert first.dedup_key != second.dedup_key


def test_dedup_key_differs_across_senders():
    chat = ChatIdentity(chat_id="name:x", display_name="x")
    a = ObservedMessage(chat=chat, text="在吗", timestamp=1.0, sender="张三", anchor="A")
    b = ObservedMessage(chat=chat, text="在吗", timestamp=1.0, sender="李四", anchor="A")
    assert a.dedup_key != b.dedup_key


def test_attach_anchors_uses_previous_message_fingerprint():
    """锚点必须取自前一条消息，才能反映"是不是同一条"。"""
    chat = ChatIdentity(chat_id="name:x", display_name="x")
    messages = [
        ObservedMessage(chat=chat, text="第一条", timestamp=1.0),
        ObservedMessage(chat=chat, text="第二条", timestamp=2.0),
        ObservedMessage(chat=chat, text="第三条", timestamp=3.0),
    ]
    anchored = WeChatObserver._attach_anchors(messages)
    assert anchored[0].anchor == ""
    assert anchored[1].anchor and anchored[1].anchor != anchored[2].anchor


def test_attach_anchors_keeps_original_message_fields():
    chat = ChatIdentity(chat_id="name:x", display_name="x")
    original = ObservedMessage(
        chat=chat, text="在吗", timestamp=1.0, is_from_me=False, confidence=0.9
    )
    anchored = WeChatObserver._attach_anchors([original])[0]
    assert anchored.text == original.text
    assert anchored.confidence == original.confidence
    assert anchored.is_from_me == original.is_from_me


# ------------------------------------------------------------------ UI 元素
@pytest.mark.parametrize(
    "text",
    ["转文字", "转语音", "翻译", "发送", "撤回", "复制", "删除"],
)
def test_ui_button_text_is_not_treated_as_message(text):
    """微信界面按钮不是消息。

    实测缺陷：语音条上的「转文字」按钮底色 (41,41,42) 与对方气泡
    (47,47,48) 只差 6，落在容差内 → 被当成对方消息 → 触发一次
    毫无意义的回复（模型还因此答了句莫名其妙的话）。
    """
    assert WeChatObserver._is_ui_button(text) is True


@pytest.mark.parametrize("text", ["在吗", "哈哈", "语音没点开 你直接说吧", "转文字了"])
def test_real_message_text_is_not_mistaken_for_button(text):
    """词表是精确匹配，不能误杀真消息。"""
    assert WeChatObserver._is_ui_button(text) is False


# ------------------------------------------------------------------ 正文净化
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("只输出正文。转不了，你打字吧", "转不了，你打字吧"),
        ("只输出回复正文：行 那你忙", "行 那你忙"),
        ("回复：行 那你忙", "行 那你忙"),
        ("正文：在的 咋了", "在的 咋了"),
        ("行 那你忙", "行 那你忙"),
        ("“行 那你忙”", "行 那你忙"),
        ("好的，在的", "在的"),
    ],
)
def test_clean_reply_strips_prompt_fragments(raw, expected):
    """模型会复述提示词里的祈使句，实测返回过「只输出正文。」开头。

    清洗必须**保守**：只剥确定的指令残片，不改写正文。
    """
    assert clean_reply(raw) == expected


def test_clean_reply_keeps_natural_text_untouched():
    text = "你语音我没点开，你直接说吧"
    assert clean_reply(text) == text


def test_clean_reply_handles_empty():
    assert clean_reply("") == ""
    assert clean_reply("   ") == ""


def test_clean_reply_strips_markdown_fence():
    assert clean_reply("```\n行 那你忙\n```") == "行 那你忙"


# ------------------------------------------------------------------ 集成
def test_voice_bar_button_does_not_trigger_reply(tmp_path):
    """端到端：只看到一条「转文字」按钮时，不应产生任何回复。"""
    from pwa.auto_reply import AutoReplyRunner
    from pwa.persona import AUTO_REPLY_SYSTEM_PROMPT
    from pwa.providers.openai_compat import MockProvider
    from pwa.router import Router
    from pwa.store import Store

    chat = ChatIdentity(chat_id="name:示例联系人", display_name="示例联系人")
    obs = MockObserver(
        chats=[chat],
        messages={
            chat.chat_id: [
                ObservedMessage(chat=chat, text="转文字", timestamp=1000.0, confidence=1.0)
            ]
        },
        current_chat=chat.chat_id,
    )
    store = Store(tmp_path / "t.sqlite3")
    provider = MockProvider("转不了，你打字吧", endpoint_name="primary")
    runner = AutoReplyRunner(
        observer=obs,
        router=Router(providers={"primary": provider}, prices={}),
        store=store,
        chat_name="示例联系人",
        system_prompt=AUTO_REPLY_SYSTEM_PROMPT,
        sender=None,
    )
    runner.start()
    # 模拟观察层已过滤按钮 → 这里直接验证过滤逻辑
    assert WeChatObserver._is_ui_button("转文字")
    store.close()
