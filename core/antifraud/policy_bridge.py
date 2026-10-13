# -*- coding: utf-8 -*-
"""风险等级 → 动作名称的轻量映射，供命令层与 WebUI 测试器展示“预计动作”。

真正的动作构造在 core/actions/policy.py，这里只做展示，避免命令层依赖配置对象。
"""

from __future__ import annotations

from ..core.antifraud.models import FraudRiskLevel


def plan_actions_for_level(level: FraudRiskLevel, bypassed: bool = False) -> list[str]:
    """按等级给出预计动作名称（不读取配置，只用于提示）。"""
    if bypassed or level is FraudRiskLevel.SAFE:
        return []
    if level is FraudRiskLevel.SUSPICIOUS:
        return ["记录事件", "通知管理员"]
    if level is FraudRiskLevel.HIGH:
        return ["记录事件", "通知管理员（可选自动撤回）"]
    return ["撤回消息", "临时禁言", "群内预警", "记录事件", "通知管理员"]
