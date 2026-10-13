# -*- coding: utf-8 -*-
"""OneBot v11（aiocqhttp）适配器。

- 统一通过 ``event.bot`` 暴露的 ``call_action`` 调用协议端 API；
- 高级能力（群公告/群文件/精华/群名片等）依赖协议端实现，
  API 返回「不支持」时统一映射为 UNSUPPORTED，不冒充支持；
- 角色解析带短 TTL 缓存，但每次危险动作仍以平台返回为准。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger

from ..core.actions.models import ActionResult, make_failed, make_success, make_unsupported
from ..core.capabilities import (
    PlatformCapability,
    PlatformLimits,
    PlatformProfile,
    onebot_recall_policy,
)
from ..core.errors import classify_platform_error
from ..core.models import GroupRole, GuardianMessage
from .base import GuardianPlatformAdapter, MemberInfo

_LOG = "[Guardian][Platform][OneBot]"

#: 单次 API 调用超时，避免协议端无响应导致协程挂起。
CALL_TIMEOUT = 20.0
#: 角色缓存 TTL（秒）。缓存不是事实来源，只用于反诈快速放行判定。
ROLE_CACHE_TTL = 60.0

#: 基础能力：OneBot v11 标准 API 覆盖。
BASE_CAPABILITIES = (
    PlatformCapability.RECEIVE_GROUP_MESSAGE
    | PlatformCapability.RECEIVE_IMAGE
    | PlatformCapability.SEND_MESSAGE
    | PlatformCapability.SEND_IMAGE
    | PlatformCapability.PROACTIVE_MESSAGE
    | PlatformCapability.RECALL_OWN_MESSAGE
    | PlatformCapability.RECALL_MEMBER_MESSAGE
    | PlatformCapability.MUTE_MEMBER
    | PlatformCapability.UNMUTE_MEMBER
    | PlatformCapability.KICK_MEMBER
    | PlatformCapability.GET_MEMBER_ROLE
    | PlatformCapability.GET_MEMBER_INFO
    | PlatformCapability.GET_MEMBER_LIST
    | PlatformCapability.WHOLE_GROUP_MUTE
    | PlatformCapability.REVIEW_JOIN_REQUEST
)

#: 扩展能力：NapCat / LLOneBot 等协议端提供，非 OneBot v11 标准。
EXTENDED_CAPABILITIES = (
    PlatformCapability.SET_MEMBER_CARD
    | PlatformCapability.SET_GROUP_ADMIN
    | PlatformCapability.SET_GROUP_NAME
    | PlatformCapability.GROUP_NOTICE
    | PlatformCapability.GROUP_FILE
    | PlatformCapability.ESSENCE_MESSAGE
    | PlatformCapability.KICK_MEMBERS_BATCH
)


class OneBotPlatformAdapter(GuardianPlatformAdapter):
    """OneBot v11 适配器。"""

    platform_name = "aiocqhttp"

    def __init__(self, context: Any = None, profile: PlatformProfile | None = None) -> None:
        super().__init__(context, profile)
        self._role_cache: dict[tuple[str, str, str], tuple[float, GroupRole]] = {}
        self._bot_role_cache: dict[tuple[str, str], tuple[float, GroupRole]] = {}
        self.extended_available = True

    @classmethod
    def default_profile(cls) -> PlatformProfile:
        """OneBot 能力画像。

        扩展能力默认按“协议端可能支持”声明，调用失败时会以 UNSUPPORTED/FAILED
        返回给业务层，不会因为 NapCat 支持就认为所有 OneBot 实现都支持。
        """
        return PlatformProfile(
            name=cls.platform_name,
            capabilities=BASE_CAPABILITIES | EXTENDED_CAPABILITIES,
            limits=PlatformLimits(
                normal_bot_own_recall_max_age_seconds=120,
                admin_bot_own_recall_max_age_seconds=120,
                admin_bot_member_recall_max_age_seconds=120,
                mute_max_seconds=None,
                batch_kick_max_size=50,
            ),
            recall_policy=onebot_recall_policy(),
            notes=[
                "撤回窗口约 2 分钟，与机器人是否为管理员无关。",
                "禁言最小有效粒度约 60 秒，用户输入时长单位为分钟。",
                "群公告/群文件/精华/群名片/设置管理员属于协议端扩展，"
                "不同实现可能不支持，调用失败会降级为 UNSUPPORTED。",
                "OneBot v11 没有群黑名单接口，黑名单动作在该平台固定 UNSUPPORTED。",
            ],
        )

    # ------------------------------------------------------------------
    # 客户端与调用
    # ------------------------------------------------------------------
    def _resolve_client(self, event: Any) -> Any | None:
        """取可 call_action 的对象（兼容 event.bot 与 event.bot.api）。

        定时任务没有事件对象时，退回从 AstrBot 平台实例里取（§187 到点解禁）。
        """
        candidate = getattr(event, "bot", None) if event is not None else None
        if candidate is not None:
            if hasattr(candidate, "call_action"):
                return candidate
            api = getattr(candidate, "api", None)
            if api is not None and hasattr(api, "call_action"):
                return api
        return self._client_from_platform_manager()

    def _client_from_platform_manager(self) -> Any | None:
        """无事件时的客户端兜底：从 context.platform_manager 找 aiocqhttp 实例。"""
        manager = getattr(self.context, "platform_manager", None)
        if manager is None:
            return None
        try:
            instances = list(manager.get_insts())
        except Exception:
            return None
        for instance in instances:
            meta = getattr(instance, "meta", None)
            name = ""
            try:
                name = str(meta().name) if callable(meta) else str(getattr(meta, "name", ""))
            except Exception:
                name = ""
            if name and name != "aiocqhttp":
                continue
            client = None
            if hasattr(instance, "get_client"):
                try:
                    client = instance.get_client()
                except Exception:
                    client = None
            if client is None:
                client = getattr(instance, "bot", None) or getattr(instance, "client", None)
            if client is not None and hasattr(client, "call_action"):
                return client
        return None

    async def _call(self, message: GuardianMessage, action: str, **params: Any) -> Any:
        """调用协议端 API。"""
        client = self._resolve_client(message.raw_event)
        if client is None:
            raise RuntimeError("当前事件没有可用的 OneBot 客户端")
        return await asyncio.wait_for(client.call_action(action, **params), timeout=CALL_TIMEOUT)

    async def _call_result(self, message: GuardianMessage, action: str, **params: Any) -> ActionResult:
        """调用并把返回值/异常统一映射为 ActionResult。"""
        try:
            result = await self._call(message, action, **params)
        except asyncio.TimeoutError:
            return make_failed(action, "OneBot API 调用超时")
        except Exception as exc:
            error_type = classify_platform_error(exc)
            if error_type.value == "not_supported":
                self.extended_available = False
                return make_unsupported(action, f"协议端不支持 {action}，已降级。")
            return self.failed(action, exc)
        if isinstance(result, dict):
            status = str(result.get("status", "")).lower()
            retcode = result.get("retcode")
            if status == "failed" or (isinstance(retcode, int) and retcode not in (0, 1)):
                error_type = classify_platform_error(str(result.get("message") or result), retcode)
                if retcode == 100 or error_type.value == "not_supported":
                    self.extended_available = False
                    return make_unsupported(action, f"协议端不支持 {action}，已降级。")
                return self.failed(action, str(result.get("message") or result), retcode)
        return make_success(action)

    # ------------------------------------------------------------------
    # 角色
    # ------------------------------------------------------------------
    async def _member_role_uncached(self, message: GuardianMessage, user_id: str) -> GroupRole:
        try:
            info = await self._call(
                message,
                "get_group_member_info",
                group_id=message.group_id,
                user_id=user_id,
                no_cache=True,
            )
        except Exception as exc:
            logger.debug(f"{_LOG} 获取成员角色失败: {exc}")
            return GroupRole.UNKNOWN
        if isinstance(info, dict):
            payload = info.get("data") if isinstance(info.get("data"), dict) else info
            return GroupRole.parse(payload.get("role"))
        return GroupRole.UNKNOWN

    async def get_member_role(self, message: GuardianMessage, user_id: str) -> GroupRole:
        key = (message.adapter_id, message.group_id or "", str(user_id))
        cached = self._role_cache.get(key)
        now = time.monotonic()
        if cached and (now - cached[0]) < ROLE_CACHE_TTL:
            return cached[1]
        role = await self._member_role_uncached(message, user_id)
        self._role_cache[key] = (now, role)
        return role

    async def get_bot_role(self, message: GuardianMessage) -> GroupRole:
        bot_id = message.bot_id or ""
        if not bot_id:
            return GroupRole.UNKNOWN
        key = (message.adapter_id, message.group_id or "")
        cached = self._bot_role_cache.get(key)
        now = time.monotonic()
        if cached and (now - cached[0]) < ROLE_CACHE_TTL:
            return cached[1]
        role = await self._member_role_uncached(message, bot_id)
        self._bot_role_cache[key] = (now, role)
        return role

    def invalidate_role_cache(self) -> None:
        """清空角色缓存（权限变更后调用）。"""
        self._role_cache.clear()
        self._bot_role_cache.clear()

    async def get_member_info(self, message: GuardianMessage, user_id: str) -> MemberInfo | None:
        try:
            info = await self._call(
                message, "get_group_member_info", group_id=message.group_id, user_id=user_id
            )
        except Exception as exc:
            logger.debug(f"{_LOG} 获取成员信息失败: {exc}")
            return None
        payload = info.get("data") if isinstance(info, dict) and isinstance(info.get("data"), dict) else info
        if not isinstance(payload, dict):
            return None
        return MemberInfo(
            user_id=str(payload.get("user_id", user_id)),
            name=payload.get("nickname"),
            role=GroupRole.parse(payload.get("role")),
            card=payload.get("card"),
        )

    async def get_member_list(
        self, group_id: str, message: GuardianMessage | None = None
    ) -> list[MemberInfo]:
        if message is None:
            return []
        try:
            result = await self._call(message, "get_group_member_list", group_id=group_id)
        except Exception as exc:
            logger.debug(f"{_LOG} 获取成员列表失败: {exc}")
            return []
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list):
            return []
        return [
            MemberInfo(
                user_id=str(item.get("user_id", "")),
                name=item.get("nickname"),
                role=GroupRole.parse(item.get("role")),
                card=item.get("card"),
            )
            for item in data
            if isinstance(item, dict)
        ]

    # ------------------------------------------------------------------
    # 撤回 / 成员管理
    # ------------------------------------------------------------------
    async def recall_message(
        self, message: GuardianMessage, is_bot_message: bool = False
    ) -> ActionResult:
        _ = is_bot_message
        return await self._call_result(message, "delete_msg", message_id=message.message_id)

    async def mute_member(
        self, message: GuardianMessage, user_id: str, duration: int, reason: str = ""
    ) -> ActionResult:
        _ = reason
        seconds = int(duration or 0)
        if seconds > 0 and seconds < 60:
            # OneBot 最小有效粒度约 60 秒，向上取整避免“禁言了但没生效”。
            seconds = 60
        return await self._call_result(
            message,
            "set_group_ban",
            group_id=message.group_id,
            user_id=user_id,
            duration=seconds,
        )

    async def unmute_member(self, message: GuardianMessage, user_id: str) -> ActionResult:
        return await self._call_result(
            message,
            "set_group_ban",
            group_id=message.group_id,
            user_id=user_id,
            duration=0,
        )

    async def kick_member(self, message: GuardianMessage, user_id: str, reason: str = "") -> ActionResult:
        return await self._call_result(
            message,
            "set_group_kick",
            group_id=message.group_id,
            user_id=user_id,
            reject_add_request=False,
        )

    async def batch_kick_members(
        self, message: GuardianMessage, user_ids: list[str], reason: str = ""
    ) -> ActionResult:
        _ = reason
        if not user_ids:
            return self.unsupported("batch_kick", "没有需要移除的成员。")
        failed: list[str] = []
        for user_id in user_ids:
            result = await self.kick_member(message, user_id)
            if not result.ok:
                failed.append(user_id)
        if failed:
            return make_failed("batch_kick", f"部分成员移除失败：{failed}")
        return make_success("batch_kick", detail={"count": len(user_ids)})

    async def close(self) -> None:
        self._role_cache.clear()
        self._bot_role_cache.clear()
