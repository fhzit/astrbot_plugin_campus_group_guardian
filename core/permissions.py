# -*- coding: utf-8 -*-
"""权限模型：区分 AstrBot 管理员、插件管理员、可信操作者与平台群角色。

关键约束（不可简化）：
- 插件管理员权限 ≠ 群管理员权限，不能互相替代；
- LLM 工具入口不得放宽聊天指令的权限；
- 角色 UNKNOWN 一律按普通成员处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .models import GroupRole


class PermissionLevel(Enum):
    """操作者身份等级（按宽松度递减）。"""

    ASTRBOT_ADMIN = "astrbot_admin"
    PLUGIN_ADMIN = "plugin_admin"
    TRUSTED_OPERATOR = "trusted_operator"
    GROUP_OWNER = "group_owner"
    GROUP_ADMIN = "group_admin"
    MEMBER = "member"
    UNKNOWN = "unknown"


_LEVEL_LABELS = {
    PermissionLevel.ASTRBOT_ADMIN: "AstrBot 管理员",
    PermissionLevel.PLUGIN_ADMIN: "插件管理员",
    PermissionLevel.TRUSTED_OPERATOR: "可信操作者",
    PermissionLevel.GROUP_OWNER: "群主",
    PermissionLevel.GROUP_ADMIN: "群管理员",
    PermissionLevel.MEMBER: "普通成员",
    PermissionLevel.UNKNOWN: "未知身份",
}


@dataclass
class PermissionContext:
    """一次操作的身份上下文。"""

    sender_id: str = ""
    sender_role: GroupRole = GroupRole.UNKNOWN
    is_astrbot_admin: bool = False
    plugin_admins: set[str] = field(default_factory=set)
    trusted_operators: set[str] = field(default_factory=set)
    bot_role: GroupRole = GroupRole.UNKNOWN

    def level(self) -> PermissionLevel:
        """当前操作者的最高身份等级。"""
        if self.is_astrbot_admin:
            return PermissionLevel.ASTRBOT_ADMIN
        if self.sender_id and self.sender_id in self.plugin_admins:
            return PermissionLevel.PLUGIN_ADMIN
        if self.sender_role is GroupRole.OWNER:
            return PermissionLevel.GROUP_OWNER
        if self.sender_role is GroupRole.ADMIN:
            return PermissionLevel.GROUP_ADMIN
        if self.sender_id and self.sender_id in self.trusted_operators:
            return PermissionLevel.TRUSTED_OPERATOR
        if self.sender_role is GroupRole.MEMBER:
            return PermissionLevel.MEMBER
        return PermissionLevel.UNKNOWN

    def level_label(self) -> str:
        return _LEVEL_LABELS[self.level()]

    @property
    def is_plugin_admin(self) -> bool:
        level = self.level()
        return level in (PermissionLevel.ASTRBOT_ADMIN, PermissionLevel.PLUGIN_ADMIN)

    @property
    def is_group_manager(self) -> bool:
        return self.sender_role in (GroupRole.OWNER, GroupRole.ADMIN)

    def can_manage_group(self) -> bool:
        """是否能执行群管理类操作：插件管理员、群主或群管理员。"""
        return self.is_plugin_admin or self.is_group_manager

    def can_manage_config(self) -> bool:
        """危险写操作（黑名单、规则、插件配置）只看插件管理员。"""
        return self.is_plugin_admin

    def is_trusted(self) -> bool:
        """可信操作者：用于企微/钉钉等拿不到完整角色信息的平台。

        可信只影响反诈放行判定，不等价于群管理员权限。
        """
        return self.sender_id in self.trusted_operators or self.is_plugin_admin

    @property
    def bot_is_admin(self) -> bool:
        return self.bot_role in (GroupRole.ADMIN, GroupRole.OWNER)


def target_is_protected(target_role: GroupRole, sender_is_bot: bool = False) -> bool:
    """目标角色是否受保护（不允许被执行踢人/禁言等管理动作）。

    机器人自己不算受保护对象，否则自检逻辑会被误判。
    """
    if sender_is_bot:
        return False
    return target_role in (GroupRole.OWNER, GroupRole.ADMIN)
