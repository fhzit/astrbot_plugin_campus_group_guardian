# -*- coding: utf-8 -*-
"""权限模型测试（规格 §36、§37、§53、§164-§166）。"""

from __future__ import annotations

from astrbot_plugin_campus_group_guardian.commands.management import build_permission_context
from astrbot_plugin_campus_group_guardian.core.models import GroupRole
from astrbot_plugin_campus_group_guardian.core.permissions import (
    PermissionContext,
    PermissionLevel,
    target_is_protected,
)
from astrbot_plugin_campus_group_guardian.services.config_service import ConfigService


def make_config_service() -> ConfigService:
    return ConfigService(
        {
            "general": {
                "plugin_admins": ["777"],
                "trusted_sender_ids": ["888"],
            }
        }
    )


def test_plugin_admin_and_group_admin_are_not_interchangeable(message_factory):
    """§36：插件管理员权限 ≠ 群管理员权限。"""
    service = make_config_service()

    plugin_admin = build_permission_context(
        message_factory(sender_id="777", role=GroupRole.MEMBER),
        config_service=service,
        is_astrbot_admin=False,
        bot_role=GroupRole.ADMIN,
    )
    group_admin = build_permission_context(
        message_factory(sender_id="20001", role=GroupRole.ADMIN),
        config_service=service,
        is_astrbot_admin=False,
        bot_role=GroupRole.ADMIN,
    )

    assert plugin_admin.level() is PermissionLevel.PLUGIN_ADMIN
    assert plugin_admin.can_manage_config() is True

    assert group_admin.level() is PermissionLevel.GROUP_ADMIN
    # 群管理员不能改插件配置 / 黑名单等危险写操作
    assert group_admin.can_manage_config() is False
    # 但可以执行群内管理动作
    assert group_admin.can_manage_group() is True


def test_astrbot_admin_is_top_level(message_factory):
    context = build_permission_context(
        message_factory(sender_id="1"),
        config_service=make_config_service(),
        is_astrbot_admin=True,
        bot_role=GroupRole.ADMIN,
    )
    assert context.level() is PermissionLevel.ASTRBOT_ADMIN
    assert context.can_manage_config() is True


def test_normal_member_has_no_management_power(message_factory):
    context = build_permission_context(
        message_factory(sender_id="20001", role=GroupRole.MEMBER),
        config_service=make_config_service(),
        is_astrbot_admin=False,
        bot_role=GroupRole.ADMIN,
    )
    assert context.level() is PermissionLevel.MEMBER
    assert context.can_manage_group() is False
    assert context.can_manage_config() is False


def test_unknown_role_is_treated_as_member(message_factory):
    """§53/§166：角色未知不得当管理员。"""
    context = build_permission_context(
        message_factory(sender_id="20001", role=GroupRole.UNKNOWN),
        config_service=make_config_service(),
        is_astrbot_admin=False,
        bot_role=GroupRole.UNKNOWN,
    )
    assert context.level() is PermissionLevel.UNKNOWN
    assert context.can_manage_group() is False
    assert context.is_group_manager is False


def test_trusted_operator_is_trusted_but_not_manager(message_factory):
    context = build_permission_context(
        message_factory(sender_id="888", role=GroupRole.UNKNOWN),
        config_service=make_config_service(),
        is_astrbot_admin=False,
        bot_role=GroupRole.UNKNOWN,
    )
    assert context.is_trusted() is True
    assert context.can_manage_group() is False
    assert context.level() is PermissionLevel.TRUSTED_OPERATOR


def test_target_protection_rules():
    assert target_is_protected(GroupRole.OWNER) is True
    assert target_is_protected(GroupRole.ADMIN) is True
    assert target_is_protected(GroupRole.MEMBER) is False
    assert target_is_protected(GroupRole.UNKNOWN) is False
    # 机器人自己不受保护
    assert target_is_protected(GroupRole.ADMIN, sender_is_bot=True) is False


def test_bot_admin_flag():
    context = PermissionContext(sender_role=GroupRole.MEMBER, bot_role=GroupRole.ADMIN)
    assert context.bot_is_admin is True
    unknown_bot = PermissionContext(sender_role=GroupRole.MEMBER, bot_role=GroupRole.UNKNOWN)
    assert unknown_bot.bot_is_admin is False
