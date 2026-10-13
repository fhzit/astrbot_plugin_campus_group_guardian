# -*- coding: utf-8 -*-
"""ActionEngine：决策 → 能力检查 → 权限检查 → 平台动作 → ActionResult → 落库（§102）。

关键点：
- 平台不支持的动作用 UNSUPPORTED 表示，绝不整单失败（§30、§181）；
- 撤回前同时检查机器人角色与目标角色（§20）；
- 每次执行都接受平台 API 的最终结果，Cache 不是事实来源（§165）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from ..capabilities import PlatformCapability
from ..models import GroupRole, GuardianMessage
from .models import (
    ACTION_CAPABILITY,
    ACTION_LABELS,
    ActionResult,
    ActionStatus,
    GuardianAction,
    GuardianActionType,
    make_skipped,
    make_success,
    make_unsupported,
)

_LOG = "[Guardian][Action]"


@dataclass
class ActionOutcome:
    """一次动作批次的执行结果汇总。"""

    results: list[ActionResult] = field(default_factory=list)

    @property
    def executed_count(self) -> int:
        return sum(1 for result in self.results if result.actionable)

    @property
    def success_count(self) -> int:
        return sum(1 for result in self.results if result.status is ActionStatus.SUCCESS)

    @property
    def unsupported_count(self) -> int:
        return sum(1 for result in self.results if result.status is ActionStatus.UNSUPPORTED)

    @property
    def failed_count(self) -> int:
        return sum(1 for result in self.results if result.status is ActionStatus.FAILED)

    def get(self, action: GuardianActionType) -> ActionResult | None:
        for result in self.results:
            if result.action == action.value:
                return result
        return None

    def to_list(self) -> list[dict[str, object]]:
        return [result.to_dict() for result in self.results]


class ActionEngine:
    """统一动作执行器。"""

    def __init__(
        self,
        adapter: Any,
        *,
        storage: Any = None,
        notifier: Any = None,
    ) -> None:
        self.adapter = adapter
        self.storage = storage
        self.notifier = notifier

    async def execute(
        self,
        message: GuardianMessage,
        actions: list[GuardianAction],
        *,
        bot_role: GroupRole = GroupRole.UNKNOWN,
        target_role: GroupRole | None = None,
        origin: str = "auto",
    ) -> ActionOutcome:
        """按顺序执行动作，逐个记录结果。"""
        outcome = ActionOutcome()
        effective_target_role = target_role or message.sender_role
        for action in actions:
            result = await self._execute_one(
                message,
                action,
                bot_role=bot_role,
                target_role=effective_target_role,
                origin=origin,
            )
            outcome.results.append(result)
            if self.storage is not None:
                try:
                    await self.storage.record_action(
                        message=message,
                        action=action.action.value,
                        status=result.status.value,
                        error=result.platform_error,
                        message_text=result.message,
                        origin=origin,
                    )
                except Exception as exc:  # 落库失败不得影响动作结果
                    logger.debug(f"{_LOG} 动作落库失败: {exc}")
        return outcome

    async def _execute_one(
        self,
        message: GuardianMessage,
        action: GuardianAction,
        *,
        bot_role: GroupRole,
        target_role: GroupRole,
        origin: str,
    ) -> ActionResult:
        action_type = action.action

        if action_type is GuardianActionType.STOP_EVENT:
            return make_success("stop_event", "已请求阻断后续处理", origin=origin)

        if action_type is GuardianActionType.NOTIFY_ADMIN:
            return await self._notify_admin(message, action, origin=origin)

        is_bot_message = message.sender_id == getattr(message, "bot_id", None)
        required = ACTION_CAPABILITY.get(action_type)
        if action_type is GuardianActionType.RECALL:
            # 撤回自己的消息与撤回他人消息是两个不同的能力（§18、§161）：
            # 普通成员身份的机器人也能撤回自己的消息，因此这里按目标区分。
            required = (
                PlatformCapability.RECALL_OWN_MESSAGE
                if is_bot_message
                else PlatformCapability.RECALL_MEMBER_MESSAGE
            )
        if required is not None and not self.adapter.supports(required):
            return make_unsupported(
                action_type.value,
                f"当前平台不支持“{ACTION_LABELS[action_type]}”，已降级为记录与提醒。",
            )

        if action_type is GuardianActionType.RECALL:
            return await self._recall(message, action, bot_role=bot_role, target_role=target_role)

        if action_type in (
            GuardianActionType.MUTE,
            GuardianActionType.KICK,
            GuardianActionType.BLACKLIST,
        ):
            if target_role is GroupRole.OWNER or target_role is GroupRole.ADMIN:
                return make_skipped(
                    action_type.value,
                    f"目标成员是{target_role.value}，受平台保护，不执行{ACTION_LABELS[action_type]}。",
                )

        try:
            if action_type is GuardianActionType.MUTE:
                result = await self.adapter.mute_member(
                    message, action.target_id or message.sender_id, action.duration, action.reason
                )
            elif action_type is GuardianActionType.UNMUTE:
                result = await self.adapter.unmute_member(message, action.target_id or message.sender_id)
            elif action_type is GuardianActionType.KICK:
                result = await self.adapter.kick_member(
                    message, action.target_id or message.sender_id, action.reason
                )
            elif action_type is GuardianActionType.BLACKLIST:
                result = await self.adapter.add_blacklist(message, action.target_id or message.sender_id)
            elif action_type is GuardianActionType.WARN:
                return await self._warn(message, action)
            else:
                return make_unsupported(action_type.value, f"未实现的动作：{action_type.value}")
        except Exception as exc:  # 适配器内部已捕获，这里再兜底一层
            logger.debug(f"{_LOG} 动作 {action_type.value} 执行异常: {exc}")
            return self.adapter.failed(action_type.value, exc)

        if result.ok:
            logger.info(
                f"{_LOG} {ACTION_LABELS[action_type]} 成功：群={message.group_id} "
                f"目标={action.target_id or message.sender_id} 来源={origin}"
            )
        elif result.status is ActionStatus.FAILED:
            logger.warning(
                f"{_LOG} {ACTION_LABELS[action_type]} 失败：群={message.group_id} "
                f"原因={result.message} 原始错误={result.platform_error}"
            )
        else:
            logger.info(f"{_LOG} {ACTION_LABELS[action_type]} 未执行：{result.message}")
        return result

    async def _recall(
        self,
        message: GuardianMessage,
        action: GuardianAction,
        *,
        bot_role: GroupRole,
        target_role: GroupRole,
    ) -> ActionResult:
        """撤回：先按平台撤回策略判定，再调用平台。"""
        is_bot_message = message.sender_id == getattr(message, "bot_id", None)
        check = self.adapter.check_recall(
            message,
            bot_role=bot_role,
            target_role=target_role,
            is_bot_message=is_bot_message,
        )
        if not check.allowed:
            return self.adapter.recall_denied_result(check)
        try:
            result = await self.adapter.recall_message(message, is_bot_message=is_bot_message)
        except Exception as exc:
            return self.adapter.failed("recall", exc)
        if result.ok:
            logger.info(f"{_LOG} 撤回成功：群={message.group_id} 消息={message.message_id}")
        else:
            logger.warning(f"{_LOG} 撤回失败：{result.message}（{result.error_type}）")
        return result

    async def _warn(self, message: GuardianMessage, action: GuardianAction) -> ActionResult:
        if not self.adapter.supports(PlatformCapability.SEND_MESSAGE):
            return make_unsupported("warn", "当前平台不支持发送群消息，无法发出群内预警。")
        text = str(action.metadata.get("text") or action.reason or "")
        if not text:
            return make_skipped("warn", "警告文案为空，跳过发送。")
        result = await self.adapter.send_message(message.unified_msg_origin, text)
        # 适配器内部的动作名可能是 send，统一成 warn，保证动作日志与结果一致。
        result.action = GuardianActionType.WARN.value
        return result

    async def _notify_admin(self, message: GuardianMessage, action: GuardianAction, *, origin: str) -> ActionResult:
        if self.notifier is None:
            return make_skipped("notify_admin", "未配置管理员通知渠道。")
        try:
            delivered = await self.notifier.notify_admin(message, action)
        except Exception as exc:
            return self.adapter.failed("notify_admin", exc)
        if delivered:
            return make_success("notify_admin", "已通知插件管理员", origin=origin)
        return make_skipped("notify_admin", "没有可用的管理员会话，通知未发送。")
