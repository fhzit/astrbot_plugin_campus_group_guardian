# -*- coding: utf-8 -*-
"""QQ 官方机器人（qq_official / qq_official_webhook）适配器。

接口路径与参数均来自 QQ 机器人开放平台 API v2 文档（实现时已重新核对）：
- 撤回群消息：DELETE /v2/groups/{group_openid}/messages/{message_id}
- 设置成员禁言：POST /v2/groups/{group_openid}/restrict_chat_setting
  body: {"members":[{"op":"add|update|del","member_openid":..,"mute_expire_at":RFC3339}]}
- 群成员批量移除：POST /v2/groups/{group_openid}/batch_remove_members
  body: {"member_openids":[..],"add_to_member_blacklist":bool}
- 群成员列表/信息：GET /v2/groups/{group_openid}/members[/{member_openid}]
- 群黑名单：GET/POST /v2/groups/{group_openid}/member_blacklist
  body(op=add|del): {"op":"add","member_openids":[..]}

并发/权限约束（已确认行为，不得简化）：
- 机器人是普通成员：只能撤回自己发送的、2 分钟内的消息；
- 机器人是群管理员：可撤回自己的消息与普通群成员的消息，不设时间上限；
  但不能撤回其他管理员和群主的消息。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from astrbot.api import logger

from ..core.actions.models import ActionResult, make_failed, make_success, make_unsupported
from ..core.capabilities import (
    CAPABILITY_LABELS,
    PlatformCapability,
    PlatformLimits,
    PlatformProfile,
    qq_official_recall_policy,
)
from ..core.errors import classify_platform_error
from ..core.models import GroupRole, GuardianMessage
from .base import GuardianPlatformAdapter, MemberInfo

_LOG = "[Guardian][Platform][QQOfficial]"

CALL_TIMEOUT = 20.0
ROLE_CACHE_TTL = 60.0
#: 单次禁言上限：30 天（平台限制）。
MUTE_MAX_SECONDS = 30 * 24 * 3600
#: 批量移除单次上限：20 个。
BATCH_KICK_MAX = 20

CAPABILITIES = (
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
    | PlatformCapability.KICK_MEMBERS_BATCH
    | PlatformCapability.GET_MEMBER_ROLE
    | PlatformCapability.GET_MEMBER_INFO
    | PlatformCapability.GET_MEMBER_LIST
    | PlatformCapability.QUERY_BLACKLIST
    | PlatformCapability.MANAGE_BLACKLIST
)


class QQOfficialPlatformAdapter(GuardianPlatformAdapter):
    """QQ 官方机器人适配器（网关与 Webhook 共用）。"""

    platform_name = "qq_official"

    def __init__(self, context: Any = None, profile: PlatformProfile | None = None) -> None:
        super().__init__(context, profile)
        self._role_cache: dict[tuple[str, str, str], tuple[float, GroupRole]] = {}
        self._bot_role_cache: dict[tuple[str, str], tuple[float, GroupRole]] = {}
        self.message_event_counter: dict[str, int] = {}
        self._client_cache: dict[str, Any] = {}

    @classmethod
    def default_profile(cls) -> PlatformProfile:
        return PlatformProfile(
            name=cls.platform_name,
            capabilities=CAPABILITIES,
            limits=PlatformLimits(
                normal_bot_own_recall_max_age_seconds=120,
                admin_bot_own_recall_max_age_seconds=None,
                admin_bot_member_recall_max_age_seconds=None,
                mute_max_seconds=MUTE_MAX_SECONDS,
                batch_kick_max_size=BATCH_KICK_MAX,
            ),
            recall_policy=qq_official_recall_policy(),
            notes=[
                "机器人是普通成员时，只能撤回自己 2 分钟内的消息。",
                "机器人是群管理员时，可撤回自己与普通成员的消息，不设时间上限；"
                "不能撤回其他管理员或群主的消息。",
                "单次禁言最长 30 天；超过该时长的逻辑处罚需要分段续期。",
                "群成员列表、批量移除、黑名单接口仅白名单机器人可用，"
                "无权限时返回 UNSUPPORTED 而不是失败整单。",
                "自动审核需要管理员在开放平台开启“机器人可获取全部群消息”，"
                "否则只能收到 @ 消息。",
            ],
        )

    # ------------------------------------------------------------------
    # 平台调用
    # ------------------------------------------------------------------
    def _api(self, message: GuardianMessage) -> Any | None:
        """取 botpy 的 BotAPI 对象。

        优先从事件里取；定时任务（如到点解禁）没有事件对象，
        因此这里同时缓存最近一次观测到的 client，保证无事件时仍可调用。
        """
        raw_event = message.raw_event
        client = getattr(raw_event, "bot", None) if raw_event is not None else None
        if client is None:
            client = getattr(raw_event, "client", None) if raw_event is not None else None
        if client is None:
            client = self._client_cache.get("client")
        else:
            self._client_cache["client"] = client
        return getattr(client, "api", None) if client is not None else None

    async def _request(
        self,
        message: GuardianMessage,
        method: str,
        path: str,
        *,
        path_params: dict[str, Any],
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        action: str = "request",
    ) -> ActionResult:
        """调用 QQ OpenAPI。任何异常都被归一化，不向上抛。"""
        api = self._api(message)
        if api is None:
            return make_unsupported(action, "当前事件没有可用的 QQ 官方机器人客户端。")
        http = getattr(api, "_http", None)
        if http is None or not hasattr(http, "request"):
            return make_unsupported(action, "当前 botpy 版本不提供原始 HTTP 请求能力，已降级。")
        try:
            from botpy.http import Route
        except Exception:  # pragma: no cover - 依赖缺失时降级
            return make_unsupported(action, "未安装 qq-botpy，无法调用 QQ 官方接口。")

        try:
            route = Route(method, path, **path_params)
            kwargs: dict[str, Any] = {}
            if json_body is not None:
                kwargs["json"] = json_body
            if params:
                kwargs["params"] = params
            result = await asyncio.wait_for(http.request(route, **kwargs), timeout=CALL_TIMEOUT)
        except asyncio.TimeoutError:
            return make_failed(action, "QQ 官方接口调用超时")
        except Exception as exc:
            error_type = classify_platform_error(exc)
            if error_type.value in {"not_supported"}:
                return make_unsupported(action, f"QQ 官方接口不支持该操作（{exc}）。")
            return self.failed(action, exc)
        return self._interpret(action, result)

    @staticmethod
    def _interpret(action: str, result: Any) -> ActionResult:
        """把返回体映射为 ActionResult。DELETE 类接口成功时返回 None。"""
        if result is None:
            return make_success(action)
        if isinstance(result, dict):
            fail_ids = result.get("fail_openids")
            if isinstance(fail_ids, list) and fail_ids:
                return make_failed(action, f"部分 openid 操作失败：{fail_ids}")
            if result.get("remove_members_result") not in (None, "success"):
                return make_failed(action, f"移除成员结果异常：{result.get('remove_members_result')}")
        return make_success(action)

    # ------------------------------------------------------------------
    # 角色
    # ------------------------------------------------------------------
    async def _member_info_raw(self, message: GuardianMessage, member_openid: str) -> dict[str, Any] | None:
        api = self._api(message)
        if api is None:
            return None
        http = getattr(api, "_http", None)
        if http is None or not hasattr(http, "request"):
            return None
        try:
            from botpy.http import Route

            route = Route(
                "GET",
                "/v2/groups/{group_openid}/members/{member_openid}",
                group_openid=message.group_id,
                member_openid=member_openid,
            )
            result = await asyncio.wait_for(http.request(route), timeout=CALL_TIMEOUT)
        except Exception as exc:
            logger.debug(f"{_LOG} 获取成员信息失败（可能未获白名单权限）: {exc}")
            return None
        return result if isinstance(result, dict) else None

    async def get_member_role(self, message: GuardianMessage, user_id: str) -> GroupRole:
        key = (message.adapter_id, message.group_id or "", str(user_id))
        cached = self._role_cache.get(key)
        now = time.monotonic()
        if cached and (now - cached[0]) < ROLE_CACHE_TTL:
            return cached[1]
        payload = await self._member_info_raw(message, str(user_id))
        role = GroupRole.parse(payload.get("member_role")) if payload else GroupRole.UNKNOWN
        if role is GroupRole.UNKNOWN and payload is None:
            role = await self._role_from_member_list(message, str(user_id))
        self._role_cache[key] = (now, role)
        return role

    async def _role_from_member_list(self, message: GuardianMessage, member_openid: str) -> GroupRole:
        """成员信息接口不可用时退回成员列表（最多翻 3 页）。"""
        api = self._api(message)
        if api is None:
            return GroupRole.UNKNOWN
        http = getattr(api, "_http", None)
        if http is None or not hasattr(http, "request"):
            return GroupRole.UNKNOWN
        try:
            from botpy.http import Route

            cursor: str | None = None
            for _ in range(3):
                params = {"cursor": cursor} if cursor else None
                route = Route("GET", "/v2/groups/{group_openid}/members", group_openid=message.group_id)
                result = await asyncio.wait_for(
                    http.request(route, params=params) if params else http.request(route),
                    timeout=CALL_TIMEOUT,
                )
                if not isinstance(result, dict):
                    return GroupRole.UNKNOWN
                for item in result.get("members") or []:
                    if isinstance(item, dict) and str(item.get("member_openid")) == member_openid:
                        return GroupRole.parse(item.get("member_role"))
                cursor = result.get("next_cursor") or ""
                if not cursor:
                    break
        except Exception as exc:
            logger.debug(f"{_LOG} 读取成员列表失败: {exc}")
        return GroupRole.UNKNOWN

    async def get_bot_role(self, message: GuardianMessage) -> GroupRole:
        """机器人自身群角色。

        优先用机器人 openid 直接查；拿不到时在成员列表里找 ``bot=true`` 的成员。
        """
        key = (message.adapter_id, message.group_id or "")
        cached = self._bot_role_cache.get(key)
        now = time.monotonic()
        if cached and (now - cached[0]) < ROLE_CACHE_TTL:
            return cached[1]
        role = GroupRole.UNKNOWN
        if message.bot_id:
            payload = await self._member_info_raw(message, message.bot_id)
            if payload:
                role = GroupRole.parse(payload.get("member_role"))
        if role is GroupRole.UNKNOWN:
            role = await self._bot_role_from_member_list(message)
        self._bot_role_cache[key] = (now, role)
        return role

    async def _bot_role_from_member_list(self, message: GuardianMessage) -> GroupRole:
        api = self._api(message)
        http = getattr(api, "_http", None) if api is not None else None
        if http is None or not hasattr(http, "request"):
            return GroupRole.UNKNOWN
        try:
            from botpy.http import Route

            route = Route("GET", "/v2/groups/{group_openid}/members", group_openid=message.group_id)
            result = await asyncio.wait_for(http.request(route), timeout=CALL_TIMEOUT)
            if isinstance(result, dict):
                for item in result.get("members") or []:
                    if isinstance(item, dict) and item.get("bot"):
                        return GroupRole.parse(item.get("member_role"))
        except Exception as exc:
            logger.debug(f"{_LOG} 读取机器人角色失败: {exc}")
        return GroupRole.UNKNOWN

    async def get_member_info(self, message: GuardianMessage, user_id: str) -> MemberInfo | None:
        payload = await self._member_info_raw(message, str(user_id))
        if not payload:
            return None
        return MemberInfo(
            user_id=str(payload.get("member_openid", user_id)),
            name=payload.get("username"),
            role=GroupRole.parse(payload.get("member_role")),
            card=None,
        )

    async def get_member_list(
        self, group_id: str, message: GuardianMessage | None = None
    ) -> list[MemberInfo]:
        if message is None:
            return []
        api = self._api(message)
        http = getattr(api, "_http", None) if api is not None else None
        if http is None or not hasattr(http, "request"):
            return []
        members: list[MemberInfo] = []
        try:
            from botpy.http import Route

            cursor: str | None = None
            for _ in range(3):
                route = Route("GET", "/v2/groups/{group_openid}/members", group_openid=group_id)
                result = await asyncio.wait_for(
                    http.request(route, params={"cursor": cursor}) if cursor else http.request(route),
                    timeout=CALL_TIMEOUT,
                )
                if not isinstance(result, dict):
                    break
                for item in result.get("members") or []:
                    if isinstance(item, dict):
                        members.append(
                            MemberInfo(
                                user_id=str(item.get("member_openid", "")),
                                name=item.get("username"),
                                role=GroupRole.parse(item.get("member_role")),
                            )
                        )
                cursor = result.get("next_cursor") or ""
                if not cursor:
                    break
        except Exception as exc:
            logger.debug(f"{_LOG} 获取成员列表失败: {exc}")
        return members

    def invalidate_role_cache(self) -> None:
        self._role_cache.clear()
        self._bot_role_cache.clear()

    # ------------------------------------------------------------------
    # 撤回 / 成员管理
    # ------------------------------------------------------------------
    async def recall_message(
        self, message: GuardianMessage, is_bot_message: bool = False
    ) -> ActionResult:
        _ = is_bot_message
        return await self._request(
            message,
            "DELETE",
            "/v2/groups/{group_openid}/messages/{message_id}",
            path_params={
                "group_openid": message.group_id,
                "message_id": message.message_id,
            },
            action="recall",
        )

    async def mute_member(
        self, message: GuardianMessage, user_id: str, duration: int, reason: str = ""
    ) -> ActionResult:
        _ = reason
        seconds = max(0, int(duration or 0))
        if seconds <= 0:
            return await self.unmute_member(message, user_id)
        op = "add"
        if seconds > MUTE_MAX_SECONDS:
            # 平台单次上限 30 天：超长处罚需要分段续期，不能谎称一次性生效。
            seconds = MUTE_MAX_SECONDS
            logger.info(f"{_LOG} 禁言时长超过平台上限，已按 30 天提交（需分段续期）")
        expire_at = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
        return await self._request(
            message,
            "POST",
            "/v2/groups/{group_openid}/restrict_chat_setting",
            path_params={"group_openid": message.group_id},
            json_body={
                "members": [
                    {
                        "op": op,
                        "member_openid": str(user_id),
                        "mute_expire_at": expire_at,
                    }
                ]
            },
            action="mute",
        )

    async def unmute_member(self, message: GuardianMessage, user_id: str) -> ActionResult:
        return await self._request(
            message,
            "POST",
            "/v2/groups/{group_openid}/restrict_chat_setting",
            path_params={"group_openid": message.group_id},
            json_body={
                "members": [
                    {
                        "op": "del",
                        "member_openid": str(user_id),
                        "mute_expire_at": "",
                    }
                ]
            },
            action="unmute",
        )

    async def kick_member(self, message: GuardianMessage, user_id: str, reason: str = "") -> ActionResult:
        return await self.batch_kick_members(message, [str(user_id)], reason)

    async def batch_kick_members(
        self, message: GuardianMessage, user_ids: list[str], reason: str = ""
    ) -> ActionResult:
        _ = reason
        targets = [str(item) for item in user_ids if item][:BATCH_KICK_MAX]
        if not targets:
            return make_unsupported(action="batch_kick", message="没有需要移除的成员。")
        return await self._request(
            message,
            "POST",
            "/v2/groups/{group_openid}/batch_remove_members",
            path_params={"group_openid": message.group_id},
            json_body={"member_openids": targets, "add_to_member_blacklist": False},
            action="kick",
        )

    async def add_blacklist(self, message: GuardianMessage, user_id: str) -> ActionResult:
        """加入群黑名单。

        目标仍在群中时 member_blacklist 接口会失败，此时退回
        batch_remove_members 的 add_to_member_blacklist 分支。
        """
        result = await self._request(
            message,
            "POST",
            "/v2/groups/{group_openid}/member_blacklist",
            path_params={"group_openid": message.group_id},
            json_body={"op": "add", "member_openids": [str(user_id)]},
            action="blacklist_add",
        )
        if result.ok:
            return result
        fallback = await self._request(
            message,
            "POST",
            "/v2/groups/{group_openid}/batch_remove_members",
            path_params={"group_openid": message.group_id},
            json_body={"member_openids": [str(user_id)], "add_to_member_blacklist": True},
            action="blacklist_add",
        )
        if fallback.ok:
            return fallback
        return result

    async def remove_blacklist(self, message: GuardianMessage, user_id: str) -> ActionResult:
        return await self._request(
            message,
            "POST",
            "/v2/groups/{group_openid}/member_blacklist",
            path_params={"group_openid": message.group_id},
            json_body={"op": "del", "member_openids": [str(user_id)]},
            action="blacklist_remove",
        )

    async def query_blacklist(self, message: GuardianMessage) -> list[str]:
        """查询群黑名单（capability: QUERY_BLACKLIST）。"""
        api = self._api(message)
        http = getattr(api, "_http", None) if api is not None else None
        if http is None or not hasattr(http, "request"):
            return []
        try:
            from botpy.http import Route

            route = Route(
                "GET", "/v2/groups/{group_openid}/member_blacklist", group_openid=message.group_id
            )
            result = await asyncio.wait_for(http.request(route), timeout=CALL_TIMEOUT)
        except Exception as exc:
            logger.debug(f"{_LOG} 查询群黑名单失败: {exc}")
            return []
        if isinstance(result, dict):
            # 文档：GET /v2/groups/{group_openid}/member_blacklist 返回 {users:[{member_openid,...}], next_cursor}
            members = result.get("users") or result.get("members") or []
            return [
                str(item.get("member_openid"))
                for item in members
                if isinstance(item, dict) and item.get("member_openid")
            ]
        return []

    # ------------------------------------------------------------------
    # 全量消息诊断（§24、§159）
    # ------------------------------------------------------------------
    def note_message_event(self, event: Any) -> None:
        """记录观测到的消息事件类型，用于判断是否收到全量群消息。

        注意：QQ 官方在“全量消息”与“仅 @ 消息”两种模式下都会给出 GroupMessage，
        因此这里用是否包含 @ 提及作为启发式判断，结论仅用于提示管理员。
        """
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None:
            return
        name = type(raw).__name__
        text = str(getattr(event, "message_str", "") or "")
        if name == "GroupMessage":
            if "<@!" in text:
                self.message_event_counter["group_at_message_create"] = (
                    self.message_event_counter.get("group_at_message_create", 0) + 1
                )
            else:
                self.message_event_counter["group_message_create"] = (
                    self.message_event_counter.get("group_message_create", 0) + 1
                )

    def full_message_diagnosis(self) -> dict[str, Any]:
        """返回全量消息诊断结果，供 WebUI 提示使用。"""
        full = self.message_event_counter.get("group_message_create", 0)
        at_only = self.message_event_counter.get("group_at_message_create", 0)
        if full > 0:
            level, hint = "ok", "已观察到疑似全量群消息。"
        elif at_only > 0:
            level, hint = (
                "warn",
                "目前只观察到 @ 机器人 的消息，自动审核可能只能收到部分消息；"
                "请在 QQ 开放平台为机器人开启全部群消息权限。",
            )
        else:
            level, hint = "unknown", "尚未收到群消息，无法判断全量消息是否可用。"
        return {
            "level": level,
            "hint": hint,
            "group_message_create": full,
            "group_at_message_create": at_only,
        }
