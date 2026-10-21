"""示例联系人自动回复模式（可选，默认关闭）。

与规格里的**人工审核模式**并存：``PWA_AUTO_REPLY_ENABLED`` 未打开时，
本模块完全不参与运行，系统的默认行为仍然是"只生成草稿、等人审核"。

打开后（本模块）：

* 每 ``interval`` 秒（默认 20 秒）观察一次目标会话；
* 只处理**对方发来的**新消息（按去重键）；
* 用 :data:`pwa.persona.AUTO_REPLY_SYSTEM_PROMPT` 生成回复；
* 发送前重新核验当前会话仍然是目标会话，核验不通过就**中止**；
* 连续运行 ``duration`` 秒（默认 6 小时）后自动停止。

安全约束（与人工审核模式一致）：

* 只对**明确指定**的一个会话生效，不会误回别人；
* 任何身份不明、OCR 不可靠、登录失效都会让本轮中止，不会带着不确定的
  上下文继续回复；
* 发送结果不确定时**不重试**，只记录，避免重复发送。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .guard import SendGatekeeper, SendNotAllowed
from .models import ChatIdentity, ChatKind, ObserverError, chat_id_for
from .observer.base import Observer
from .prompts import SYSTEM_PROMPT
from .router import Router
from .store import Store


@dataclass
class AutoReplyState:
    """自动回复的运行期状态。"""

    running: bool = False
    started_at: float = 0.0
    deadline: float = 0.0
    poll_count: int = 0
    replied_count: int = 0
    last_error: str = ""
    last_error_kind: str = ""

    @property
    def remaining(self) -> float:
        if not self.running:
            return 0.0
        return max(0.0, self.deadline - time.time())


@dataclass
class AutoReplyRunner:
    """示例联系人自动回复的运行器。

    ``observer`` 必须支持 ``current_chat_id()``——自动回复要确认"当前屏幕上
    显示的就是目标会话"，否则拒绝回复。
    """

    observer: Observer
    router: Router
    store: Store
    chat_name: str
    system_prompt: str = SYSTEM_PROMPT
    interval: float = 20.0
    duration: float = 6 * 3600
    sender: Any | None = None  # 发送回调；None 表示只生成草稿不发送
    # 发送闸门：白名单 + 频率限制 + 紧急停止。为 None 时按最严格处理
    # （白名单为空 → 不允许任何发送）。
    gatekeeper: SendGatekeeper | None = None
    state: AutoReplyState = field(default_factory=AutoReplyState)

    # -- 生命周期 -----------------------------------------------------------
    def start(self) -> None:
        if not self.chat_name.strip():
            raise ValueError("未指定自动回复的目标会话")
        self.state = AutoReplyState(
            running=True,
            started_at=time.time(),
            deadline=time.time() + self.duration,
        )
        self.store.add_event(
            None,
            "auto_reply_started",
            f"目标会话={self.chat_name} 间隔={self.interval}s 时长={self.duration}s",
        )

    def stop(self) -> None:
        self.state.running = False
        self.store.add_event(None, "auto_reply_stopped", f"已回复 {self.state.replied_count} 条")

    def emergency_stop(self, reason: str = "用户紧急停止") -> None:
        """紧急停止：立刻停止轮询，并拒绝之后的所有发送。"""
        self.state.running = False
        if self.gatekeeper is not None:
            self.gatekeeper.trip()
        self.store.add_event(None, "auto_reply_emergency_stop", reason)

    # -- 身份 ---------------------------------------------------------------
    def _target_identity(self) -> ChatIdentity:
        # 用与观察器相同的派生规则，保证 chat_id 与 current_chat_id() 完全
        # 一致——不一致会导致发送前的会话核验永远失败。
        chat_id = chat_id_for(self.chat_name.strip())
        return ChatIdentity(
            chat_id=chat_id,
            display_name=self.chat_name.strip(),
            kind=ChatKind.CONTACT,
            is_group=False,
        )

    def _verify_current_chat(self) -> None:
        """确认当前微信窗口显示的就是目标会话。"""
        getter = getattr(self.observer, "current_chat_id", None)
        if getter is None:
            raise ObserverError(
                "当前观察器不支持确认正在显示的会话，自动回复已中止",
                kind="identity_unknown",
            )
        current = getter()
        expected = self._target_identity().chat_id
        if current != expected:
            raise ObserverError(
                f"当前微信会话已切换（当前={current}，目标={expected}），自动回复已中止",
                kind="identity_unknown",
            )

    # -- 单轮 ---------------------------------------------------------------
    def poll_once(self) -> list[dict[str, Any]]:
        """执行一轮：观察 → 生成回复 → 发送。返回本轮产生的回复记录。"""
        if not self.state.running:
            return []
        if time.time() >= self.state.deadline:
            self.stop()
            return []

        self.state.poll_count += 1
        chat = self._target_identity()

        try:
            if not self.observer.is_login_valid():
                raise ObserverError("微信未处于有效登录状态", kind="login_expired")
            self._verify_current_chat()
            messages = self.observer.read_chat(chat)
        except ObserverError as exc:
            self.state.last_error = str(exc)
            self.state.last_error_kind = exc.kind
            self.store.add_event(None, f"auto_reply_error:{exc.kind}", str(exc))
            if exc.fatal:
                # 身份/登录类问题：立即停止整个自动回复，不带着不确定的上下文继续
                self.stop()
            return []

        results: list[dict[str, Any]] = []
        for message in messages:
            text = message.text.strip()
            if not text or message.is_from_me:
                continue
            if message.confidence < 0.45:
                continue
            if not self.store.mark_seen(message.dedup_key):
                continue  # 已处理过

            reply = self._generate(text, history=messages)
            if reply is None:
                continue
            results.append(reply)
            self.state.replied_count += 1

        return results

    def _generate(
        self, trigger_text: str, *, history: list[Any] | None = None
    ) -> dict[str, Any] | None:
        """调用模型生成回复正文。失败时记录事件并跳过。

        ``history`` 传入当前会话里可见的历史消息——人格提示词要求"回复前先
        理解整个对话，而不是只看最新一句话"，只给最后一句模型无法判断
        对方是在开玩笑还是在认真说事。
        """
        from .models import ChatContext, ObservedMessage

        chat = self._target_identity()
        trigger = ObservedMessage(chat=chat, text=trigger_text, timestamp=time.time())
        # 历史里排除触发消息本身，保留**双方**的话。
        # 人格提示词要求"回复前先理解整个对话"——只给对方的句子，模型
        # 无法判断对话进行到哪一步，容易过度保守地回"需要人工处理"。
        prior = [
            m
            for m in (history or [])
            if m.text.strip() != trigger_text
        ]
        context = ChatContext(chat=chat, trigger=trigger, history=prior)

        try:
            reply = self.router.generate(self.system_prompt, context)
        except Exception as exc:  # noqa: BLE001 - 模型异常不应中断整个循环
            self.state.last_error = str(exc)
            self.store.add_event(None, "auto_reply_model_error", str(exc))
            return None

        content = (reply.content or "").strip()
        if not content or content in {"[无需回复]", "[需要人工处理]"}:
            self.store.add_event(None, "auto_reply_skipped", content or "(空)")
            return None

        record = {
            "chat_id": chat.chat_id,
            "chat_name": chat.display_name,
            "trigger_text": trigger_text,
            "reply_text": content,
            "model": reply.model,
            "sent": False,
        }

        if self.sender is None:
            self.store.add_event(None, "auto_reply_drafted", content[:80])
            return record

        # 发送授权闸门：白名单 + 频率限制 + 紧急停止。
        # 不通过就**不发送**——但保留已生成的回复，让人能看到发生了什么。
        gate = self.gatekeeper
        if gate is not None:
            try:
                gate.check(chat.display_name)
            except SendNotAllowed as exc:
                self.state.last_error = str(exc)
                self.state.last_error_kind = exc.kind
                self.store.add_event(None, f"auto_reply_blocked:{exc.kind}", str(exc))
                record["blocked"] = exc.kind
                record["blocked_reason"] = str(exc)
                if exc.kind == "emergency_stop":
                    self.stop()
                return record

        # 发送前再核验一次当前会话——生成回复期间用户可能切走了
        try:
            self._verify_current_chat()
        except ObserverError as exc:
            self.state.last_error = str(exc)
            self.state.last_error_kind = exc.kind
            self.store.add_event(None, "auto_reply_send_blocked", str(exc))
            self.stop()
            return None

        try:
            result = self.sender(chat.chat_id, content)
        except Exception as exc:  # noqa: BLE001
            self.store.add_event(None, "auto_reply_send_error", str(exc))
            return record

        confirmed = bool(result.get("ok")) if isinstance(result, dict) else False
        record["sent"] = confirmed
        record["send_result"] = result
        if gate is not None:
            gate.record_send()
        self.store.add_event(
            None,
            "auto_reply_sent" if confirmed else "auto_reply_send_unknown",
            content[:80],
        )
        return record

    # -- 主循环 -------------------------------------------------------------
    def run(self, *, max_polls: int | None = None) -> None:
        """轮询直到到达时限、被停止或出错。"""
        if not self.state.running:
            self.start()
        polls = 0
        while self.state.running and time.time() < self.state.deadline:
            if max_polls is not None and polls >= max_polls:
                break
            polls += 1
            try:
                results = self.poll_once()
            except Exception as exc:  # noqa: BLE001 - 单轮异常不应终止整个会话
                self.state.last_error = str(exc)
                self.store.add_event(None, "auto_reply_poll_error", str(exc))
                results = []
            for record in results:
                marker = "已发送" if record.get("sent") else "已生成（未发送）"
                print(
                    f"[{time.strftime('%H:%M:%S')}] {marker}："
                    f"{record['trigger_text'][:30]} → {record['reply_text'][:60]}",
                    flush=True,
                )
            if max_polls is not None and polls >= max_polls:
                break
            time.sleep(self.interval)
        if self.state.running:
            self.stop()
