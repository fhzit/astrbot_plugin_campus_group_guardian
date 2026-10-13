# -*- coding: utf-8 -*-
"""端到端集成测试：走真实插件入口（main.py），使用 Mock 平台客户端，不连真实群（§182）。

覆盖：
- 指令 / LLM 工具注册；
- OneBot 群消息 → 反诈 → 撤回 + 禁言 + 群内预警 + 事件落库；
- 群主/管理员放行（不撤回、不建事件）；
- 钉钉类平台：动作降级为 UNSUPPORTED，事件与预警仍成功（§213）。
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from astrbot.api.event import REGISTRY, AstrMessageEvent
from astrbot.api.star import Context

SCAM_TEXT = (
    "各位家长：学校统一购买学习资料，每位495元，请扫码付款，"
    "付款时备注学生姓名，付款完成后截图方便统计。"
)


class FakeOneBotClient:
    """记录 OneBot API 调用，并回答角色查询。"""

    def __init__(self, *, bot_role: str = "admin", member_role: str = "member") -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.bot_role = bot_role
        self.member_role = member_role

    async def call_action(self, action: str, **params: Any) -> dict[str, Any]:
        self.calls.append((action, params))
        if action == "get_group_member_info":
            role = self.bot_role if str(params.get("user_id")) == "9" else self.member_role
            return {"status": "ok", "retcode": 0, "data": {"role": role, "nickname": "某人"}}
        if action == "get_group_member_list":
            return {"status": "ok", "retcode": 0, "data": []}
        return {"status": "ok", "retcode": 0, "data": None}

    def actions(self) -> list[str]:
        return [action for action, _ in self.calls]

    def params_for(self, action: str) -> list[dict[str, Any]]:
        return [params for called, params in self.calls if called == action]


@pytest.fixture
async def plugin(tmp_path: Path, monkeypatch):
    """构建真实插件实例（异步 fixture，测试结束后释放数据库连接，避免 aiosqlite 线程挂住退出）。"""
    monkeypatch.setenv("GG_TEST_DATA_DIR", str(tmp_path / "plugin_data"))
    if "astrbot_plugin_campus_group_guardian.main" in sys.modules:
        module = importlib.reload(sys.modules["astrbot_plugin_campus_group_guardian.main"])
    else:
        module = importlib.import_module("astrbot_plugin_campus_group_guardian.main")

    context = Context()
    instance = module.CampusGroupGuardian(
        context,
        {
            "platform": {"role_cache_ttl_seconds": 0},
            "anti_fraud": {
                "enabled": True,
                "auto_recall_critical": True,
                "auto_mute_critical": True,
                "critical_mute_seconds": 600,
                "group_warning_enabled": True,
            },
            "moderation": {
                "enabled": True,
                "admin_exempt": True,
            },
        },
    )
    try:
        yield instance, module, context
    finally:
        for task in (instance._init_task, instance._scheduler_task):
            if task is not None and not task.done():
                task.cancel()
        if instance.ocr_service is not None:
            await instance.ocr_service.close()
        if instance.storage is not None:
            await instance.storage.close()


def build_event(client: FakeOneBotClient, **overrides: Any) -> AstrMessageEvent:
    kwargs: dict[str, Any] = {
        "message_str": SCAM_TEXT,
        "group_id": "10001",
        "sender_id": "20001",
        "sender_name": "班主任张老师",
        "self_id": "9",
        "platform_name": "aiocqhttp",
        "platform_id": "inst-1",
        "role": "member",
        "bot": client,
    }
    kwargs.update(overrides)
    return AstrMessageEvent(**kwargs)


def test_commands_and_tools_are_registered(plugin):
    """指令与 LLM 工具确实注册到 AstrBot（而不是只写了函数）。"""
    _instance, _module, _context = plugin
    commands = {
        entry["args"][0] for entry in REGISTRY.get("command", []) if entry.get("args")
    }
    assert {
        "守护状态",
        "守护统计",
        "反诈状态",
        "反诈开启",
        "反诈关闭",
        "反诈测试",
        "反诈",
        "禁言",
        "解禁",
        "踢人",
    } <= commands
    tools = {
        entry["kwargs"].get("name") for entry in REGISTRY.get("llm_tool", [])
    }
    assert {
        "antifraud_test_text",
        "ban_group_member",
        "unban_group_member",
        "kick_group_member",
        "recall_group_message",
    } <= tools


def test_platform_filter_covers_all_six_platforms(plugin):
    _instance, module, _context = plugin
    from astrbot.api.event import PlatformAdapterType

    for flag in (
        PlatformAdapterType.AIOCQHTTP,
        PlatformAdapterType.QQOFFICIAL,
        PlatformAdapterType.QQOFFICIAL_WEBHOOK,
        PlatformAdapterType.WECOM,
        PlatformAdapterType.WECOM_AI_BOT,
        PlatformAdapterType.DINGTALK,
    ):
        assert module.PLATFORM_FILTER & flag


def test_web_apis_are_registered(plugin):
    _instance, _module, context = plugin
    _instance.register_web_apis()
    routes = {route for route, _handler, _methods, _desc in context.web_apis}
    assert "/astrbot_plugin_campus_group_guardian/overview" in routes
    assert "/astrbot_plugin_campus_group_guardian/test" in routes
    assert "/astrbot_plugin_campus_group_guardian/capabilities" in routes


async def test_end_to_end_scam_is_recalled_and_recorded(plugin, tmp_path):
    instance, _module, _context = plugin
    await instance._bootstrap()
    client = FakeOneBotClient(bot_role="admin")
    event = build_event(client)

    await instance.on_group_message(event)

    actions = client.actions()
    assert "delete_msg" in actions, actions
    assert "set_group_ban" in actions
    ban = client.params_for("set_group_ban")[0]
    assert ban["user_id"] == "20001"
    assert ban["duration"] == 600
    assert ban["group_id"] == "10001"

    # 群内预警已发出
    sent = [chain for _session, chain in _context.sent]
    assert sent, "应发送群内反诈预警"
    assert any("请勿" in str(getattr(part, "text", "")) for chain in sent for part in chain)

    # 事件与动作落库
    stats = await instance.storage.stats_snapshot()
    assert stats["incidents_total"] == 1
    assert stats["action_success"] >= 2
    incidents = await instance.storage.list_incidents(limit=5)
    assert incidents[0]["risk_level"] == "critical"
    assert incidents[0]["group_id"] == "10001"
    assert incidents[0]["sender_role"] == "member"
    logs = await instance.storage.list_action_logs(limit=10)
    assert {row["action"] for row in logs} >= {"recall", "mute", "warn"}

    # 高风险诈骗会阻断 AstrBot 后续处理
    assert event.stopped is True


async def test_admin_sender_bypasses_school_fraud(plugin):
    instance, _module, _context = plugin
    await instance._bootstrap()
    client = FakeOneBotClient(bot_role="admin", member_role="admin")
    event = build_event(client, role="admin", sender_id="20002")

    await instance.on_group_message(event)

    assert "delete_msg" not in client.actions()
    assert "set_group_ban" not in client.actions()
    stats = await instance.storage.stats_snapshot()
    assert stats["incidents_total"] == 0
    assert event.stopped is False


async def test_dingtalk_platform_degrades_but_records(plugin):
    """§213：钉钉平台检测层一致，管理动作降级。"""
    instance, _module, context = plugin
    await instance._bootstrap()
    client = FakeOneBotClient()
    event = build_event(client, platform_name="dingtalk", platform_id="inst-9")

    await instance.on_group_message(event)

    # 钉钉适配器不会调用 OneBot 客户端
    assert client.actions() == []
    stats = await instance.storage.stats_snapshot()
    assert stats["incidents_total"] == 1
    logs = await instance.storage.list_action_logs(limit=10)
    by_action = {row["action"]: row["status"] for row in logs}
    assert by_action["recall"] == "unsupported"
    assert by_action["warn"] == "success"


async def test_normal_message_is_ignored(plugin):
    instance, _module, _context = plugin
    await instance._bootstrap()
    client = FakeOneBotClient()
    event = build_event(client, message_str="老师，今天的作业是什么？")

    await instance.on_group_message(event)

    actions = client.actions()
    assert "delete_msg" not in actions
    assert "set_group_ban" not in actions
    assert "set_group_kick" not in actions
    stats = await instance.storage.stats_snapshot()
    assert stats["incidents_total"] == 0
    assert event.stopped is False


async def test_terminate_releases_resources(plugin):
    """§186：卸载后取消任务、关闭数据库。"""
    instance, _module, _context = plugin
    await instance._bootstrap()
    await instance.terminate()
    assert instance._ready is False


async def test_plugin_state_survives_reload(plugin, tmp_path):
    """§125/§188：数据在插件数据目录，重载不清空。"""
    instance, module, _context = plugin
    await instance._bootstrap()
    client = FakeOneBotClient()
    await instance.on_group_message(build_event(client))
    stats_before = await instance.storage.stats_snapshot()
    assert stats_before["incidents_total"] == 1
    await instance.terminate()

    reloaded = module.CampusGroupGuardian(Context(), {"anti_fraud": {"enabled": True}})
    await reloaded._bootstrap()
    stats_after = await reloaded.storage.stats_snapshot()
    assert stats_after["incidents_total"] == 1
    await reloaded.terminate()
