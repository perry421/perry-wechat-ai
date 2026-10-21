"""观察适配器接口。

观察层只做一件事：把"当前微信界面上可见的内容"读出来，变成结构化的
:class:`ObservedMessage`。它不生成回复，也不发送任何东西。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import ChatIdentity, ObservedMessage


class Observer(ABC):
    """只读观察器。"""

    name: str = "observer"

    @abstractmethod
    def list_chats(self) -> list[ChatIdentity]:
        """列出当前可见的会话（聊天列表）。"""

    @abstractmethod
    def read_chat(self, chat: ChatIdentity, limit: int = 20) -> list[ObservedMessage]:
        """读取指定会话当前可见的消息。

        实现必须只返回**屏幕上真实可见**的消息。
        """

    @abstractmethod
    def is_login_valid(self) -> bool:
        """微信是否处于已登录、可正常使用的状态。"""

    def close(self) -> None:  # pragma: no cover - 默认无资源
        return None
