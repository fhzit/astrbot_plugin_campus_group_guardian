# -*- coding: utf-8 -*-
"""统一动作模型：动作类型、处罚、执行结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..capabilities import PlatformCapability
from ..errors import GuardianPlatformErrorType, to_platform_error, user_message


class GuardianActionType(Enum):
    """统一动作类型。"""

    WARN = "warn"
    RECALL = "recall"
    MUTE = "mute"
    UNMUTE = "unmute"
    KICK = "kick"
    BLACKLIST = "blacklist"
    NOTIFY_ADMIN = "notify_admin"
    STOP_EVENT = "stop_event"


#: 动作 → 所需平台能力（None 表示不需要平台 API）。
ACTION_CAPABILITY: dict[GuardianActionType, PlatformCapability | None] = {
    GuardianActionType.WARN: PlatformCapability.SEND_MESSAGE,
    GuardianActionType.RECALL: PlatformCapability.RECALL_MEMBER_MESSAGE,
    GuardianActionType.MUTE: PlatformCapability.MUTE_MEMBER,
    GuardianActionType.UNMUTE: PlatformCapability.UNMUTE_MEMBER,
    GuardianActionType.KICK: PlatformCapability.KICK_MEMBER,
    GuardianActionType.BLACKLIST: PlatformCapability.MANAGE_BLACKLIST,
    GuardianActionType.NOTIFY_ADMIN: PlatformCapability.SEND_MESSAGE,
    GuardianActionType.STOP_EVENT: None,
}

ACTION_LABELS: dict[GuardianActionType, str] = {
    GuardianActionType.WARN: "群内警告",
    GuardianActionType.RECALL: "撤回消息",
    GuardianActionType.MUTE: "禁言",
    GuardianActionType.UNMUTE: "解除禁言",
    GuardianActionType.KICK: "踢出成员",
    GuardianActionType.BLACKLIST: "加入黑名单",
    GuardianActionType.NOTIFY_ADMIN: "通知管理员",
    GuardianActionType.STOP_EVENT: "阻断后续处理",
}


class ActionStatus(Enum):
    """动作执行状态。"""

    SUCCESS = "success"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    SKIPPED = "skipped"


@dataclass
class Punishment:
    """逻辑处罚。平台适配层负责把 duration 落到平台可接受的形态。"""

    action: GuardianActionType
    duration: int = 0  # 秒；0 表示不适用或永久
    reason: str = ""


@dataclass
class GuardianAction:
    """一次待执行的动作。"""

    action: GuardianActionType
    reason: str = ""
    duration: int = 0
    target_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class ActionResult:
    """动作执行结果。平台不支持时必须是 UNSUPPORTED，而不是抛异常。"""

    action: str
    status: ActionStatus
    message: str | None = None
    platform_error: str | None = None
    error_type: GuardianPlatformErrorType | None = None
    detail: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is ActionStatus.SUCCESS

    @property
    def actionable(self) -> bool:
        """是否属于“已尝试执行”的结果（成功或失败）。"""
        return self.status in (ActionStatus.SUCCESS, ActionStatus.FAILED)

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "status": self.status.value,
            "message": self.message,
            "platform_error": self.platform_error,
            "error_type": self.error_type.value if self.error_type else None,
            "detail": self.detail,
        }


def unsupported(action: GuardianActionType, message: str | None = None) -> ActionResult:
    """构造 UNSUPPORTED 结果。"""
    return ActionResult(
        action=action.value,
        status=ActionStatus.UNSUPPORTED,
        message=message or f"当前平台机器人接口不支持{ACTION_LABELS.get(action, action.value)}。",
    )


def make_unsupported(action: str, message: str | None = None) -> ActionResult:
    """按动作名构造 UNSUPPORTED 结果（适配层用，动作名可来自平台 API 名）。"""
    return ActionResult(
        action=action,
        status=ActionStatus.UNSUPPORTED,
        message=message or f"{action}：当前平台机器人接口不支持该操作。",
    )


def make_skipped(action: str, message: str) -> ActionResult:
    """按动作名构造 SKIPPED 结果（策略主动跳过，不是失败）。"""
    return ActionResult(action=action, status=ActionStatus.SKIPPED, message=message)


def make_success(action: str, message: str | None = None, **detail: object) -> ActionResult:
    return ActionResult(action=action, status=ActionStatus.SUCCESS, message=message, detail=dict(detail))


def make_failed(
    action: str,
    error: BaseException | str,
    retcode: int | None = None,
) -> ActionResult:
    """把平台异常转成 FAILED 结果，并带上归一化错误类型。"""
    platform_error = to_platform_error(error, retcode)
    return ActionResult(
        action=action,
        status=ActionStatus.FAILED,
        message=user_message(platform_error.error_type),
        platform_error=platform_error.raw,
        error_type=platform_error.error_type,
    )
