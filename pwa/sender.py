"""发送模块（默认关闭，带安全门）。

安全门设计：

1. **默认不启用**。必须同时满足：
   - 环境变量 ``PWA_ENABLE_SEND=true``；
   - 草稿状态为 ``approved``（即用户已对**这一条**消息明确批准）。
2. 发送前**重新核验**：
   - 微信当前处于登录状态；
   - 当前微信窗口的会话与草稿的会话一致（防止窗口被切走发错人）；
   - 触发的消息仍然出现在当前会话中（防止上下文已经变了）；
   - 草稿内容未被改动，或改动后已重新展示给用户确认。
3. 批准一条消息**不代表**批准后续消息。每条都要单独走 approve → send。
4. 发送后**验证结果**。不确定时标记 ``unknown``，**不自动重试**，
   避免重复发送。

如果微信客户端无法可靠完成上述核验，就拒绝发送，让用户用复制功能自行发送。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .models import DraftStatus, ObserverError
from .observer.base import Observer
from .store import Store


class SendBlocked(Exception):
    """发送被安全门拦截。

    ``kind`` 是稳定的机器可读错误码，``reason`` 是给人看的说明。
    界面/接口应该按 ``kind`` 决定展示文案，不要直接把 ``reason`` 透出去——
    ``reason`` 里可能带内部细节（如异常原文）。
    """

    def __init__(self, reason: str, *, kind: str = "blocked") -> None:
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


@dataclass
class SendVerification:
    ok: bool
    reason: str = ""
    checks: dict[str, bool] | None = None
    kind: str = "blocked"


class SendGate:
    """发送前的核验与执行。"""

    def __init__(
        self,
        *,
        store: Store,
        observer: Observer,
        enable_send: bool = False,
        sender: Any | None = None,
        allow_chats: tuple[str, ...] = (),
        rate_limiter: Any | None = None,
    ) -> None:
        self.store = store
        self.observer = observer
        self.enable_send = enable_send
        # sender 是实际执行发送动作的回调，默认没有——即不提供任何发送能力。
        self.sender = sender
        # 白名单为空表示**不允许任何会话**（默认安全）。人工审核模式下
        # 目标会话由 PWA_ALLOWED_CHATS 决定。
        self.allow_chats = tuple(c.strip() for c in allow_chats if c.strip())
        self.rate_limiter = rate_limiter

    # -- 核验 ---------------------------------------------------------------
    def verify(self, draft_id: str) -> SendVerification:
        """在发送前重新核验一切。返回是否通过。"""
        checks: dict[str, bool] = {}

        draft = self.store.get_draft(draft_id)
        if draft is None:
            return SendVerification(False, "草稿不存在", checks, kind="draft_not_found")
        checks["draft_exists"] = True

        if not self.enable_send:
            return SendVerification(
                False, "发送能力未启用（PWA_ENABLE_SEND != true）", checks, kind="send_disabled"
            )

        if self.sender is None:
            return SendVerification(
                False,
                "没有可用的发送实现（sender 未配置），请使用复制功能自行发送",
                checks,
                kind="no_sender",
            )

        # 目标会话白名单：空 → 一律拒绝
        if not self.allow_chats:
            return SendVerification(
                False,
                "目标会话白名单为空，不允许发送任何会话",
                checks,
                kind="allowlist_empty",
            )
        draft_chat_name = (draft["chat_name"] or "").strip().lower()
        allowed = {name.strip().lower() for name in self.allow_chats}
        checks["chat_allowlisted"] = draft_chat_name in allowed
        if draft_chat_name not in allowed:
            return SendVerification(
                False,
                f"草稿会话 {draft['chat_name']!r} 不在白名单内"
                f"（允许：{', '.join(sorted(allowed))}）",
                checks,
                kind="not_allowlisted",
            )

        # 发送频率限制
        if self.rate_limiter is not None and not self.rate_limiter.check():
            return SendVerification(
                False,
                f"发送频率超限（{self.rate_limiter.window_seconds:.0f} 秒内最多 "
                f"{self.rate_limiter.max_sends} 条）",
                checks,
                kind="rate_limited",
            )

        if draft["status"] != DraftStatus.APPROVED.value:
            return SendVerification(
                False, "草稿未处于已批准状态", checks, kind="not_approved"
            )

        # 1. 登录状态
        try:
            login_ok = self.observer.is_login_valid()
        except ObserverError as exc:
            return SendVerification(
                False, "无法确认微信登录状态", checks, kind=exc.kind
            )
        checks["login_valid"] = bool(login_ok)
        if not login_ok:
            return SendVerification(
                False, "微信未处于有效登录状态", checks, kind="login_expired"
            )

        # 2. 当前窗口会话必须仍是草稿对应的会话
        try:
            current = self._current_chat_id()
        except ObserverError as exc:
            return SendVerification(
                False, "无法确认当前会话", checks, kind=exc.kind
            )
        checks["chat_unchanged"] = current == draft["chat_id"]
        if current != draft["chat_id"]:
            return SendVerification(
                False,
                "当前微信会话已切换，已中止发送",
                checks,
                kind="chat_changed",
            )

        # 3. 触发消息仍应出现在当前会话中
        try:
            messages = self.observer.read_chat(
                self._chat_identity(draft), limit=30
            )
        except ObserverError as exc:
            return SendVerification(
                False, "无法读取当前会话", checks, kind=exc.kind
            )
        trigger = (draft["trigger_text"] or "").strip()
        matched = any(m.text.strip() == trigger for m in messages)
        checks["trigger_present"] = matched
        if not matched:
            return SendVerification(
                False,
                "触发消息已不在当前会话中（上下文可能已变化），已中止发送",
                checks,
                kind="trigger_missing",
            )

        return SendVerification(True, "核验通过", checks, kind="ok")

    # -- 发送 ---------------------------------------------------------------
    def send(self, draft_id: str, *, approved_text: str | None = None) -> dict[str, Any]:
        """发送一条已批准的草稿。"""
        draft = self.store.get_draft(draft_id)
        if draft is None:
            raise SendBlocked("草稿不存在", kind="draft_not_found")

        verification = self.verify(draft_id)
        self.store.add_event(
            draft_id,
            "send_verify",
            f"ok={verification.ok} kind={verification.kind} {verification.reason} "
            f"checks={verification.checks}",
        )
        if not verification.ok:
            self.store.update_draft(
                draft_id, status=DraftStatus.FAILED.value, error=verification.reason
            )
            raise SendBlocked(verification.reason, kind=verification.kind)

        text = (approved_text or draft["reply_text"] or "").strip()
        if not text:
            self.store.update_draft(
                draft_id, status=DraftStatus.FAILED.value, error="回复内容为空"
            )
            raise SendBlocked("回复内容为空", kind="empty_reply")

        assert self.sender is not None
        try:
            result = self.sender(draft["chat_id"], text)
        except Exception as exc:  # noqa: BLE001 - 发送失败必须被捕获并记录
            # 详细原因写进本地事件表（不外传）；对外只给固定文案。
            self.store.update_draft(
                draft_id, status=DraftStatus.FAILED.value, error="发送失败，请查看处理记录"
            )
            self.store.add_event(draft_id, "send_error", f"{type(exc).__name__}: {exc}")
            raise SendBlocked("发送失败，请查看处理记录", kind="send_error") from exc

        if self.rate_limiter is not None:
            self.rate_limiter.allow()

        confirmed = _is_confirmed(result)
        # dry-run 只把内容填进输入框，没有按回车——绝不能当成"已发送"。
        typed_only = bool(isinstance(result, dict) and result.get("typed_only"))
        if typed_only:
            status = DraftStatus.APPROVED.value
            error = "已填入微信输入框（dry-run，未按回车发送），请在微信里确认后手动发出"
            sent_at = None
        else:
            status = DraftStatus.SENT.value if confirmed else DraftStatus.UNKNOWN.value
            error = "" if confirmed else "发送结果不确定，请人工确认是否已发出"
            sent_at = time.time()
        self.store.update_draft(
            draft_id,
            status=status,
            sent_at=sent_at,
            error=error,
        )
        self.store.add_event(
            draft_id,
            "sent" if confirmed else "send_unknown",
            f"result={result!r}",
        )
        return {"status": status, "result": result, "typed_only": typed_only}

    # -- 辅助 ---------------------------------------------------------------
    def _current_chat_id(self) -> str:
        """读出当前微信正在显示的会话 id。"""
        getter = getattr(self.observer, "current_chat_id", None)
        if getter is not None:
            return getter()
        # 退化路径：用授权会话列表的最后一个（微信选中项通常高亮）不可靠，
        # 因此默认拒绝——宁可让用户复制，也不能发错人。
        raise ObserverError(
            "当前观察器不支持确认正在显示的会话，无法保证发对人", kind="identity_unknown"
        )

    def _chat_identity(self, draft: dict[str, Any]):
        from .models import ChatIdentity, ChatKind

        return ChatIdentity(
            chat_id=draft["chat_id"],
            display_name=draft["chat_name"],
            kind=ChatKind.GROUP if draft["is_group"] else ChatKind.CONTACT,
            is_group=bool(draft["is_group"]),
        )


def _is_confirmed(result: Any) -> bool:
    """判断发送结果是否可确认。

    只有明确的成功信号才算确认。任何异常、None 或无法识别的返回值
    都按"不确定"处理，宁可让用户手动确认，也不自动重试。
    """
    if result is True:
        return True
    if isinstance(result, dict):
        return bool(result.get("ok") is True or result.get("sent") is True)
    if isinstance(result, str):
        return result.strip().lower() in {"ok", "sent", "success", "true"}
    return False
