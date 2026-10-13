# -*- coding: utf-8 -*-
"""处罚策略：风险等级 → 动作序列（§86、§87、§88、§103、§104）。"""

from __future__ import annotations

from ..antifraud.models import FraudResult, FraudRiskLevel
from .models import GuardianAction, GuardianActionType


def build_fraud_actions(result: FraudResult, config) -> list[GuardianAction]:
    """按风险等级生成动作序列。

    SAFE      → 无动作
    SUSPICIOUS→ 仅记录（由调用方落库）
    HIGH      → 记录 + 管理员提醒（可选自动撤回）
    CRITICAL  → 撤回 + 临时禁言 + 群内预警 + 事件留痕 + 通知管理员
                （踢人/黑名单默认关闭，严格模式才开启）
    """
    if result.bypassed or result.level is FraudRiskLevel.SAFE:
        return []

    actions: list[GuardianAction] = []
    reason = f"家校缴费诈骗风险 {result.score:g}（{result.level.label}）"

    if result.level is FraudRiskLevel.SUSPICIOUS:
        actions.append(
            GuardianAction(
                action=GuardianActionType.NOTIFY_ADMIN,
                reason=f"{reason}：仅记录，供人工复核",
                metadata={"only_record": True},
            )
        )
        return actions

    if result.level is FraudRiskLevel.HIGH:
        if getattr(config, "auto_recall_high", False):
            actions.append(GuardianAction(action=GuardianActionType.RECALL, reason=reason))
        actions.append(
            GuardianAction(
                action=GuardianActionType.NOTIFY_ADMIN,
                reason=f"{reason}：请人工确认",
            )
        )
        return actions

    # CRITICAL
    if getattr(config, "auto_recall_critical", True):
        actions.append(GuardianAction(action=GuardianActionType.RECALL, reason=reason))
    if getattr(config, "auto_mute_critical", True):
        actions.append(
            GuardianAction(
                action=GuardianActionType.MUTE,
                duration=int(getattr(config, "critical_mute_seconds", 600) or 600),
                reason=reason,
            )
        )
    if getattr(config, "auto_kick_critical", False):
        actions.append(GuardianAction(action=GuardianActionType.KICK, reason=reason))
    if getattr(config, "auto_blacklist_critical", False):
        actions.append(GuardianAction(action=GuardianActionType.BLACKLIST, reason=reason))
    if getattr(config, "group_warning_enabled", True):
        actions.append(GuardianAction(action=GuardianActionType.WARN, reason=reason))
    actions.append(GuardianAction(action=GuardianActionType.NOTIFY_ADMIN, reason=reason))
    return actions


def should_stop_event(result: FraudResult, config) -> bool:
    """是否需要阻断 AstrBot 后续处理（§103、§104）。

    只有已确认的极高风险诈骗才阻断；群主/管理员的放行分支永远不阻断，
    以免影响其他插件与 AstrBot 的正常流程。
    """
    if result.bypassed:
        return False
    if result.level is not FraudRiskLevel.CRITICAL:
        return False
    return bool(getattr(config, "stop_event_on_critical", True))


def incident_required(result: FraudResult) -> bool:
    """是否需要建立反诈事件记录。"""
    return result.is_risky or result.level is FraudRiskLevel.SUSPICIOUS
