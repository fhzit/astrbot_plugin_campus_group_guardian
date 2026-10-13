# -*- coding: utf-8 -*-
"""测试共用的 Mock 平台适配器（对齐规格 §182：不连真实群）。"""

from __future__ import annotations

from typing import Any

from astrbot_plugin_campus_group_guardian.core.actions.models import make_success
from astrbot_plugin_campus_group_guardian.core.capabilities import (
    PlatformCapability,
    PlatformLimits,
    PlatformProfile,
    RecallPolicy,
    onebot_recall_policy,
    qq_official_recall_policy,
    readonly_recall_policy,
)
from astrbot_plugin_campus_group_guardian.core.models import GroupRole, GuardianMessage
from astrbot_plugin_campus_group_guardian.platforms.base import (
    GuardianPlatformAdapter,
    MemberInfo,
)


class MockAdapter(GuardianPlatformAdapter):
    """内存适配器：记录所有调用，可按需开关能力。"""

    platform_name = "mock"

    def __init__(
        self,
        capability: PlatformCapability,
        *,
        recall_policy: RecallPolicy | None = None,
        limits: PlatformLimits | None = None,
        roles: dict[str, GroupRole] | None = None,
        bot_role: GroupRole = GroupRole.MEMBER,
        fail_recall: str | None = None,
        support_blacklist: bool = False,
        profile_name: str = "mock",
    ) -> None:
        profile = PlatformProfile(
            name=profile_name,
            capabilities=capability,
            limits=limits or PlatformLimits(),
            recall_policy=recall_policy or readonly_recall_policy(),
        )
        super().__init__(context=None, profile=profile)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.roles = roles or {}
        self._bot_role = bot_role
        self.fail_recall = fail_recall
        self.support_blacklist = support_blacklist

    # ---- 角色 ----
    async def get_bot_role(self, message: GuardianMessage) -> GroupRole:
        return self._bot_role

    async def get_member_role(self, message: GuardianMessage, user_id: str) -> GroupRole:
        return self.roles.get(user_id, GroupRole.MEMBER)

    async def get_member_info(
        self, message: GuardianMessage, user_id: str
    ) -> MemberInfo | None:
        return MemberInfo(
            user_id=user_id,
            name=f"成员{user_id}",
            role=await self.get_member_role(message, user_id),
        )

    # ---- 动作 ----
    def _record(self, name: str, **params: Any) -> None:
        self.calls.append((name, params))

    def called(self, name: str) -> list[dict[str, Any]]:
        return [params for called_name, params in self.calls if called_name == name]

    async def recall_message(
        self, message: GuardianMessage, is_bot_message: bool = False
    ) -> Any:
        self._record("recall", message_id=message.message_id, is_bot=is_bot_message)
        if self.fail_recall:
            return self.failed("recall", self.fail_recall, retcode=11253)
        return make_success("recall", "已撤回")

    async def mute_member(
        self, message: GuardianMessage, user_id: str, duration: int, reason: str = ""
    ) -> Any:
        self._record("mute", user_id=user_id, duration=duration, reason=reason)
        return make_success("mute", f"已禁言 {duration} 秒")

    async def unmute_member(self, message: GuardianMessage, user_id: str) -> Any:
        self._record("unmute", user_id=user_id)
        return make_success("unmute", "已解除禁言")

    async def kick_member(
        self, message: GuardianMessage, user_id: str, reason: str = ""
    ) -> Any:
        self._record("kick", user_id=user_id, reason=reason)
        return make_success("kick", "已踢出")

    async def add_blacklist(self, message: GuardianMessage, user_id: str) -> Any:
        self._record("add_blacklist", user_id=user_id)
        if not self.support_blacklist:
            return self.unsupported("blacklist", "当前平台不支持群黑名单。")
        return make_success("blacklist", "已加入黑名单")

    async def send_message(self, session: str, text: str) -> Any:
        self._record("send", session=session, text=text)
        return make_success("send", "已发送")


def dingtalk_like(profile_name: str = "dingtalk", **kwargs: Any) -> MockAdapter:
    """钉钉/企业微信风格：只能收发文本与图片（§29、§213）。"""
    return MockAdapter(
        PlatformCapability.RECEIVE_GROUP_MESSAGE
        | PlatformCapability.RECEIVE_IMAGE
        | PlatformCapability.SEND_MESSAGE
        | PlatformCapability.SEND_IMAGE
        | PlatformCapability.PROACTIVE_MESSAGE,
        recall_policy=readonly_recall_policy(),
        profile_name=profile_name,
        **kwargs,
    )


def wecom_like(**kwargs: Any) -> MockAdapter:
    """企业微信风格（§27、§212）。"""
    return dingtalk_like("wecom", **kwargs)


def qq_admin_bot(**kwargs: Any) -> MockAdapter:
    """QQ 官方机器人为管理员：可撤回普通成员消息且无时限。"""
    return MockAdapter(
        PlatformCapability.RECEIVE_GROUP_MESSAGE
        | PlatformCapability.RECEIVE_IMAGE
        | PlatformCapability.SEND_MESSAGE
        | PlatformCapability.SEND_IMAGE
        | PlatformCapability.PROACTIVE_MESSAGE
        | PlatformCapability.RECALL_MEMBER_MESSAGE
        | PlatformCapability.RECALL_OWN_MESSAGE
        | PlatformCapability.MUTE_MEMBER
        | PlatformCapability.UNMUTE_MEMBER
        | PlatformCapability.KICK_MEMBER
        | PlatformCapability.GET_MEMBER_ROLE,
        recall_policy=qq_official_recall_policy(),
        limits=PlatformLimits(mute_max_seconds=30 * 24 * 3600, batch_kick_max_size=20),
        bot_role=GroupRole.ADMIN,
        profile_name="qq_official",
        **kwargs,
    )


def qq_member_bot(**kwargs: Any) -> MockAdapter:
    """QQ 官方机器人是普通成员。"""
    return MockAdapter(
        PlatformCapability.RECEIVE_GROUP_MESSAGE
        | PlatformCapability.SEND_MESSAGE
        | PlatformCapability.RECALL_OWN_MESSAGE
        | PlatformCapability.GET_MEMBER_ROLE,
        recall_policy=qq_official_recall_policy(),
        limits=PlatformLimits(normal_bot_own_recall_max_age_seconds=120),
        bot_role=GroupRole.MEMBER,
        profile_name="qq_official",
        **kwargs,
    )


def onebot_adapter(**kwargs: Any) -> MockAdapter:
    """OneBot v11 协议端（NapCat/LLOneBot 风格）。"""
    return MockAdapter(
        PlatformCapability.RECEIVE_GROUP_MESSAGE
        | PlatformCapability.RECEIVE_IMAGE
        | PlatformCapability.SEND_MESSAGE
        | PlatformCapability.SEND_IMAGE
        | PlatformCapability.PROACTIVE_MESSAGE
        | PlatformCapability.RECALL_MEMBER_MESSAGE
        | PlatformCapability.RECALL_OWN_MESSAGE
        | PlatformCapability.MUTE_MEMBER
        | PlatformCapability.UNMUTE_MEMBER
        | PlatformCapability.KICK_MEMBER
        | PlatformCapability.GET_MEMBER_ROLE
        | PlatformCapability.GET_MEMBER_INFO
        | PlatformCapability.GET_MEMBER_LIST,
        recall_policy=onebot_recall_policy(),
        bot_role=GroupRole.ADMIN,
        profile_name="aiocqhttp",
        **kwargs,
    )
