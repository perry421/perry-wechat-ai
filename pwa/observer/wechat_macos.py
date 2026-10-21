"""微信 macOS 观察适配器（只读）。

## 本机验证结论（WeChat 4.1.15 build 270115，macOS 27.2.0）

WeChat 4.x 是 Electron 重写版（内部模块名 ``xwechat_mac``，运行 Chromium
GPU/Renderer helper）。实测：

* **辅助功能（AX）树不可用于读消息**：主聊天窗口的 AX 树只有 5 个元素
  （全是窗口装饰按钮），图片/视频浮窗 22 个，**不含任何聊天文字、联系人名
  或消息列表**。所以 AX 只能用来确认"哪个窗口在前台"，不能用来读内容。
* **窗口截图 + 原生 Vision OCR 可行**：``screencapture -l <windowID>``
  能拿到**只属于该窗口**的干净画面（不含被遮挡内容，也不含其它应用），
  OCR 用 macOS 原生 ``VNRecognizeTextRequest``，离线、中文可用、无模型下载。
* **必须先把微信置于前台**：微信在后台时，``CGWindowListCopyWindowInfo``
  返回的 bounds 是脏值（实测 424x640 变成 45x107），``screencapture -l``
  也会退化成残缺画面（90x214）。激活后一切正常。
* **消息归属可用气泡颜色判定**：微信 4.x 深色模式下，我方气泡是绿色
  ``RGB(53, 209, 140)``，对方气泡是灰色 ``RGB(47, 47, 48)``。用颜色采样
  就能判断一条消息是谁发的，比靠 x 坐标猜更稳。

## 坐标系约定（重要）

Vision 的 ``boundingBox`` **原点在左下角**。本模块统一把它转成
**自上而下**的归一化坐标（``top`` / ``bottom``），所有区域判断都用转换后
的坐标，避免和直觉相反的上下颠倒。

## 已知限制

* 微信窗口较窄时左侧聊天列表会折叠成**纯头像**，此时读不出会话名。
  本模块因此不依赖会话列表定位，而是直接操作**当前已打开的**目标会话，
  并用标题栏文字校验当前会话身份。
* 气泡颜色判定对**浅色模式**无效（浅色下对方气泡接近背景白）。当前实现
  只覆盖深色模式，浅色模式下会判不出归属并跳过，而不是猜。
* OCR 结果依赖字号和渲染状态，识别不可靠是常态。低置信度会被上游当作
  致命错误处理，而不是猜测。

本模块**不**读取微信本地数据库、**不**提取登录凭据、**不**逆向协议，
只做屏幕像素观察。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..models import (
    ChatIdentity,
    ChatKind,
    ObservedMessage,
    ObserverError,
    chat_id_for,
)
from .base import Observer

# 微信主窗口在 CG 层面的 owner name 是中文 "微信"
WECHAT_OWNER_NAMES = {"微信", "WeChat"}
WECHAT_WINDOW_TITLE = "微信"

# 登录失效 / 异常界面上的关键字
_LOGIN_MARKERS = (
    "登录",
    "扫码登录",
    "微信登录",
    "登录微信",
    "新设备登录",
    "安全验证",
)

# 会话列表条目右侧的时间戳样式
_TIME_RE = re.compile(
    r"^(\d{1,2}:\d{2}|\d{1,2}月\d{1,2}日|昨天|前天|星期[一二三四五六日天]"
    r"|上午\d{1,2}:\d{2}|下午\d{1,2}:\d{2}|\d{4}年\d{1,2}月\d{1,2}日)$"
)

# 群聊消息前缀样式："张三: 内容"
_GROUP_SENDER_RE = re.compile(r"^([^:：\s]{1,20})[:：]\s*(.+)$", re.DOTALL)

# 气泡颜色（深色模式实测值）。容差刻意收紧：背景色 (30,30,31) 与对方气泡
# (47,47,48) 只差 17，放宽会把背景误判成对方气泡。
COLOR_MINE = (53, 209, 140)  # 我方绿色气泡
COLOR_OTHER = (47, 47, 48)  # 对方灰色气泡
COLOR_TOLERANCE = 12

# 窗口几何分区（自上而下的归一化比例，来自实测截图 848x1280）
#
#   0.000 ──────────  标题栏：会话名在这里，右侧是功能按钮
#   0.075 ──────────  聊天区上沿（第一条消息气泡顶部的留白）
#   0.777 ──────────  输入框上边框（实测水平细线，归一化 993/1280）
#   0.777 ──────────  输入区：会被 OCR 读成 "发送" / 输入内容，必须排除
#
# 输入框上边框用**动态检测**：整行横向是比背景亮一点的灰线。它比气泡更靠
# 下，作为聊天区下沿比写死比例可靠（窗口尺寸变化时不会失效）。
TITLE_BAND = (0.0, 0.075)
INPUT_BORDER_MIN_Y = 0.55  # 边框不可能高于此（否则聊天区没空间）
INPUT_BORDER_MAX_Y = 0.92
CHAT_AREA_TOP = 0.075
CHAT_AREA_BOTTOM_FALLBACK = 0.86

# 左侧会话列表占窗口宽度的比例（实测侧边栏约 0.135）
SIDEBAR_MAX_X = 0.16

# 头像区域：微信会把头像里的图片文字识别出来（实测 "PERRY"），
# 必须排除，否则会被当成消息。
AVATAR_MIN_X = 0.16
AVATAR_MAX_X = 0.32

# 合并同一气泡内多行文字的判定参数
LINE_MERGE_GAP_FACTOR = 1.8  # 行间距 < 该系数 × 行高 → 视为同一气泡
LINE_MERGE_LEFT_TOL = 0.03  # 左缘对齐容差（归一化）

# 微信界面上的**按钮文字**，不是对方发的消息。语音条上的「转文字」实测
# 底色是 (41,41,42)，与对方气泡 (47,47,48) 只差 6——落在气泡色容差内，
# 会被误判成对方消息并触发一次毫无意义的回复。
#
# 判据用**位置 + 词表**：这些按钮固定出现在语音条右侧，且整词匹配。
# 只拦确定的几个词，不做模糊匹配——误杀一条真消息比漏一个按钮糟糕得多。
UI_BUTTON_TEXTS = frozenset(
    {
        "转文字",
        "转语音",
        "翻译",
        "引用",
        "转发",
        "收藏",
        "删除",
        "撤回",
        "多选",
        "复制",
        "搜索",
        "发送",
        "语音输入",
        "边写边译",
    }
)

# 语音时长标记（3" / 12''），属于语音条本身
_VOICE_DURATION_RE = re.compile(r"^\d{1,3}[\"']{1,2}$")



@dataclass
class OCRLine:
    text: str
    confidence: float
    x: float  # 归一化 0-1，原点左上
    y: float  # 文字行**顶部**的归一化纵坐标，原点左上
    width: float
    height: float
    owner: str = "unknown"  # mine | other | unknown，气泡颜色判定结果

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height


class WeChatObserver(Observer):
    """基于窗口截图 + 原生 Vision OCR 的只读观察器。"""

    name = "wechat-macos"

    def __init__(
        self,
        *,
        min_confidence: float = 0.45,
        window_title: str = WECHAT_WINDOW_TITLE,
        max_list_items: int = 30,
        activate: bool = True,
    ) -> None:
        self.min_confidence = min_confidence
        self.window_title = window_title
        self.max_list_items = max_list_items
        self.activate = activate
        self._last_window_id: int | None = None

    # -- 窗口定位 -----------------------------------------------------------
    def _quartz(self):
        try:
            import Quartz  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise ObserverError(
                "缺少 pyobjc-framework-Quartz，无法访问窗口列表", kind="permission_denied"
            ) from exc
        return Quartz

    def _all_windows(self) -> list[dict[str, Any]]:
        Quartz = self._quartz()
        try:
            windows = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID
            )
        except Exception as exc:  # pragma: no cover
            raise ObserverError(f"读取窗口列表失败: {exc}", kind="unknown") from exc
        return list(windows or [])

    def _wechat_windows(self) -> list[tuple[int, dict[str, Any]]]:
        """列出微信 layer-0 窗口，按面积从大到小排序。

        注意：``kCGWindowListOptionAll`` 返回的 bounds 在窗口动画期间可能
        是**过期**坐标，所以这里只用来取 window id，不用来定位。
        """
        result: list[tuple[int, dict[str, Any]]] = []
        for win in self._all_windows():
            if win.get("kCGWindowOwnerName") not in WECHAT_OWNER_NAMES:
                continue
            if win.get("kCGWindowLayer") != 0:
                continue
            bounds = win.get("kCGWindowBounds") or {}
            if bounds.get("Width", 0) < 200 or bounds.get("Height", 0) < 300:
                continue
            result.append((int(win["kCGWindowNumber"]), bounds))
        result.sort(key=lambda item: -(item[1]["Width"] * item[1]["Height"]))
        return result

    def _find_window(self) -> int:
        """返回微信主窗口的 window id。"""
        candidates = self._wechat_windows()
        if not candidates:
            raise ObserverError(
                "没有找到微信主窗口（可能未启动、已最小化或处于登录页）",
                kind="window_changed",
            )
        return candidates[0][0]

    def _activate(self) -> None:
        """把微信置于前台。

        这是**必须的**：微信在后台时 CG 返回的窗口 bounds 是脏值
        （实测 424x640 变成 45x107），``screencapture -l`` 也会退化成
        残缺画面（90x214）。
        """
        result = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to tell process "微信" to set frontmost to true',
            ],
            capture_output=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            raise ObserverError(
                f"无法激活微信窗口（可能缺少辅助功能权限）: {result.stderr.decode()[:200]}",
                kind="permission_denied",
            )
        # 等窗口真正前置并完成显示
        for _ in range(10):
            time.sleep(0.4)
            if self._is_frontmost():
                return
        raise ObserverError("微信窗口未能切换到前台", kind="window_changed")

    def _is_frontmost(self) -> bool:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to get name of first process whose frontmost is true',
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return False
        return result.stdout.decode(errors="ignore").strip() in WECHAT_OWNER_NAMES

    # -- 截图 ---------------------------------------------------------------
    def capture_window(self) -> Any:
        """截取微信窗口本身，返回 (CGImage, 临时PNG路径)。

        使用 ``screencapture -l <windowID>``：它只拍指定窗口的内容，
        不含遮挡物，也不含其它应用，隐私上优于整屏截图。
        """
        if self.activate:
            self._activate()
        wid = self._find_window()

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            path = Path(tmp.name)

        result = subprocess.run(
            ["screencapture", "-x", "-o", "-l", str(wid), str(path)],
            capture_output=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0 or not path.exists() or path.stat().st_size == 0:
            path.unlink(missing_ok=True)
            raise ObserverError(
                f"窗口截图失败（可能缺少屏幕录制权限）: {result.stderr.decode()[:200]}",
                kind="permission_denied",
            )

        try:
            import Quartz  # noqa: PLC0415
            from Foundation import NSURL  # noqa: PLC0415

            url = NSURL.fileURLWithPath_(str(path))
            source = Quartz.CGImageSourceCreateWithURL(url, None)
            if source is None:
                raise ObserverError("截图无法解析", kind="ocr_unreliable")
            image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
            if image is None:
                raise ObserverError("截图无法解析", kind="ocr_unreliable")
        except ImportError as exc:
            path.unlink(missing_ok=True)
            raise ObserverError(
                "缺少 pyobjc-framework-Quartz，无法读取截图", kind="permission_denied"
            ) from exc

        self._last_window_id = wid
        return image, path

    # -- OCR ----------------------------------------------------------------
    def ocr(self, image: Any) -> list[OCRLine]:
        """用 macOS 原生 Vision 识别文字。

        返回的坐标已从 Vision 的**左下原点**转换成**自上而下**。
        """
        try:
            import objc
            import Vision
        except ImportError as exc:
            raise ObserverError(
                "缺少 pyobjc-framework-Vision，无法进行本地 OCR", kind="permission_denied"
            ) from exc

        collected: list[OCRLine] = []

        def handler(request: Any, error: Any) -> None:
            for observation in request.results() or []:
                candidates = observation.topCandidates_(1)
                if not candidates or len(candidates) == 0:
                    continue
                best = candidates[0]
                conf = float(best.confidence())
                text = str(best.string())
                if not text.strip():
                    continue
                box = observation.boundingBox()
                if box is None:
                    continue
                # Vision 原点在左下 → 翻转成自上而下的 y
                top = 1.0 - (box.origin.y + box.size.height)
                collected.append(
                    OCRLine(
                        text=text.strip(),
                        confidence=conf,
                        x=float(box.origin.x),
                        y=top,
                        width=float(box.size.width),
                        height=float(box.size.height),
                    )
                )

        request = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(
            objc.python_method(handler)
        )
        request.setRecognitionLanguages_(["zh-Hans", "en-US"])
        request.setRecognitionLevel_(0)  # Accurate
        request.setUsesLanguageCorrection_(True)

        handler_obj = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
            image, None
        )
        ok, error = handler_obj.performRequests_error_([request], None)
        if not ok:
            raise ObserverError(f"OCR 执行失败: {error}", kind="ocr_unreliable")
        return collected

    # -- 颜色判定 -----------------------------------------------------------
    @staticmethod
    def _load_pixels(path: Path) -> Any:
        """把截图加载成 PIL Image，供像素采样使用。"""
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover
            raise ObserverError("缺少 Pillow，无法判定气泡颜色", kind="unknown") from exc
        return Image.open(path).convert("RGB")

    def _bubble_color(
        self, img: Any, line: OCRLine, size: tuple[int, int]
    ) -> str:
        """采样气泡背景色，返回 ``mine`` / ``other`` / ``unknown``。

        采样点从文字行**左缘向左**取一段（实测气泡在文字左侧延伸约 15px）。
        采用**多数票**，避免单个像素受抗锯齿或阴影干扰。
        """
        width, height = size
        y = int(line.center_y * height)
        if not (0 <= y < height):
            return "unknown"
        x_start = int(line.x * width)

        votes: list[tuple[int, int, int]] = []
        for dx in range(2, 22, 4):
            x = x_start - dx
            if x < 0:
                break
            votes.append(img.getpixel((x, y)))

        if not votes:
            return "unknown"

        mine_hits = sum(1 for c in votes if _close(c, COLOR_MINE))
        other_hits = sum(1 for c in votes if _close(c, COLOR_OTHER))
        if mine_hits > other_hits and mine_hits > 0:
            return "mine"
        if other_hits > mine_hits and other_hits > 0:
            return "other"
        return "unknown"

    def _is_clipped_bubble(
        self, img: Any, line: OCRLine, size: tuple[int, int], owner: str
    ) -> bool:
        """判断这条消息是否因为滚动被标题栏裁掉了一部分。

        微信聊天区滚动时，顶部的气泡会被标题栏**切掉上半截**。OCR 仍会读出
        一行字，但内容是残缺的——实测「你能回消息不」被读成「体能口泪思小」。
        拿这种残缺内容去触发回复是危险的，必须跳过。

        判据：完整气泡在文字行**上方**留有约 22px 的气泡色内边距（实测）；
        被裁切的气泡紧贴标题栏下沿，上方没有内边距。
        """
        width, height = size
        x = int(line.x * width)
        y = int(line.y * height)
        if x < 0 or x >= width:
            return False

        target = COLOR_MINE if owner == "mine" else COLOR_OTHER
        # 文字行上方向上找气泡色，允许的最大距离即内边距长度
        padding_limit = int(0.020 * height)  # 实测约 22/1280 = 0.017
        for dy in range(1, padding_limit):
            yy = y - dy
            if yy < 0:
                break
            if _close(img.getpixel((x, yy)), target):
                return False  # 上方还有气泡色 → 气泡完整
        return True

    def _find_input_border(self, img: Any, size: tuple[int, int]) -> float | None:
        """检测输入框上边框，返回其归一化纵坐标。

        输入框上边框是一条贯穿聊天区宽度的水平细线，颜色比聊天背景
        （深色模式约 (30,30,31)）亮一点（实测约 (51,51,52)）。逐行统计
        [20%, 82%] 区间内"比背景亮的中性灰"像素占比，占比接近 1 的行即为边框。

        检测不到返回 ``None``，由上层用固定比例兜底。
        """
        width, height = size
        x0, x1 = int(0.20 * width), int(0.82 * width)
        samples = list(range(x0, x1, 2))
        if not samples:
            return None

        y_start = int(INPUT_BORDER_MIN_Y * height)
        y_end = int(INPUT_BORDER_MAX_Y * height)
        for y in range(y_start, min(y_end, height - 1)):
            hits = 0
            for x in samples:
                r, g, b = img.getpixel((x, y))
                if abs(r - g) > 8 or abs(g - b) > 8:
                    continue
                if 40 <= r <= 90:
                    hits += 1
            if hits >= len(samples) * 0.9:
                return y / height
        return None

    # -- 解析 ---------------------------------------------------------------
    def _is_avatar(self, line: OCRLine) -> bool:
        """判断这一行是不是头像里的文字（微信会把头像图片当文字识别出来）。"""
        return AVATAR_MIN_X <= line.center_x <= AVATAR_MAX_X

    @staticmethod
    def _is_ui_button(text: str) -> bool:
        """判断这一行是不是微信界面上的按钮文字。

        语音条右侧的「转文字」按钮底色是 (41,41,42)，落在对方气泡色的
        容差内，会被当成对方消息触发回复。按钮词表是白名单式精确匹配，
        宁可漏掉一个新按钮，也不能误杀一条真消息。
        """
        return text in UI_BUTTON_TEXTS

    def _parse_chat_list(self, lines: list[OCRLine]) -> list[ChatIdentity]:
        """从左侧会话列表解析会话名（仅在列表展开时有意义）。

        微信窗口窄时侧边栏会折叠成**纯头像**，此时 OCR 读到的是头像图片里的
        文字（实测 "PERRY"）和未读数徽章（实测 "22"），都不是会话名。
        因此本方法的结果**不能**用来定位目标会话——见 :meth:`list_chats`。
        """
        chats: list[ChatIdentity] = []
        seen: set[str] = set()
        for line in sorted(lines, key=lambda item: item.center_y):
            text = line.text.strip()
            if not text or text in seen:
                continue
            if line.center_x > SIDEBAR_MAX_X:
                continue
            if _TIME_RE.match(text) or text.isdigit():
                continue  # 时间戳 / 未读数徽章
            if any(
                marker in text
                for marker in ("搜索", "通讯录", "收藏", "朋友圈", "卡包", "聊天信息", "文件", "小程序")
            ):
                continue
            if len(text) > 40:
                continue
            seen.add(text)
            is_group = text.startswith("[") and text.endswith("]")
            chats.append(
                ChatIdentity(
                    chat_id=self._chat_id_for(text, is_group),
                    display_name=text,
                    kind=ChatKind.GROUP if is_group else ChatKind.CONTACT,
                    is_group=is_group,
                )
            )
            if len(chats) >= self.max_list_items:
                break
        return chats

    @staticmethod
    def _chat_id_for(display_name: str, is_group: bool = False) -> str:
        """从显示名派生稳定 chat_id。规则见 :func:`pwa.models.chat_id_for`。"""
        return chat_id_for(display_name, is_group)

    def _merge_bubble_lines(self, lines: list[OCRLine]) -> list[OCRLine]:
        """把同一个气泡里的多行文字合并成一条。

        微信一条长消息会渲染成多行，每行都是独立的 OCR 结果。若不合并，
        一条 4 行消息会变成 4 条消息 → 4 次模型调用 → 4 条重复草稿。
        """
        groups: list[list[OCRLine]] = []
        for line in sorted(lines, key=lambda x: x.center_y):
            if groups:
                prev = groups[-1][-1]
                gap = line.y - prev.bottom
                same_bubble = (
                    prev.owner == line.owner
                    and gap < LINE_MERGE_GAP_FACTOR * max(prev.height, line.height)
                    and abs(prev.x - line.x) < LINE_MERGE_LEFT_TOL
                )
                if same_bubble:
                    groups[-1].append(line)
                    continue
            groups.append([line])

        merged: list[OCRLine] = []
        for group in groups:
            head = group[0]
            left = min(item.x for item in group)
            merged.append(
                OCRLine(
                    text=" ".join(item.text for item in group),
                    confidence=min(item.confidence for item in group),
                    x=left,
                    y=head.y,
                    width=max(item.right for item in group) - left,
                    height=group[-1].bottom - head.y,
                    owner=head.owner,
                )
            )
        return merged

    def _parse_messages(
        self,
        lines: list[OCRLine],
        chat: ChatIdentity,
        *,
        capture_path: Path,
        size: tuple[int, int],
    ) -> list[ObservedMessage]:
        """从聊天区解析消息，并用气泡颜色判断发送方。"""
        messages: list[ObservedMessage] = []
        now = time.time()
        pixels = self._load_pixels(capture_path)
        chat_bottom = self._find_input_border(pixels, size) or CHAT_AREA_BOTTOM_FALLBACK

        candidates: list[OCRLine] = []
        for line in lines:
            if not (CHAT_AREA_TOP <= line.center_y <= chat_bottom):
                continue
            # 左侧头像里的文字不是消息
            if self._is_avatar(line):
                continue
            # 侧边栏的内容不是消息
            if line.center_x < SIDEBAR_MAX_X:
                continue
            text = line.text.strip()
            if not text or _TIME_RE.match(text):
                continue
            # 微信界面按钮（语音条上的「转文字」等）不是消息
            if self._is_ui_button(text):
                continue
            # 语音时长标记（3"）属于语音条本身，不是消息内容
            if _VOICE_DURATION_RE.match(text):
                continue

            owner = self._bubble_color(pixels, line, size)
            if owner == "unknown":
                # 颜色判不出来 → 不猜，跳过这条
                continue
            # 被标题栏裁切的气泡内容残缺 → 跳过，不拿乱码去触发回复
            if self._is_clipped_bubble(pixels, line, size, owner):
                continue
            line.owner = owner
            candidates.append(line)

        for line in self._merge_bubble_lines(candidates):
            text = line.text.strip()
            if not text:
                continue
            is_from_me = line.owner == "mine"
            sender = ""
            if chat.is_group and not is_from_me:
                m = _GROUP_SENDER_RE.match(text)
                if m:
                    sender, text = m.group(1), m.group(2)
            messages.append(
                ObservedMessage(
                    chat=chat,
                    text=text,
                    timestamp=now,
                    is_from_me=is_from_me,
                    confidence=line.confidence,
                    sender=sender,
                )
            )

        # 给每条消息打上"前一条消息的指纹"，作为跨轮次稳定的去重锚点。
        # 微信不暴露消息 id，只能靠相邻关系定位同一条消息：屏幕滚动了，
        # 前一条变了 → 说明是新消息；没滚动 → 前一条不变 → 同一条。
        return self._attach_anchors(messages)

    @staticmethod
    def _attach_anchors(messages: list[ObservedMessage]) -> list[ObservedMessage]:
        """返回带 anchor 字段的新消息列表。

        anchor 取**前一条**消息的指纹（同会话 + 同发送者 + 归一化文本）。
        首条消息的 anchor 为空——它没有前一条可参照，靠自身内容哈希兜底。

        已知局限：若用户向上滚动导致首条可见消息改变，只有**紧邻首条**的那条
        消息锚点会变，可能触发一次多余回复。正常聊天不滚动，不会发生。
        """
        result: list[ObservedMessage] = []
        for index, message in enumerate(messages):
            if index == 0:
                anchor = ""
            else:
                previous = messages[index - 1]
                raw = (
                    f"{previous.chat.chat_id}|{int(previous.is_from_me)}|"
                    f"{previous.sender}|{''.join(previous.text.split())}"
                )
                anchor = hashlib.sha256(raw.encode()).hexdigest()[:16]
            result.append(replace(message, anchor=anchor))
        return result

    # -- Observer 接口 -------------------------------------------------------
    def list_chats(self) -> list[ChatIdentity]:
        """返回**当前正在显示**的会话。

        微信 4.x 的 AX 树是空的，无法点击侧边栏切换会话，也读不到会话列表
        的真实结构（窗口窄时侧边栏折叠成纯头像）。所以本观察器**只能**操作
        当前已打开的那个会话——:meth:`read_chat` 同样只能读到屏幕上这一段。

        因此这里返回标题栏读出的会话名，而不是侧边栏列表。侧边栏解析结果
        不可信（会把头像图片里的文字当成会话名），用它选会话会读错人。

        要观察别的会话，请先在微信里手动点开它。
        """
        chat_id = self.current_chat_id()
        display_name = chat_id.split(":", 1)[1] if ":" in chat_id else chat_id
        return [
            ChatIdentity(
                chat_id=chat_id,
                display_name=display_name,
                kind=ChatKind.CONTACT,
                is_group=False,
            )
        ]

    def read_chat(self, chat: ChatIdentity, limit: int = 20) -> list[ObservedMessage]:
        image, path = self.capture_window()
        try:
            lines = self.ocr(image)
            if not lines:
                raise ObserverError("OCR 没有识别到任何文字", kind="ocr_unreliable")
            import Quartz

            size = (Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image))
            messages = self._parse_messages(lines, chat, capture_path=path, size=size)
        finally:
            path.unlink(missing_ok=True)
        return messages[-limit:]

    def is_login_valid(self) -> bool:
        """通过截图判断微信是否处于正常登录状态。"""
        try:
            image, path = self.capture_window()
        except ObserverError:
            return False
        try:
            lines = self.ocr(image)
        except ObserverError:
            return False
        finally:
            path.unlink(missing_ok=True)
        texts = " ".join(line.text for line in lines)
        if any(marker in texts for marker in _LOGIN_MARKERS):
            return False
        return True

    def current_chat_id(self) -> str:
        """读出当前微信正在显示的会话 id。

        微信 4.x 的会话列表在窗口窄时折叠成头像，识别不可靠。但**标题栏**
        始终显示当前会话名，它的位置固定、字号大、OCR 置信度高，是可靠来源。
        """
        image, path = self.capture_window()
        try:
            lines = self.ocr(image)
        finally:
            path.unlink(missing_ok=True)
        if not lines:
            raise ObserverError("OCR 没有识别到任何文字", kind="ocr_unreliable")

        band = [
            line
            for line in lines
            if TITLE_BAND[0] <= line.center_y <= TITLE_BAND[1]
        ]
        band.sort(key=lambda line: line.x)
        for line in band:
            text = line.text.strip()
            if not text or text in WECHAT_OWNER_NAMES or _TIME_RE.match(text):
                continue
            # 排除右侧的功能按钮文字
            if line.x > 0.6:
                continue
            return self._chat_id_for(text)
        raise ObserverError(
            "无法从标题栏确定当前会话（微信 4.x 未暴露会话状态）",
            kind="identity_unknown",
        )


def _close(a: tuple[int, int, int], b: tuple[int, int, int], tol: int = COLOR_TOLERANCE) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


class MockObserver(Observer):
    """可运行的模拟适配器，用于在没有真实微信接入时开发与测试。

    它由脚本化场景驱动，可以模拟：正常消息、空消息、重复消息、相似联系人、
    窗口切换、登录失效、OCR 失败。
    """

    name = "mock"

    def __init__(
        self,
        chats: list[ChatIdentity] | None = None,
        messages: dict[str, list[ObservedMessage]] | None = None,
        *,
        login_valid: bool = True,
        fail_with: ObserverError | None = None,
        current_chat: str | None = None,
    ) -> None:
        self._chats = chats or []
        self._messages = messages or {}
        self._login_valid = login_valid
        self._fail_with = fail_with
        self._current_chat = current_chat
        self.read_calls: list[str] = []

    def set_chats(self, chats: list[ChatIdentity]) -> None:
        self._chats = chats

    def set_messages(self, chat_id: str, messages: list[ObservedMessage]) -> None:
        self._messages[chat_id] = messages

    def set_login_valid(self, valid: bool) -> None:
        self._login_valid = valid

    def set_failure(self, error: ObserverError | None) -> None:
        self._fail_with = error

    def set_current_chat(self, chat_id: str | None) -> None:
        self._current_chat = chat_id

    def list_chats(self) -> list[ChatIdentity]:
        if self._fail_with:
            raise self._fail_with
        return list(self._chats)

    def read_chat(self, chat: ChatIdentity, limit: int = 20) -> list[ObservedMessage]:
        if self._fail_with:
            raise self._fail_with
        self.read_calls.append(chat.chat_id)
        return list(self._messages.get(chat.chat_id, []))[-limit:]

    def is_login_valid(self) -> bool:
        if self._fail_with:
            raise self._fail_with
        return self._login_valid

    def current_chat_id(self) -> str:
        if self._fail_with:
            raise self._fail_with
        if self._current_chat is None:
            raise ObserverError(
                "无法确定当前会话", kind="identity_unknown"
            )
        return self._current_chat
