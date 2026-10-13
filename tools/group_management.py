# -*- coding: utf-8 -*-
"""LLM 群管工具的实现（带强制权限校验）。

关键约束（§38、§105、§106）：
- LLM 决定调用工具不等于授权，执行前必须做后端权限校验；
- 平台不支持的动作返回明确文案，不抛异常；
- 所有工具最终走 ActionEngine，不直接调用平台 API。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from ..core.actions.engine import ActionEngine
from ..core.actions.models import (
    ActionStatus,
    GuardianAction,
    GuardianActionType,
)
from ..core.models import GroupRole, GuardianMessage

_LOG = "[Guardian][Tool]"

#: 工具名 → 动作类型。
TOOL_ACTIONS = {
    "ban_group_member": GuardianActionType.MUTE,
    "unban_group_member": GuardianActionType.UNMUTE,
    "kick_group_member": GuardianActionType.KICK,
    "recall_message": GuardianActionType.RECALL,
}


def _format(results) -> str:
    lines = []
    for result in results:
        if result.status is ActionStatus.SUCCESS:
            lines.append("操作已执行。")
        elif result.status in (ActionStatus.UNSUPPORTED, ActionStatus.SKIPPED):
            lines.append(result.message or "操作未执行。")
        else:
            lines.append(f"操作失败：{result.message}")
    return "\n".join(lines) or "未执行任何操作。"


async def execute_management_tool(
    plugin: Any,
    event: Any,
    tool: str,
    params: dict[str, Any],
    *,
    user_id: str | None = None,
    duration_minutes: int = 10,
) -> str:
    """统一的 LLM 工具执行入口。

    Args:
        plugin: 插件实例（提供 normalize_event / 适配器 / 权限上下文）。
        event: AstrBot 事件。
        tool: 工具名。
        params: 工具参数。
        user_id: 目标用户 id。
        duration_minutes: 禁言时长（分钟）。

    Returns:
        面向 LLM 与用户的文本结果。
    """
    action_type = TOOL_ACTIONS.get(tool)
    if action_type is None:
        return f"未知工具：{tool}"

    message: GuardianMessage | None = await plugin.normalize_event(event)
    if message is None:
        return "该工具只能在群聊中使用。"

    permission = await plugin.permission_for(message, event)
    if not permission.can_manage_group():
        return f"没有权限执行该操作（当前身份：{permission.level_label()}）。"

    adapter = plugin.registry.create(message.platform)
    if adapter is None:
        return f"当前平台（{message.platform}）暂不支持该操作。"

    target_id = user_id or params.get("user_id") or params.get("message_id")
    if action_type is GuardianActionType.RECALL:
        if not params.get("message_id"):
            return "缺少 message_id 参数。"
        message = plugin.message_with_id(message, str(params["message_id"]))

    bot_role = await plugin.bot_role_for(message)
    engine = ActionEngine(adapter, storage=plugin.storage, notifier=plugin.notifier)
    action = GuardianAction(
        action=action_type,
        duration=(int(duration_minutes) * 60) if action_type is GuardianActionType.MUTE else 0,
        reason="LLM 工具调用",
        target_id=str(target_id) if target_id else None,
    )
    outcome = await engine.execute(
        message,
        [action],
        bot_role=bot_role,
        origin="llm_tool",
    )
    text = _format(outcome.results)
    logger.info(f"{_LOG} LLM 工具 {tool} 结果：{[r.status.value for r in outcome.results]}")
    return text
