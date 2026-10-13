# -*- coding: utf-8 -*-
"""平台动作降级与权限测试（规格 §30、§35、§100-§106、§181、§208-§213）。"""

from __future__ import annotations

from astrbot_plugin_campus_group_guardian.core.actions.engine import ActionEngine
from astrbot_plugin_campus_group_guardian.core.actions.models import (
    ActionStatus,
    GuardianAction,
    GuardianActionType,
)
from astrbot_plugin_campus_group_guardian.core.actions.policy import (
    build_fraud_actions,
    incident_required,
    should_stop_event,
)
from astrbot_plugin_campus_group_guardian.core.antifraud.detectors import detect_text_signals
from astrbot_plugin_campus_group_guardian.core.antifraud.engine import (
    AntiFraudConfig,
    SchoolFraudGuard,
)
from astrbot_plugin_campus_group_guardian.core.antifraud.models import FraudRiskLevel
from astrbot_plugin_campus_group_guardian.core.antifraud.risk_engine import score_signals
from astrbot_plugin_campus_group_guardian.core.models import GroupRole

from tests.mock_adapter import dingtalk_like, qq_admin_bot, qq_member_bot, wecom_like

SCAM_TEXT = "学校统一买资料，每位495元，扫码付款，备注学生姓名，付款后截图。"


class FakeNotifier:
    """记录管理员通知的简易通知器。"""

    def __init__(self) -> None:
        self.notified: list[dict] = []
        self.warning_text = "检测到高风险缴费信息，请勿立即扫码转账。"

    async def notify_admin(self, message, action) -> bool:  # noqa: ANN001
        self.notified.append({"group": message.group_id, "reason": action.reason})
        return True

    async def send_group_warning(self, group_key: str, text: str) -> bool:
        return True


def critical_result():
    """用真实检测器产出一个 CRITICAL 结果（不手写分数）。"""
    signals, _ = detect_text_signals(SCAM_TEXT, sender_name="班主任张老师")
    return score_signals(signals)


def test_critical_result_fixture_is_critical():
    result = critical_result()
    assert result.level is FraudRiskLevel.CRITICAL
    assert incident_required(result) is True


async def test_platform_without_permissions_degrades_not_fails(message_factory):
    """§30/§181/§213：无撤回/禁言能力的平台，其余处置必须成功且不抛异常。"""
    adapter = dingtalk_like()
    notifier = FakeNotifier()
    config = AntiFraudConfig(enabled=True, auto_recall_critical=True, auto_mute_critical=True)
    result = critical_result()
    actions = build_fraud_actions(result, config)

    engine = ActionEngine(adapter, notifier=notifier)
    outcome = await engine.execute(
        message_factory(SCAM_TEXT, platform="dingtalk"),
        actions,
        bot_role=GroupRole.UNKNOWN,
        origin="antifraud",
    )
    statuses = {item.action: item.status for item in outcome.results}
    assert statuses["recall"] is ActionStatus.UNSUPPORTED
    assert statuses["mute"] is ActionStatus.UNSUPPORTED
    assert statuses["warn"] is ActionStatus.SUCCESS
    assert statuses["notify_admin"] is ActionStatus.SUCCESS
    assert outcome.failed_count == 0
    assert should_stop_event(result, config) is True


async def test_wecom_like_platform_degrades_the_same_way(message_factory):
    adapter = wecom_like()
    config = AntiFraudConfig(enabled=True, auto_recall_critical=True, auto_mute_critical=True)
    engine = ActionEngine(adapter, notifier=FakeNotifier())
    outcome = await engine.execute(
        message_factory(SCAM_TEXT, platform="wecom"),
        build_fraud_actions(critical_result(), config),
        bot_role=GroupRole.UNKNOWN,
        origin="antifraud",
    )
    statuses = {item.action: item.status for item in outcome.results}
    assert statuses["recall"] is ActionStatus.UNSUPPORTED
    assert statuses["warn"] is ActionStatus.SUCCESS


async def test_qq_admin_bot_executes_recall_and_mute(message_factory):
    """§208：Bot 为管理员时撤回一小时前的普通成员消息，不得被 2 分钟预检查拦下。"""
    adapter = qq_admin_bot()
    config = AntiFraudConfig(enabled=True, auto_recall_critical=True, auto_mute_critical=True)
    engine = ActionEngine(adapter, notifier=FakeNotifier())
    message = message_factory(SCAM_TEXT, platform="qq_official", age_seconds=3600)
    outcome = await engine.execute(
        message,
        build_fraud_actions(critical_result(), config),
        bot_role=GroupRole.ADMIN,
        origin="antifraud",
    )
    statuses = {item.action: item.status for item in outcome.results}
    assert statuses["recall"] is ActionStatus.SUCCESS
    assert statuses["mute"] is ActionStatus.SUCCESS
    assert adapter.called("recall")
    assert adapter.called("mute")


async def test_qq_member_bot_own_message_aged_out_is_skipped(message_factory):
    """§210：普通机器人撤回自己 3 分钟前的消息 → 策略拒绝（不是异常）。"""
    adapter = qq_member_bot()
    engine = ActionEngine(adapter)
    message = message_factory(
        "测试", platform="qq_official", sender_id="9", bot_id="9", age_seconds=180
    )
    outcome = await engine.execute(
        message,
        [GuardianAction(action=GuardianActionType.RECALL, reason="测试")],
        bot_role=GroupRole.MEMBER,
        origin="antifraud",
    )
    result = outcome.get(GuardianActionType.RECALL)
    assert result is not None
    assert result.status is ActionStatus.SKIPPED
    assert "120 秒" in str(result.message)
    assert adapter.called("recall") == []


async def test_qq_member_bot_own_fresh_message_is_recalled(message_factory):
    """§210：自己 30 秒前的消息应允许尝试撤回。"""
    adapter = qq_member_bot()
    engine = ActionEngine(adapter)
    message = message_factory(
        "测试", platform="qq_official", sender_id="9", bot_id="9", age_seconds=30
    )
    outcome = await engine.execute(
        message,
        [GuardianAction(action=GuardianActionType.RECALL, reason="测试")],
        bot_role=GroupRole.MEMBER,
        origin="antifraud",
    )
    assert outcome.get(GuardianActionType.RECALL).status is ActionStatus.SUCCESS
    assert adapter.called("recall")


async def test_member_bot_cannot_recall_member_message(message_factory):
    """普通成员身份的机器人无成员消息撤回能力 → 能力层直接降级为 UNSUPPORTED。"""
    adapter = qq_member_bot()
    engine = ActionEngine(adapter)
    outcome = await engine.execute(
        message_factory("测试", platform="qq_official", age_seconds=10),
        [GuardianAction(action=GuardianActionType.RECALL, reason="测试")],
        bot_role=GroupRole.MEMBER,
        origin="antifraud",
    )
    assert outcome.results[0].status is ActionStatus.UNSUPPORTED


async def test_protected_target_is_skipped(message_factory):
    """§163：不应对群主/其他管理员执行禁言或踢人。"""
    adapter = qq_admin_bot()
    engine = ActionEngine(adapter)
    outcome = await engine.execute(
        message_factory("测试", platform="qq_official", role=GroupRole.ADMIN),
        [
            GuardianAction(action=GuardianActionType.KICK, reason="测试"),
            GuardianAction(action=GuardianActionType.MUTE, duration=600, reason="测试"),
        ],
        bot_role=GroupRole.ADMIN,
        origin="antifraud",
    )
    assert all(item.status is ActionStatus.SKIPPED for item in outcome.results)
    assert adapter.called("kick") == []


async def test_blacklist_unsupported_on_platform(message_factory):
    """§88：平台不支持黑名单时返回 UNSUPPORTED，而非异常。"""
    adapter = dingtalk_like()
    engine = ActionEngine(adapter)
    outcome = await engine.execute(
        message_factory(SCAM_TEXT, platform="dingtalk"),
        [GuardianAction(action=GuardianActionType.BLACKLIST, reason="测试")],
        origin="antifraud",
    )
    assert outcome.results[0].status is ActionStatus.UNSUPPORTED


async def test_admin_bypass_produces_no_actions(message_factory):
    """§104：放行不产生任何动作，也不会阻断后续处理。"""
    guard = SchoolFraudGuard()
    result = await guard.analyze(
        message_factory(SCAM_TEXT, role=GroupRole.ADMIN), AntiFraudConfig(enabled=True)
    )
    assert build_fraud_actions(result, AntiFraudConfig()) == []
    assert should_stop_event(result, AntiFraudConfig()) is False


async def test_recall_failure_is_reported_not_raised(message_factory):
    """§21：平台 API 报错必须被捕获并归一化。"""
    adapter = qq_admin_bot(fail_recall="机器人没有权限撤回该成员的消息")
    engine = ActionEngine(adapter)
    outcome = await engine.execute(
        message_factory(SCAM_TEXT, platform="qq_official", age_seconds=3600),
        [GuardianAction(action=GuardianActionType.RECALL, reason="测试")],
        bot_role=GroupRole.ADMIN,
        origin="antifraud",
    )
    result = outcome.get(GuardianActionType.RECALL)
    assert result is not None
    assert result.status is ActionStatus.FAILED
    assert result.platform_error
