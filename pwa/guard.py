"""发送限流与授权闸门。

自动回复模式连续运行 6 小时，最怕的是"跑飞"：模型判断失误导致连环
回复，或一条消息被重复发送。规格明确要求：

* 发送频率限制；
* 紧急停止；
* 目标会话白名单。

这三项都做成**独立于运行器**的组件，这样人工审核模式和自动回复模式
可以共用同一套闸门，也便于单独测试。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


class SendNotAllowed(Exception):
    """发送被闸门拦截。``reason`` 说明原因。"""

    def __init__(self, reason: str, *, kind: str = "blocked") -> None:
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


@dataclass
class RateLimiter:
    """滑动窗口限流器。

    默认：60 秒内最多 5 条。超过就拒绝发送（而不是排队等待）——自动回复
    的场景里，"积压"意味着延迟发送一条已经过时的回复，比不发更糟。
    """

    max_sends: int = 5
    window_seconds: float = 60.0
    _timestamps: deque[float] = field(default_factory=deque, init=False)

    def allow(self, now: float | None = None) -> bool:
        """记录一次发送并返回是否允许。"""
        moment = time.time() if now is None else now
        cutoff = moment - self.window_seconds
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.max_sends:
            return False
        self._timestamps.append(moment)
        return True

    def check(self, now: float | None = None) -> bool:
        """只检查不记录。"""
        moment = time.time() if now is None else now
        cutoff = moment - self.window_seconds
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()
        return len(self._timestamps) < self.max_sends

    @property
    def sent_in_window(self) -> int:
        return len(self._timestamps)

    def reset(self) -> None:
        self._timestamps.clear()


@dataclass
class SendGatekeeper:
    """发送前的授权闸门。

    每一项检查都可以单独开关，方便按场景收紧：

    * ``allow_chats``——目标会话白名单。空列表表示**不允许任何会话**
      （而不是允许所有），这是"默认安全"的取向。
    * ``rate_limiter``——频率限制。
    * ``emergency_stopped``——紧急停止开关，一旦置位，所有发送都被拒绝。
    """

    allow_chats: tuple[str, ...] = ()
    rate_limiter: RateLimiter = field(default_factory=RateLimiter)
    emergency_stopped: bool = False

    def check(self, chat_name: str) -> None:
        """检查是否允许向 ``chat_name`` 发送。不允许则抛 :class:`SendNotAllowed`。"""
        if self.emergency_stopped:
            raise SendNotAllowed("已紧急停止，所有发送被拒绝", kind="emergency_stop")

        if not self.allow_chats:
            raise SendNotAllowed(
                "目标会话白名单为空，不允许发送任何会话", kind="no_allowlist"
            )

        normalized = chat_name.strip().lower()
        allowed = {name.strip().lower() for name in self.allow_chats if name.strip()}
        if normalized not in allowed:
            raise SendNotAllowed(
                f"会话 {chat_name!r} 不在白名单内（允许：{', '.join(sorted(allowed))}）",
                kind="not_allowlisted",
            )

        if not self.rate_limiter.check():
            raise SendNotAllowed(
                f"发送频率超限（{self.rate_limiter.window_seconds:.0f} 秒内最多 "
                f"{self.rate_limiter.max_sends} 条）",
                kind="rate_limited",
            )

    def record_send(self) -> None:
        """发送成功后记录一次占用。"""
        self.rate_limiter.allow()

    def trip(self) -> None:
        """紧急停止。"""
        self.emergency_stopped = True

    def reset(self) -> None:
        """解除紧急停止并清空限流计数。"""
        self.emergency_stopped = False
        self.rate_limiter.reset()
