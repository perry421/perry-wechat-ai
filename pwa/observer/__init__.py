"""观察适配器。"""

from .base import Observer
from .wechat_macos import MockObserver, WeChatObserver

__all__ = ["Observer", "WeChatObserver", "MockObserver"]
