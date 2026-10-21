"""配置层：从环境变量 / .env 读取，运行期只保存不含密钥的值。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=False)

_BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ModelEndpoint:
    """一个 OpenAI 兼容模型端点。"""

    name: str
    base_url: str
    api_key: str
    model: str
    price_input_per_mtok: float = 0.0
    price_output_per_mtok: float = 0.0
    timeout: float = 30.0
    # 单次生成的最大 token 数。推理模型（reasoning）会把 reasoning 的
    # token 计入 completion_tokens，预算给太小会返回空正文——实测
    # DeepSeek 在 max_tokens=16 时 reply 为空串，800 才正常。
    max_tokens: int = 800

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)

    @property
    def is_local(self) -> bool:
        return "127.0.0.1" in self.base_url or "localhost" in self.base_url


@dataclass(frozen=True)
class Config:
    host: str = field(default_factory=lambda: _env("PWA_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("PWA_PORT", "8765")))
    data_dir: Path = field(
        default_factory=lambda: Path(_env("PWA_DATA_DIR", "./data")).expanduser()
    )

    primary: ModelEndpoint = field(
        default_factory=lambda: ModelEndpoint(
            name="primary",
            base_url=_env("PWA_LLM_BASE_URL"),
            api_key=_env("PWA_LLM_API_KEY"),
            model=_env("PWA_LLM_MODEL"),
            price_input_per_mtok=_env_float("PWA_PRICE_PRIMARY_INPUT", 0.0),
            price_output_per_mtok=_env_float("PWA_PRICE_PRIMARY_OUTPUT", 0.0),
            max_tokens=_env_int("PWA_MAX_TOKENS_PRIMARY", 800),
        )
    )
    fallback: ModelEndpoint = field(
        default_factory=lambda: ModelEndpoint(
            name="fallback",
            base_url=_env("PWA_LLM_FALLBACK_BASE_URL"),
            api_key=_env("PWA_LLM_FALLBACK_API_KEY"),
            model=_env("PWA_LLM_FALLBACK_MODEL"),
            price_input_per_mtok=_env_float("PWA_PRICE_FALLBACK_INPUT", 0.0),
            price_output_per_mtok=_env_float("PWA_PRICE_FALLBACK_OUTPUT", 0.0),
            max_tokens=_env_int("PWA_MAX_TOKENS_FALLBACK", 800),
        )
    )
    local: ModelEndpoint = field(
        default_factory=lambda: ModelEndpoint(
            name="local",
            base_url=_env("PWA_LOCAL_BASE_URL"),
            api_key=_env("PWA_LOCAL_API_KEY"),
            model=_env("PWA_LOCAL_MODEL"),
            price_input_per_mtok=_env_float("PWA_PRICE_LOCAL_INPUT", 0.0),
            price_output_per_mtok=_env_float("PWA_PRICE_LOCAL_OUTPUT", 0.0),
            max_tokens=_env_int("PWA_MAX_TOKENS_LOCAL", 800),
        )
    )

    allowed_chats: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            c.strip() for c in _env("PWA_ALLOWED_CHATS").split(",") if c.strip()
        )
    )
    enable_send: bool = field(default_factory=lambda: _env_bool("PWA_ENABLE_SEND"))
    # 真正的发送开关。与 enable_send 分开：enable_send 表示"允许发送"，
    # dry_run 表示"只填输入框不按回车"。两者都通过才会真的发出去。
    send_dry_run: bool = field(default_factory=lambda: _env_bool("PWA_SEND_DRY_RUN", True))
    poll_interval: float = field(default_factory=lambda: _env_float("PWA_POLL_INTERVAL", 3.0))
    ocr_min_confidence: float = field(
        default_factory=lambda: _env_float("PWA_OCR_MIN_CONFIDENCE", 0.45)
    )

    # 示例联系人自动回复模式（可选，默认关闭）
    auto_reply_enabled: bool = field(
        default_factory=lambda: _env_bool("PWA_AUTO_REPLY_ENABLED")
    )
    auto_reply_chat: str = field(default_factory=lambda: _env("PWA_AUTO_REPLY_CHAT"))
    auto_reply_interval: float = field(
        default_factory=lambda: _env_float("PWA_AUTO_REPLY_INTERVAL", 20.0)
    )
    auto_reply_duration: float = field(
        default_factory=lambda: _env_float("PWA_AUTO_REPLY_DURATION", 6 * 3600)
    )
    # 自动回复允许写入的会话白名单。**空列表表示不允许任何会话**——
    # 想开启自动发送，必须显式列出目标会话。
    auto_reply_allow_chats: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            c.strip()
            for c in _env("PWA_AUTO_REPLY_ALLOW_CHATS").split(",")
            if c.strip()
        )
    )
    # 发送频率限制：窗口内最多发几条
    send_rate_max: int = field(default_factory=lambda: _env_int("PWA_SEND_RATE_MAX", 5))
    send_rate_window: float = field(
        default_factory=lambda: _env_float("PWA_SEND_RATE_WINDOW", 60.0)
    )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pwa.sqlite3"

    def endpoint(self, name: str) -> ModelEndpoint | None:
        return {"primary": self.primary, "fallback": self.fallback, "local": self.local}.get(name)


def get_config() -> Config:
    return Config()
