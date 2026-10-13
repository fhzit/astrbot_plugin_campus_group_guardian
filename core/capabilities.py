# -*- coding: utf-8 -*-
"""平台能力（Capability）、平台限制（Limits）与撤回策略（RecallPolicy）。

重构核心：业务层只问“这个平台有没有这个能力”，不问“这是哪个平台”。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, Flag, auto

from .models import GroupRole


class PlatformCapability(Flag):
    """平台能力位。NONE 表示仅有最基础的收消息能力。"""

    NONE = 0
    RECEIVE_GROUP_MESSAGE = auto()
    RECEIVE_IMAGE = auto()

    SEND_MESSAGE = auto()
    SEND_IMAGE = auto()
    PROACTIVE_MESSAGE = auto()

    RECALL_OWN_MESSAGE = auto()
    RECALL_MEMBER_MESSAGE = auto()

    MUTE_MEMBER = auto()
    UNMUTE_MEMBER = auto()

    KICK_MEMBER = auto()
    KICK_MEMBERS_BATCH = auto()

    GET_MEMBER_ROLE = auto()
    GET_MEMBER_INFO = auto()
    GET_MEMBER_LIST = auto()

    SET_MEMBER_CARD = auto()
    SET_GROUP_ADMIN = auto()
    SET_GROUP_NAME = auto()

    WHOLE_GROUP_MUTE = auto()

    GROUP_NOTICE = auto()
    GROUP_FILE = auto()

    ESSENCE_MESSAGE = auto()

    REVIEW_JOIN_REQUEST = auto()

    QUERY_BLACKLIST = auto()
    MANAGE_BLACKLIST = auto()


#: 能力中文名，供 WebUI「平台能力」页使用。
CAPABILITY_LABELS: dict[PlatformCapability, str] = {
    PlatformCapability.RECEIVE_GROUP_MESSAGE: "接收群消息",
    PlatformCapability.RECEIVE_IMAGE: "接收图片",
    PlatformCapability.SEND_MESSAGE: "发送消息",
    PlatformCapability.SEND_IMAGE: "发送图片",
    PlatformCapability.PROACTIVE_MESSAGE: "主动消息",
    PlatformCapability.RECALL_OWN_MESSAGE: "撤回机器人自己的消息",
    PlatformCapability.RECALL_MEMBER_MESSAGE: "撤回普通成员消息",
    PlatformCapability.MUTE_MEMBER: "禁言成员",
    PlatformCapability.UNMUTE_MEMBER: "解除禁言",
    PlatformCapability.KICK_MEMBER: "踢出成员",
    PlatformCapability.KICK_MEMBERS_BATCH: "批量移除成员",
    PlatformCapability.GET_MEMBER_ROLE: "读取成员角色",
    PlatformCapability.GET_MEMBER_INFO: "读取成员信息",
    PlatformCapability.GET_MEMBER_LIST: "读取成员列表",
    PlatformCapability.SET_MEMBER_CARD: "设置群名片",
    PlatformCapability.SET_GROUP_ADMIN: "设置管理员",
    PlatformCapability.SET_GROUP_NAME: "修改群名称",
    PlatformCapability.WHOLE_GROUP_MUTE: "全体禁言",
    PlatformCapability.GROUP_NOTICE: "群公告",
    PlatformCapability.GROUP_FILE: "群文件",
    PlatformCapability.ESSENCE_MESSAGE: "精华消息",
    PlatformCapability.REVIEW_JOIN_REQUEST: "入群申请审批",
    PlatformCapability.QUERY_BLACKLIST: "查询群黑名单",
    PlatformCapability.MANAGE_BLACKLIST: "管理群黑名单",
}


@dataclass
class PlatformLimits:
    """能力之外的数量/时限限制。None 表示不限制（或未知）。"""

    normal_bot_own_recall_max_age_seconds: int | None = None
    admin_bot_own_recall_max_age_seconds: int | None = None
    admin_bot_member_recall_max_age_seconds: int | None = None
    mute_max_seconds: int | None = None
    batch_kick_max_size: int | None = None

    def as_dict(self) -> dict[str, int | None]:
        return {
            "normal_bot_own_recall_max_age_seconds": self.normal_bot_own_recall_max_age_seconds,
            "admin_bot_own_recall_max_age_seconds": self.admin_bot_own_recall_max_age_seconds,
            "admin_bot_member_recall_max_age_seconds": self.admin_bot_member_recall_max_age_seconds,
            "mute_max_seconds": self.mute_max_seconds,
            "batch_kick_max_size": self.batch_kick_max_size,
        }


class RecallDecision(Enum):
    """撤回决策结果。"""

    ALLOW = "allow"
    DENY_BY_ROLE = "deny_by_role"
    DENY_BY_POLICY = "deny_by_policy"


class RecallDenyReason(Enum):
    """拒绝撤回的原因（用于日志与用户提示）。"""

    BOT_NOT_PRIVILEGED = "bot_not_privileged"
    TARGET_PROTECTED = "target_protected"
    AGE_LIMIT = "age_limit"
    SELF_MESSAGE_UNSUPPORTED = "self_message_unsupported"


@dataclass
class RecallPolicy:
    """撤回策略。``None`` 表示无时间限制。"""

    normal_bot_own_max_age_seconds: int | None = None
    admin_bot_own_max_age_seconds: int | None = None
    admin_bot_member_max_age_seconds: int | None = None
    can_recall_normal_member: bool = True
    can_recall_other_admin: bool = False
    can_recall_owner: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "normal_bot_own_max_age_seconds": self.normal_bot_own_max_age_seconds,
            "admin_bot_own_max_age_seconds": self.admin_bot_own_max_age_seconds,
            "admin_bot_member_max_age_seconds": self.admin_bot_member_max_age_seconds,
            "can_recall_normal_member": self.can_recall_normal_member,
            "can_recall_other_admin": self.can_recall_other_admin,
            "can_recall_owner": self.can_recall_owner,
        }


@dataclass
class RecallCheck:
    """撤回可行性判定结果。"""

    decision: RecallDecision
    reason: RecallDenyReason | None = None
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision is RecallDecision.ALLOW


def qq_official_recall_policy() -> RecallPolicy:
    """QQ 官方机器人已确认的实际行为。

    普通成员身份：只能撤回自己 2 分钟内的消息。
    管理员身份：可撤回自己与普通成员的消息，不设时间上限；
    但不能撤回其他管理员与群主的消息。
    """
    return RecallPolicy(
        normal_bot_own_max_age_seconds=120,
        admin_bot_own_max_age_seconds=None,
        admin_bot_member_max_age_seconds=None,
        can_recall_normal_member=True,
        can_recall_other_admin=False,
        can_recall_owner=False,
    )


def onebot_recall_policy() -> RecallPolicy:
    """OneBot 协议端：撤回窗口约 2 分钟，与机器人身份无关。"""
    return RecallPolicy(
        normal_bot_own_max_age_seconds=120,
        admin_bot_own_max_age_seconds=120,
        admin_bot_member_max_age_seconds=120,
        can_recall_normal_member=True,
        can_recall_other_admin=False,
        can_recall_owner=False,
    )


def readonly_recall_policy() -> RecallPolicy:
    """企业微信 / 钉钉：当前适配层没有可用的撤回接口。"""
    return RecallPolicy(
        normal_bot_own_max_age_seconds=None,
        admin_bot_own_max_age_seconds=None,
        admin_bot_member_max_age_seconds=None,
        can_recall_normal_member=False,
        can_recall_other_admin=False,
        can_recall_owner=False,
    )


def decide_recall(
    policy: RecallPolicy,
    bot_role: GroupRole,
    target_role: GroupRole,
    is_bot_message: bool,
    age_seconds: float,
) -> RecallCheck:
    """决定是否尝试撤回。

    逻辑只依赖角色与年龄，不依赖平台名，因此可被四种平台共用并被单元测试覆盖。

    Args:
        policy: 平台撤回策略。
        bot_role: 机器人在该群的角色。
        target_role: 被撤回消息发送者的角色。
        is_bot_message: 目标消息是否由机器人自己发送。
        age_seconds: 消息年龄（秒）。

    Returns:
        撤回判定结果。
    """
    if is_bot_message:
        # 自己的消息：普通成员受时限约束，管理员不受限。
        if bot_role is GroupRole.ADMIN or bot_role is GroupRole.OWNER:
            if policy.admin_bot_own_max_age_seconds is None:
                return RecallCheck(RecallDecision.ALLOW, detail="管理员撤回自己的消息，无时间限制")
            limit = policy.admin_bot_own_max_age_seconds
        else:
            if policy.normal_bot_own_max_age_seconds is None:
                return RecallCheck(RecallDecision.ALLOW, detail="平台未限制撤回自己的消息")
            limit = policy.normal_bot_own_max_age_seconds
        if age_seconds > limit:
            return RecallCheck(
                RecallDecision.DENY_BY_POLICY,
                RecallDenyReason.AGE_LIMIT,
                f"消息已超过 {limit} 秒撤回时限",
            )
        return RecallCheck(RecallDecision.ALLOW, detail=f"在 {limit} 秒撤回时限内")

    # 撤回他人消息：需要群管理员/群主身份。
    if bot_role is not GroupRole.ADMIN and bot_role is not GroupRole.OWNER:
        return RecallCheck(
            RecallDecision.DENY_BY_ROLE,
            RecallDenyReason.BOT_NOT_PRIVILEGED,
            "机器人不是群管理员，无法撤回其他成员的消息",
        )
    if target_role is GroupRole.OWNER:
        if not policy.can_recall_owner:
            return RecallCheck(
                RecallDecision.DENY_BY_ROLE,
                RecallDenyReason.TARGET_PROTECTED,
                "群主的消息不可撤回",
            )
    elif target_role is GroupRole.ADMIN:
        if not policy.can_recall_other_admin:
            return RecallCheck(
                RecallDecision.DENY_BY_ROLE,
                RecallDenyReason.TARGET_PROTECTED,
                "其他群管理员的消息不可撤回",
            )
    else:
        if not policy.can_recall_normal_member:
            return RecallCheck(
                RecallDecision.DENY_BY_ROLE,
                RecallDenyReason.BOT_NOT_PRIVILEGED,
                "当前平台不支持撤回普通成员消息",
            )
    limit = policy.admin_bot_member_max_age_seconds
    if limit is not None and age_seconds > limit:
        return RecallCheck(
            RecallDecision.DENY_BY_POLICY,
            RecallDenyReason.AGE_LIMIT,
            f"消息已超过 {limit} 秒撤回时限",
        )
    return RecallCheck(RecallDecision.ALLOW, detail="机器人以管理员身份撤回普通成员消息")


@dataclass
class PlatformProfile:
    """某平台的静态能力画像（WebUI「平台能力」页直接展示）。"""

    name: str
    capabilities: PlatformCapability = PlatformCapability.NONE
    limits: PlatformLimits = field(default_factory=PlatformLimits)
    recall_policy: RecallPolicy = field(default_factory=RecallPolicy)
    notes: list[str] = field(default_factory=list)

    def supports(self, capability: PlatformCapability) -> bool:
        return bool(self.capabilities & capability)

    def capability_matrix(self) -> list[dict[str, object]]:
        """逐项列出支持/不支持，供前端渲染。"""
        rows = []
        for capability, label in CAPABILITY_LABELS.items():
            rows.append(
                {
                    "capability": capability.name,
                    "label": label,
                    "supported": self.supports(capability),
                }
            )
        return rows
