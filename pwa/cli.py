"""命令行入口。

用法：
    pwa serve      启动本地审核界面（默认 127.0.0.1:8765）
    pwa doctor     检查环境、权限与模型端点配置
    pwa observe    执行一轮只读观察（调试用）
    pwa seed       用模拟数据生成若干草稿，用于验证界面与模型链路
    pwa auto-reply 示例联系人自动回复模式（需显式开启，默认关闭）
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from typing import Any

from .auto_reply import AutoReplyRunner
from .config import Config, get_config
from .guard import RateLimiter, SendGatekeeper
from .models import ChatIdentity, ChatKind, ObservedMessage
from .observer.wechat_macos import MockObserver, WeChatObserver
from .observer.wechat_sender import WeChatSender
from .persona import AUTO_REPLY_SYSTEM_PROMPT
from .pipeline import Pipeline
from .providers.openai_compat import MockProvider, OpenAICompatProvider
from .router import Router
from .sender import SendGate
from .store import Store
from .web.app import create_app


def build_router(config: Config) -> Router:
    providers = {
        "primary": OpenAICompatProvider(config.primary),
        "fallback": OpenAICompatProvider(config.fallback),
        "local": OpenAICompatProvider(config.local),
    }
    prices = {
        "primary": config.primary,
        "fallback": config.fallback,
        "local": config.local,
    }
    return Router(providers, prices)


def build_observer(config: Config, *, mock: bool = False):
    if mock:
        return MockObserver()
    return WeChatObserver(min_confidence=config.ocr_min_confidence)


def build_pipeline(config: Config, *, observer=None, router=None) -> Pipeline:
    store = Store(config.db_path)
    pipeline = Pipeline(
        observer=observer or build_observer(config),
        router=router or build_router(config),
        store=store,
        allowed_chats=config.allowed_chats,
    )
    pipeline._ocr_min_confidence = config.ocr_min_confidence  # noqa: SLF001
    return pipeline


# -- 子命令 -----------------------------------------------------------------
def cmd_serve(args: argparse.Namespace) -> int:
    config = get_config()
    pipeline = build_pipeline(config)
    sender = WeChatSender(dry_run=config.send_dry_run)
    gate = SendGate(
        store=pipeline.store,
        observer=pipeline.observer,
        enable_send=config.enable_send,
        sender=sender.send if config.enable_send else None,
        # 白名单与观察授权名单一致；为空表示不允许发送任何会话
        allow_chats=config.allowed_chats,
        rate_limiter=RateLimiter(
            max_sends=config.send_rate_max, window_seconds=config.send_rate_window
        ),
    )
    app = create_app(
        config=config,
        store=pipeline.store,
        router=pipeline.router,
        pipeline=pipeline,
        send_gate=gate,
    )
    import uvicorn

    print(f"Perry WeChat AI → http://{config.host}:{config.port}")
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = get_config()
    report: dict[str, Any] = {"config": {}, "endpoints": [], "system": {}}

    report["config"] = {
        "host": config.host,
        "port": config.port,
        "data_dir": str(config.data_dir),
        "allowed_chats": list(config.allowed_chats),
        "send_enabled": config.enable_send,
        "ocr_min_confidence": config.ocr_min_confidence,
    }

    for ep in (config.primary, config.fallback, config.local):
        report["endpoints"].append(
            {
                "name": ep.name,
                "base_url": ep.base_url,
                "model": ep.model,
                "configured": ep.configured,
                "price_in": ep.price_input_per_mtok,
                "price_out": ep.price_output_per_mtok,
            }
        )

    # 系统能力
    try:
        import Quartz  # noqa: F401,PLC0415

        report["system"]["quartz"] = True
    except ImportError:
        report["system"]["quartz"] = False
    try:
        import Vision  # noqa: F401,PLC0415

        report["system"]["vision_ocr"] = True
    except ImportError:
        report["system"]["vision_ocr"] = False

    # 微信窗口与截图
    observer = WeChatObserver(min_confidence=config.ocr_min_confidence)
    try:
        image, capture_path = observer.capture_window()
        try:
            width = 0
            height = 0
            try:
                import Quartz

                width = Quartz.CGImageGetWidth(image)
                height = Quartz.CGImageGetHeight(image)
            except Exception:  # noqa: BLE001
                pass
            report["system"]["wechat_window_capture"] = {"ok": True, "size": [width, height]}
            try:
                lines = observer.ocr(image)
                report["system"]["ocr_lines"] = len(lines)
                report["system"]["login_valid"] = observer.is_login_valid()
            except Exception as exc:  # noqa: BLE001
                report["system"]["ocr_error"] = str(exc)
        finally:
            capture_path.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        report["system"]["wechat_window_capture"] = {"ok": False, "error": str(exc)}

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_observe(args: argparse.Namespace) -> int:
    config = get_config()
    pipeline = build_pipeline(config)
    try:
        drafts = pipeline.poll_once()
    except Exception as exc:  # noqa: BLE001
        print(f"观察失败：{exc}", file=sys.stderr)
        return 1
    print(f"本轮生成 {len(drafts)} 条草稿")
    for d in drafts:
        print(f"  [{d['chat_name']}] {d['trigger_text'][:30]!r} → {d['reply_text'][:40]!r}")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    """用模拟观察器 + 模拟模型验证整条链路，不触碰真实微信。"""
    config = get_config()
    chats = [
        ChatIdentity(chat_id="name:妈妈", display_name="妈妈"),
        ChatIdentity(chat_id="name:项目组", display_name="项目组", kind=ChatKind.GROUP, is_group=True),
        ChatIdentity(chat_id="name:小明", display_name="小明"),
    ]
    now = time.time()
    messages = {
        "name:妈妈": [
            ObservedMessage(chat=chats[0], text="周末回家吃饭吗", timestamp=now),
        ],
        "name:项目组": [
            ObservedMessage(chat=chats[1], text="这个方案你能帮忙分析一下吗", timestamp=now, sender="张三"),
        ],
        "name:小明": [
            ObservedMessage(chat=chats[2], text="你好", timestamp=now),
        ],
    }
    observer = MockObserver(chats=chats, messages=messages)
    router = Router(
        providers={"primary": MockProvider("（模拟）好的，周末我回去。", endpoint_name="primary")},
        prices={},
    )
    pipeline = build_pipeline(config, observer=observer, router=router)
    pipeline._ocr_min_confidence = 0.0  # noqa: SLF001
    pipeline.allowed_chats = tuple(c.display_name for c in chats)
    drafts = pipeline.poll_once()
    print(f"已生成 {len(drafts)} 条模拟草稿，写入 {config.db_path}")
    for d in drafts:
        print(f"  [{d['chat_name']}] {d['reply_text']}")
    return 0


def cmd_auto_reply(args: argparse.Namespace) -> int:
    """示例联系人自动回复模式。

    默认**只生成回复不发送**；要真的发出去，必须同时满足：
      1. ``PWA_AUTO_REPLY_ENABLED=true``
      2. ``PWA_ENABLE_SEND=true``
      3. ``PWA_SEND_DRY_RUN=false``
      4. ``PWA_AUTO_REPLY_ALLOW_CHATS`` 包含目标会话

    前三项之外，发送还要通过闸门：目标会话在白名单内、发送频率未超限、
    没有触发紧急停止。
    """
    config = get_config()

    if not config.auto_reply_enabled and not args.force:
        print(
            "自动回复模式默认关闭。要开启请设置 PWA_AUTO_REPLY_ENABLED=true，"
            "或加 --force 本次临时启用。",
            file=sys.stderr,
        )
        return 1

    chat_name = args.chat or config.auto_reply_chat
    if not chat_name:
        print("未指定目标会话。用 --chat <名字> 或设置 PWA_AUTO_REPLY_CHAT。", file=sys.stderr)
        return 1

    will_send = config.enable_send and not config.send_dry_run
    allow_chats = config.auto_reply_allow_chats or (chat_name.strip(),)

    if will_send and not config.auto_reply_allow_chats:
        print(
            "拒绝启动：PWA_AUTO_REPLY_ALLOW_CHATS 为空。\n"
            "自动发送必须显式列出允许写入的会话，例如：\n"
            f"  PWA_AUTO_REPLY_ALLOW_CHATS={chat_name.strip()}",
            file=sys.stderr,
        )
        return 1

    store = Store(config.db_path)
    observer = WeChatObserver(min_confidence=config.ocr_min_confidence)
    router = build_router(config)

    sender = WeChatSender(dry_run=not will_send) if will_send else None
    gatekeeper = SendGatekeeper(
        allow_chats=allow_chats,
        rate_limiter=RateLimiter(
            max_sends=config.send_rate_max, window_seconds=config.send_rate_window
        ),
    )

    runner = AutoReplyRunner(
        observer=observer,
        router=router,
        store=store,
        chat_name=chat_name,
        system_prompt=AUTO_REPLY_SYSTEM_PROMPT,
        interval=config.auto_reply_interval,
        duration=config.auto_reply_duration,
        sender=sender.send if sender else None,
        gatekeeper=gatekeeper,
    )
    runner.start()
    print(
        f"自动回复已启动：会话={chat_name} 间隔={config.auto_reply_interval}s "
        f"时长={config.auto_reply_duration}s 发送={'开启' if will_send else '关闭（只生成不发送）'}",
        flush=True,
    )
    if will_send:
        print(
            f"发送闸门：白名单={', '.join(allow_chats)} "
            f"限流={config.send_rate_max} 条/{config.send_rate_window:.0f} 秒",
            flush=True,
        )
    print(
        f"截止时间：{time.strftime('%H:%M:%S', time.localtime(runner.state.deadline))}",
        flush=True,
    )
    print("按 Ctrl+C 停止。", flush=True)

    # Ctrl+C 时立刻 tripped 紧急停止闸门：即使信号处理有延迟，
    # 也不会再多发出一条。
    def _on_sigint(signum, frame):  # pragma: no cover - 交互式路径
        runner.emergency_stop("收到 SIGINT")

    previous_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _on_sigint)
    try:
        runner.run()
    except KeyboardInterrupt:
        runner.emergency_stop("Ctrl+C 手动停止")
        print("\n已停止。", flush=True)
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        store.close()
    print(
        f"共回复 {runner.state.replied_count} 条，轮询 {runner.state.poll_count} 次。",
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pwa", description="Perry WeChat AI")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="启动本地审核界面")
    p_serve.set_defaults(func=cmd_serve)

    p_doctor = sub.add_parser("doctor", help="检查环境与配置")
    p_doctor.set_defaults(func=cmd_doctor)

    p_observe = sub.add_parser("observe", help="执行一轮只读观察")
    p_observe.set_defaults(func=cmd_observe)

    p_seed = sub.add_parser("seed", help="用模拟数据验证整条链路")
    p_seed.set_defaults(func=cmd_seed)

    p_auto = sub.add_parser("auto-reply", help="示例联系人自动回复模式")
    p_auto.add_argument("--chat", help="目标会话名")
    p_auto.add_argument("--force", action="store_true", help="忽略配置开关，本次临时启用")
    p_auto.set_defaults(func=cmd_auto_reply)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
