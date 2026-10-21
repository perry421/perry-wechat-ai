"""统一的数据结构：模型供应商与业务逻辑之间的契约。"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Difficulty(str, Enum):
    """任务难度。路由依据之一。"""

    TRIVIAL = "trivial"  # 打招呼、表情回应
    SIMPLE = "simple"  # 简单问答、短确认
    COMPLEX = "complex"  # 需要上下文/推理/长文


class ChatKind(str, Enum):
    CONTACT = "contact"
    GROUP = "group"


class DraftStatus(str, Enum):
    PENDING = "pending"  # 待审核
    APPROVED = "approved"  # 已批准（尚未发送）
    CANCELLED = "cancelled"  # 已取消
    SENT = "sent"  # 已发送
    FAILED = "failed"  # 处理失败
    UNKNOWN = "unknown"  # 状态未知（例如发送结果不确定）


@dataclass(frozen=True)
class ChatIdentity:
    """会话身份。用于校验"看到的"和"要回的"是不是同一个会话。"""

    chat_id: str  # 稳定标识：群聊用 wxid_xxx，联系人用规范化后的名称
    display_name: str  # 界面显示名
    kind: ChatKind = ChatKind.CONTACT
    is_group: bool = False

    @property
    def fingerprint(self) -> str:
        """身份指纹：用于变更检测。名称或类型变化会导致指纹变化。"""
        raw = f"{self.kind.value}:{self.chat_id}".encode()
        return hashlib.sha256(raw).hexdigest()[:16]


def chat_id_for(display_name: str, is_group: bool = False) -> str:
    """从显示名派生稳定 chat_id。

    微信 4.x 不暴露 wxid，因此用规范化后的显示名作为标识。群聊带 ``g:``
    前缀，避免和同名联系人碰撞。

    这是**唯一**的派生入口：观察器读到的会话、自动回复的目标会话必须用
    同一套规则，否则发送前的会话核验会永远判定"已切换"。
    """
    normalized = re.sub(r"\s+", "", display_name)
    prefix = "g:" if is_group else "name:"
    return prefix + normalized.lower()


@dataclass(frozen=True)
class ObservedMessage:
    """从观察适配器读到的单条可见消息。"""

    chat: ChatIdentity
    text: str
    timestamp: float
    is_from_me: bool = False
    confidence: float = 1.0
    sender: str = ""  # 群聊中的发送者昵称
    anchor: str = ""  # 前一条可见消息的稳定指纹，由观察器给出

    @property
    def dedup_key(self) -> str:
        """去重键：会话 + 发送者 + 归一化文本 + 前一条消息指纹。

        **不含时间戳**。早期版本用 30 秒时间桶，实测在 20 秒轮询间隔下
        会跨桶——同一条消息被当成新消息，重复回复。改用"前一条消息指纹"
        作为锚点后：同一条消息无论隔多久被读到，前一条不变 → 键不变；
        对方连发两条内容相同的消息，第二条的前一条不同 → 键不同，不会漏。

        观察器未提供 anchor 时退化为纯内容哈希（同一内容视为同一条）。
        """
        normalized = "".join(self.text.split())
        raw = f"{self.chat.chat_id}|{self.sender}|{normalized}|{self.anchor}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass
class LLMReply:
    """模型回复的统一输出。"""

    content: str
    model: str
    endpoint: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    input_cost: float = 0.0
    output_cost: float = 0.0
    latency_ms: int = 0
    degraded: bool = False  # 是否走了降级路径
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_cost(self) -> float:
        return round(self.input_cost + self.output_cost, 8)

    @property
    def is_empty(self) -> bool:
        return not self.content.strip()


@dataclass
class ChatContext:
    """交给模型的上下文。聊天内容是不可信输入，见 prompts 模块的隔离说明。"""

    chat: ChatIdentity
    trigger: ObservedMessage
    history: list[ObservedMessage] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat.chat_id,
            "display_name": self.chat.display_name,
            "is_group": self.chat.is_group,
            "trigger": {
                "text": self.trigger.text,
                "sender": self.trigger.sender,
                "is_from_me": self.trigger.is_from_me,
            },
            "history": [
                {
                    "text": m.text,
                    "sender": m.sender,
                    # 用"我说的话"/"对方说的话"标注，比裸布尔值更容易被模型读懂
                    "who": "我说的话" if m.is_from_me else "对方说的话",
                    "is_from_me": m.is_from_me,
                }
                for m in self.history
            ],
        }


class ProviderError(Exception):
    """供应商调用失败。子类区分降级策略。"""

    def __init__(self, message: str, *, kind: str = "unknown") -> None:
        super().__init__(message)
        self.kind = kind  # timeout | rate_limit | quota | unavailable | bad_output | unknown

    @property
    def can_degrade(self) -> bool:
        return self.kind in {"timeout", "rate_limit", "quota", "unavailable"}


class ObserverError(Exception):
    """观察适配器失败。kind 用于决定是否立即停止自动处理。"""

    def __init__(self, message: str, *, kind: str = "unknown") -> None:
        super().__init__(message)
        self.kind = kind
        # 身份不明、OCR 不可靠、窗口切换、登录失效 → 必须停止
        self.fatal = kind in {
            "identity_unknown",
            "ocr_unreliable",
            "window_changed",
            "login_expired",
            "permission_denied",
        }


def now_ts() -> float:
    return time.time()
