# -*- coding: utf-8 -*-
"""群管理命令逻辑（与平台无关，统一走 ActionEngine / 适配器）。"""

from __future__ import annotations

import re
from typing import Any

from ..core.actions.models import ActionResult, ActionStatus, GuardianAction, GuardianActionType
from ..core.models import GroupRole, GuardianMessage
from ..core.permissions import PermissionContext

_AT_RE = re.compile(r"\[CQ:at,qq=(\d+)\]|<@!?(\d+)>")
_NUMBER_RE = re.compile(r"\b\d{4,}\b")


def parse_target(args: list[str]) -> str | None:
    """从命令参数里取目标用户 id（支持 @ 或直接写 id）。"""
    for arg in args:
        match = _AT_RE.search(arg)
        if match:
            return match.group(1) or match.group(2)
    for arg in args:
        if _NUMBER_RE.search(arg):
            return _NUMBER_RE.search(arg).group(0)
    return None


def parse_duration_minutes(args: list[str], default: int = 10) -> int:
    """解析时长（分钟），限制在 1~43200 之间。"""
    for arg in args:
        try:
            value = int(float(arg))
        except (TypeError, ValueError):
            continue
        return max(1, min(43200, value))
    return default


def build_permission_context(
    message: GuardianMessage,
    *,
    config_service: Any,
    is_astrbot_admin: bool,
    bot_role: GroupRole,
) -> PermissionContext:
    """构造权限上下文。"""
    snapshot = config_service.global_snapshot()
    return PermissionContext(
        sender_id=message.sender_id,
        sender_role=message.sender_role,
        is_astrbot_admin=is_astrbot_admin,
        plugin_admins=config_service.plugin_admins(snapshot),
        trusted_operators={str(item) for item in snapshot.get_list("general.trusted_sender_ids")},
        bot_role=bot_role,
    )


def format_action_results(action: GuardianActionType, results: list[ActionResult]) -> str:
    """把动作结果渲染成用户可读提示（§35、§174）。"""
    if not results:
        return "未执行任何动作。"
    lines: list[str] = []
    for result in results:
        if result.status is ActionStatus.SUCCESS:
            lines.append(f"✅ 已执行：{result.action}")
        elif result.status is ActionStatus.UNSUPPORTED:
            lines.append(f"— {result.message}")
        elif result.status is ActionStatus.SKIPPED:
            lines.append(f"— {result.message}")
        else:
            lines.append(f"❌ {result.message or '执行失败'}")
    return "\n".join(lines)


__all__ = [
    "parse_target",
    "parse_duration_minutes",
    "build_permission_context",
    "format_action_results",
    "GuardianAction",
]
