"""命令行入口的测试。"""

from __future__ import annotations

import argparse

import pytest

from pwa.cli import main


def test_help_lists_all_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for command in ("serve", "doctor", "observe", "seed", "auto-reply"):
        assert command in out


def test_no_command_prints_help(capsys):
    assert main([]) == 1
    assert "usage" in capsys.readouterr().out


def test_auto_reply_requires_chat(capsys, monkeypatch):
    """没指定目标会话 → 拒绝启动，并给出配置项名。"""
    monkeypatch.delenv("PWA_AUTO_REPLY_CHAT", raising=False)
    monkeypatch.setattr("pwa.cli.get_config", _config_without_auto_chat)
    assert main(["auto-reply", "--force"]) == 1
    err = capsys.readouterr().err
    assert "未指定目标会话" in err
    assert "PWA_AUTO_REPLY_CHAT" in err


def test_auto_reply_disabled_without_force(capsys, monkeypatch):
    """默认关闭；不加 --force 不启动。"""
    monkeypatch.setattr("pwa.cli.get_config", _config_with_auto_disabled)
    assert main(["auto-reply", "--chat", "示例联系人"]) == 1
    err = capsys.readouterr().err
    assert "默认关闭" in err
    assert "PWA_AUTO_REPLY_ENABLED" in err


def test_seed_creates_mock_drafts(tmp_path, monkeypatch, capsys):
    """seed 用模拟数据跑通整条链路，不碰真实微信。"""
    monkeypatch.setenv("PWA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("pwa.cli.get_config", _config_with_auto_disabled)
    assert main(["seed"]) == 0
    out = capsys.readouterr().out
    assert "已生成" in out

    from pwa.config import get_config

    from pwa.store import Store

    store = Store(get_config().db_path)
    try:
        assert len(store.list_drafts()) == 3
    finally:
        store.close()


# --------------------------------------------------------------------------
def _config_with_auto_disabled():
    from pwa.config import Config

    return Config(auto_reply_enabled=False, auto_reply_chat="")


def _config_without_auto_chat():
    from pwa.config import Config

    return Config(auto_reply_enabled=True, auto_reply_chat="")
