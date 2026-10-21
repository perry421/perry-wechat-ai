"""处理管线：观察 → 校验 → 去重 → 路由 → 生成草稿。

安全边界：
* 本模块**只生成草稿**，不发送任何消息。
* 任何身份不明、OCR 不可靠、登录失效、窗口切换的情况都会立即停止，
  不产生草稿。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .models import (
    ChatIdentity,
    ChatContext,
    ObservedMessage,
    ObserverError,
    ProviderError,
)
from .observer.base import Observer
from .prompts import SYSTEM_PROMPT
from .router import Router
from .store import Store, build_draft_row


@dataclass
class PipelineState:
    """运行期状态，用于暂停 / 紧急停止 / 恢复。"""

    paused: bool = False
    emergency_stop: bool = False
    last_error: str = ""
    last_error_kind: str = ""
    observed_count: int = 0
    draft_count: int = 0
    total_cost: float = 0.0
    started_at: float = field(default_factory=time.time)

    @property
    def running(self) -> bool:
        return not self.paused and not self.emergency_stop


class _SkipChat(Exception):
    """内部信号：这个会话不在授权名单内，跳过即可，不算错误。"""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class Pipeline:
    """把观察到的消息变成待审核草稿。"""

    def __init__(
        self,
        *,
        observer: Observer,
        router: Router,
        store: Store,
        allowed_chats: tuple[str, ...] = (),
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self.observer = observer
        self.router = router
        self.store = store
        self.allowed_chats = allowed_chats
        self.system_prompt = system_prompt
        self.state = PipelineState()

    # -- 安全阀 -------------------------------------------------------------
    def pause(self) -> None:
        self.state.paused = True
        self.store.add_event(None, "paused", "观察已暂停")

    def resume(self) -> None:
        self.state.paused = False
        self.store.add_event(None, "resumed", "观察已恢复")

    def emergency_stop(self, reason: str = "用户紧急停止") -> None:
        self.state.emergency_stop = True
        self.state.paused = True
        self.store.add_event(None, "emergency_stop", reason)

    def reset_stop(self) -> None:
        """清除紧急停止，恢复运行。"""
        self.state.emergency_stop = False
        self.state.paused = False
        self.store.add_event(None, "reset_stop", "紧急停止已解除")

    # -- 身份校验 -----------------------------------------------------------
    def _is_allowed(self, chat: ChatIdentity) -> bool:
        """只观察明确授权的会话。allowed_chats 为空时不做任何观察。"""
        if not self.allowed_chats:
            return False
        return chat.display_name in self.allowed_chats or chat.chat_id in self.allowed_chats

    def _verify_identity(self, chat: ChatIdentity) -> None:
        """身份校验。

        未授权的会话直接**跳过**（不是错误）——微信列表里有很多无关聊天，
        它们不构成异常，也不该中断整轮处理。

        但如果一个**已授权**会话的身份指纹发生变化（例如同名会话被替换、
        群聊变成普通聊天），说明我们可能认错了人，必须立即停止。
        """
        if not self._is_allowed(chat):
            raise _SkipChat(chat.display_name)

        previous = self.store.known_chat_name(chat.chat_id)
        changed = self.store.upsert_chat(chat)
        if changed or (previous is not None and previous != chat.display_name):
            raise ObserverError(
                f"会话 {chat.display_name!r} 的身份指纹发生变化，疑似切换了窗口",
                kind="identity_unknown",
            )

    # -- 主流程 -------------------------------------------------------------
    def poll_once(self) -> list[dict[str, Any]]:
        """执行一轮观察，返回本轮新建的草稿列表。"""
        if not self.state.running:
            self.store.add_event(None, "skipped", "管线处于暂停/停止状态，未执行观察")
            return []

        try:
            if not self.observer.is_login_valid():
                raise ObserverError("微信未处于有效登录状态", kind="login_expired")

            chats = self.observer.list_chats()
            self.state.observed_count += len(chats)
        except ObserverError as exc:
            self._record_error(exc)
            raise

        created: list[dict[str, Any]] = []
        for chat in chats:
            if not self.state.running:
                break
            try:
                self._verify_identity(chat)
                messages = self.observer.read_chat(chat)
            except _SkipChat:
                # 未授权会话：安静跳过
                continue
            except ObserverError as exc:
                self._record_error(exc)
                # 身份/窗口类问题立即停止整轮
                if exc.fatal:
                    raise
                continue

            for message in messages:
                draft = self._process_message(chat, message, history=messages)
                if draft:
                    created.append(draft)
        return created

    def _record_error(self, exc: ObserverError) -> None:
        self.state.last_error = str(exc)
        self.state.last_error_kind = exc.kind
        self.store.add_event(None, f"observer_error:{exc.kind}", str(exc))
        if exc.fatal:
            self.state.paused = True
            self.store.add_event(None, "auto_paused", f"因致命错误暂停：{exc.kind}")

    def _process_message(
        self,
        chat: ChatIdentity,
        message: ObservedMessage,
        *,
        history: list[ObservedMessage] | None = None,
    ) -> dict[str, Any] | None:
        """单条消息 → 草稿。返回 None 表示被跳过。"""
        text = message.text.strip()

        # 空消息直接跳过
        if not text:
            self.store.add_event(None, "skip_empty", f"{chat.display_name}")
            return None

        # 我自己发的消息不需要回复
        if message.is_from_me:
            self.store.add_event(None, "skip_self", f"{chat.display_name}: {text[:40]}")
            return None

        # OCR 置信度过低 → 视为识别不可靠
        if message.confidence < self._min_confidence():
            self.store.add_event(
                None,
                "skip_low_confidence",
                f"{chat.display_name}: conf={message.confidence:.2f}",
            )
            return None

        # 去重
        if not self.store.mark_seen(message.dedup_key):
            return None

        # 上下文带上**双方**的历史。只给最后一句，模型无法判断对方是在
        # 开玩笑还是在认真说事，容易过度保守或答非所问。
        prior = [
            m
            for m in (history or [])
            if m.dedup_key != message.dedup_key
        ]
        context = ChatContext(chat=chat, trigger=message, history=prior)
        decision = self.router.decide(context)

        try:
            reply = self.router.generate(self.system_prompt, context)
        except ProviderError as exc:
            self.store.add_event(
                None,
                f"model_error:{exc.kind}",
                f"{chat.display_name}: {exc}",
            )
            self.state.last_error = str(exc)
            self.state.last_error_kind = exc.kind
            return None

        row = build_draft_row(
            chat=chat,
            trigger=message,
            context=prior,
            reply_text=reply.content,
            model=reply.model,
            endpoint=reply.endpoint,
            difficulty=decision.difficulty.value,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            cost=reply.total_cost,
        )
        self.store.insert_draft(row)
        self.store.add_event(
            row["id"], "draft_created", f"{chat.display_name} / {reply.model}"
        )
        self.state.draft_count += 1
        self.state.total_cost = round(self.state.total_cost + reply.total_cost, 8)
        return row

    def _min_confidence(self) -> float:
        return getattr(self, "_ocr_min_confidence", 0.45)
