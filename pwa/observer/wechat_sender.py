"""微信 macOS 发送实现（AppleScript + 剪贴板）。

## 本机验证结论（WeChat 4.1.15 build 270115）

* **AX 树是空的**：主窗口 ``entire contents`` 返回 0 个元素，输入框不是
  AX 文本框。因此**不能用 AX 定位输入框**，只能靠屏幕坐标。
* **坐标定位可用**：``click at {x, y}`` 能把光标准确放进输入框。
* **四种输入方式都生效**（实测）：``keystroke`` 直接打字、``keystroke "v"
  using command down`` 粘贴、``key code 9 using command down``、以及走
  「编辑 → 粘贴」菜单项。中文内容一律走**剪贴板 + ⌘V**，因为
  ``keystroke`` 无法输入非 ASCII 字符。
* **发送用回车**：微信输入框里 ⏎ 直接发出。

## 安全约束

本模块是**唯一**能真正把消息发出去的地方，因此：

* 默认 ``dry_run=True``，只把内容填进输入框，**不按回车**。
* 发送前必须由调用方确认目标会话（由 :class:`SendGate` 负责核验）。
* 发送后返回结果，调用方负责核验；本模块自己不去判断"是否真的发出去了"，
  因为那需要再次截图比对，成本高且不可靠——交给上层决定。
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Any

from .wechat_macos import WeChatObserver

# 输入框在窗口内的归一化位置（实测 424x640 窗口，输入框在 y≈0.88）
INPUT_X_RATIO = 0.50
INPUT_Y_RATIO = 0.88


@dataclass
class SendResult:
    """发送结果。``ok=False`` 表示结果不确定，需要人工确认。"""

    ok: bool
    detail: str = ""
    typed_only: bool = False  # True 表示只填了输入框，没有按回车

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "detail": self.detail, "typed_only": self.typed_only}


class WeChatSender:
    """通过 AppleScript 操作微信窗口发送消息。"""

    def __init__(
        self,
        *,
        observer: WeChatObserver | None = None,
        dry_run: bool = True,
        osascript_bin: str = "osascript",
    ) -> None:
        self.observer = observer or WeChatObserver()
        self.dry_run = dry_run
        self.osascript_bin = osascript_bin

    # -- 底层 ---------------------------------------------------------------
    def _osa(self, script: str, *, timeout: int = 15) -> tuple[int, str, str]:
        try:
            result = subprocess.run(
                [self.osascript_bin, "-e", script],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return 124, "", f"AppleScript 超时: {exc}"
        return (
            result.returncode,
            result.stdout.decode(errors="ignore").strip(),
            result.stderr.decode(errors="ignore").strip(),
        )

    def _window_origin(self) -> tuple[int, int, int, int]:
        """返回微信主窗口在屏幕上的 (x, y, width, height)。"""
        import Quartz

        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID
        )
        candidates = []
        for win in windows or []:
            if win.get("kCGWindowOwnerName") not in {"微信", "WeChat"}:
                continue
            if win.get("kCGWindowLayer") != 0:
                continue
            bounds = win.get("kCGWindowBounds") or {}
            w, h = bounds.get("Width", 0), bounds.get("Height", 0)
            if w < 200 or h < 300:
                continue
            candidates.append((w * h, bounds))
        if not candidates:
            raise RuntimeError("找不到微信主窗口")
        candidates.sort(key=lambda item: -item[0])
        b = candidates[0][1]
        return int(b["X"]), int(b["Y"]), int(b["Width"]), int(b["Height"])

    # -- 操作 ---------------------------------------------------------------
    def _focus_input(self) -> None:
        """把光标放进输入框。"""
        x, y, w, h = self._window_origin()
        ix = x + int(INPUT_X_RATIO * w)
        iy = y + int(INPUT_Y_RATIO * h)
        code, _out, err = self._osa(
            f'''
            tell application "System Events"
                tell process "微信"
                    click at {{{ix}, {iy}}}
                end tell
            end tell
            '''
        )
        if code != 0:
            raise RuntimeError(f"无法聚焦微信输入框: {err}")
        time.sleep(0.4)

    def _set_clipboard(self, text: str) -> None:
        """把文本放进剪贴板。

        用 AppleScript 的 ``set the clipboard to`` 而不是 ``pbcopy``，因为
        前者不需要处理换行和特殊字符的转义，也不需要额外的子进程。
        """
        escaped = (
            text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        )
        code, _out, err = self._osa(f'set the clipboard to "{escaped}"')
        if code != 0:
            raise RuntimeError(f"写入剪贴板失败: {err}")

    def _paste(self) -> None:
        code, _out, err = self._osa(
            'tell application "System Events" to keystroke "v" using command down'
        )
        if code != 0:
            raise RuntimeError(f"粘贴失败: {err}")

    def _press_return(self) -> None:
        code, _out, err = self._osa(
            'tell application "System Events" to key code 36'
        )
        if code != 0:
            raise RuntimeError(f"发送回车失败: {err}")

    def _clear_input(self) -> None:
        """清空输入框（⌘A 然后删除）。"""
        self._osa('tell application "System Events" to keystroke "a" using command down')
        time.sleep(0.15)
        self._osa('tell application "System Events" to key code 51')

    # -- 对外 ---------------------------------------------------------------
    def send(self, chat_id: str, text: str) -> dict[str, Any]:
        """把 ``text`` 发到 ``chat_id`` 对应的会话。

        ``chat_id`` 只用于日志/校验记录——真正的会话核对由 :class:`SendGate`
        在调用之前完成（比对标题栏读出的当前会话）。

        ``dry_run=True`` 时只填入输入框，不按回车。
        """
        text = (text or "").strip()
        if not text:
            return SendResult(ok=False, detail="发送内容为空").as_dict()

        self.observer._activate()  # noqa: SLF001 - 发送必须作用于前台窗口
        self._focus_input()
        self._clear_input()
        time.sleep(0.2)
        self._set_clipboard(text)
        time.sleep(0.2)
        self._paste()
        time.sleep(0.6)

        if self.dry_run:
            return SendResult(
                ok=True, detail="已填入输入框（dry-run，未发送）", typed_only=True
            ).as_dict()

        self._press_return()
        time.sleep(0.8)
        return SendResult(ok=True, detail="已发送").as_dict()


def build_sender(*, dry_run: bool = True) -> WeChatSender:
    """构造发送器。``dry_run`` 默认开启，必须显式关闭才会真正发送。"""
    return WeChatSender(dry_run=dry_run)
