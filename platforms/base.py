# -*- coding: utf-8 -*-
"""平台适配层抽象基类。

约定：
- 业务层只调用这里的统一方法，绝不出现 ``if platform == ...``；
- 平台不支持的操作用基类实现返回 ``UNSUPPORTED``，不抛 NotImplementedError；
- 平台真实报错必须被捕获并归一化成 ActionResult（理论 Capability 不代表一定成功）。
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import Any

from ..core.capabilities import (
    PlatformCapability,
    PlatformLimits,
    PlatformProfile,
    RecallCheck,
    RecallDecision,
    RecallDenyReason,
    decide_recall,
)
from ..core.actions.models import (
    ActionResult,
    make_failed,
    make_skipped,
    make_success,
    make_unsupported,
)
from ..core.errors import GuardianPlatformError
from ..core.models import GroupRole, GuardianMessage

#: 平台不支持时统一返回的提示前缀。
UNSUPPORTED_HINT = "当前平台机器人接口不支持该操作。"


@dataclass
class MemberInfo:
    """统一成员信息。"""

    user_id: str
    name: str | None = None
    role: GroupRole = GroupRole.UNKNOWN
    card: str | None = None


class GuardianPlatformAdapter(ABC):
    """平台适配器基类。

    子类只需覆盖自己真正支持的平台接口；未覆盖的方法自动降级为 UNSUPPORTED。
    """

    platform_name: str = "unknown"

    def __init__(self, context: Any = None, profile: PlatformProfile | None = None) -> None:
        self.context = context
        self.profile = profile or self.default_profile()

    # ------------------------------------------------------------------
    # 能力与限制
    # ------------------------------------------------------------------
    @classmethod
    def default_profile(cls) -> PlatformProfile:
        """平台静态能力画像。子类必须覆盖。"""
        return PlatformProfile(name=cls.platform_name)

    @property
    def capabilities(self) -> PlatformCapability:
        return self.profile.capabilities

    @property
    def limits(self) -> PlatformLimits:
        return self.profile.limits

    def supports(self, capability: PlatformCapability) -> bool:
        return bool(self.capabilities & capability)

    def capability_matrix(self) -> list[dict[str, object]]:
        return self.profile.capability_matrix()

    # ------------------------------------------------------------------
    # 消息归一化
    # ------------------------------------------------------------------
    async def normalize(self, event: Any) -> GuardianMessage | None:
        """把 AstrBot 事件转换成 GuardianMessage。"""
        from ..core.event_router import GuardianEventRouter

        return await GuardianEventRouter(self).normalize(event)

    # ------------------------------------------------------------------
    # 角色解析
    # ------------------------------------------------------------------
    async def get_bot_role(self, message: GuardianMessage) -> GroupRole:
        """机器人在该群的角色。取不到时必须是 UNKNOWN（不得假定为管理员）。"""
        return GroupRole.UNKNOWN

    async def get_member_role(self, message: GuardianMessage, user_id: str) -> GroupRole:
        return GroupRole.UNKNOWN

    async def get_member_info(self, message: GuardianMessage, user_id: str) -> MemberInfo | None:
        return None

    async def get_member_list(
        self, group_id: str, message: GuardianMessage | None = None
    ) -> list[MemberInfo]:
        return []

    # ------------------------------------------------------------------
    # 消息发送
    # ------------------------------------------------------------------
    async def send_message(self, session: str, text: str) -> ActionResult:
        """跨平台发送消息：统一走 AstrBot context.send_message（所有内置适配器都实现 send_by_session）。"""
        if self.context is None or not session:
            return make_unsupported("send_message", "当前平台不支持主动发送消息。")
        try:
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import Plain

            delivered = await self.context.send_message(session, MessageChain([Plain(text)]))
        except Exception as exc:
            return make_failed("send_message", exc)
        if delivered:
            return make_success("send_message")
        return make_failed("send_message", "平台未找到该会话，消息未发送。")

    # ------------------------------------------------------------------
    # 撤回
    # ------------------------------------------------------------------
    def check_recall(
        self,
        message: GuardianMessage,
        bot_role: GroupRole,
        target_role: GroupRole,
        is_bot_message: bool,
    ) -> RecallCheck:
        """按平台撤回策略判定是否值得发起撤回请求。

        QQ 普通机器人对自己超过 120 秒的消息会在这里被拦下，避免无意义请求；
        管理员身份撤回普通成员消息不做 120 秒预检查。
        """
        return decide_recall(
            self.profile.recall_policy,
            bot_role=bot_role,
            target_role=target_role,
            is_bot_message=is_bot_message,
            age_seconds=message.age_seconds(),
        )

    async def recall_message(
        self,
        message: GuardianMessage,
        is_bot_message: bool = False,
    ) -> ActionResult:
        return make_unsupported("recall", "当前平台机器人接口不支持撤回消息。")

    # ------------------------------------------------------------------
    # 成员管理
    # ------------------------------------------------------------------
    async def mute_member(
        self, message: GuardianMessage, user_id: str, duration: int, reason: str = ""
    ) -> ActionResult:
        return make_unsupported("mute")

    async def unmute_member(self, message: GuardianMessage, user_id: str) -> ActionResult:
        return make_unsupported("unmute")

    async def kick_member(
        self, message: GuardianMessage, user_id: str, reason: str = ""
    ) -> ActionResult:
        return make_unsupported("kick")

    async def batch_kick_members(
        self, message: GuardianMessage, user_ids: list[str], reason: str = ""
    ) -> ActionResult:
        return make_unsupported("batch_kick")

    async def add_blacklist(self, message: GuardianMessage, user_id: str) -> ActionResult:
        return make_unsupported("blacklist_add")

    async def remove_blacklist(self, message: GuardianMessage, user_id: str) -> ActionResult:
        return make_unsupported("blacklist_remove")

    # ------------------------------------------------------------------
    # 入群审核
    # ------------------------------------------------------------------
    async def approve_join_request(self, message: GuardianMessage, request_flag: str) -> ActionResult:
        return make_unsupported("approve_join_request")

    async def reject_join_request(
        self, message: GuardianMessage, request_flag: str, reason: str = ""
    ) -> ActionResult:
        return make_unsupported("reject_join_request")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def close(self) -> None:
        """释放适配器持有的资源。"""
        return None

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @staticmethod
    def failed(action: str, error: BaseException | str, retcode: int | None = None) -> ActionResult:
        """把平台异常转成 FAILED 结果。"""
        return make_failed(action, error, retcode)

    @staticmethod
    def unsupported(action: str, message: str | None = None) -> ActionResult:
        return make_unsupported(action, message)

    def recall_denied_result(self, check: RecallCheck) -> ActionResult:
        """把撤回判定失败映射成用户可读结果（策略跳过，不是失败）。"""
        if check.reason is RecallDenyReason.AGE_LIMIT:
            return make_skipped("recall", f"不撤回：{check.detail}。")
        if check.decision is RecallDecision.DENY_BY_ROLE:
            return make_skipped("recall", f"不撤回：{check.detail}。")
        return make_skipped("recall", check.detail or "不撤回：策略未允许。")


__all__ = ["GuardianPlatformAdapter", "MemberInfo", "UNSUPPORTED_HINT", "GuardianPlatformError"]
