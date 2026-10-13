# -*- coding: utf-8 -*-
"""事件路由：AstrBot 事件 → GuardianMessage。

所有平台事件在这里归一化，下游检测模块只看到 GuardianMessage。
平台特有的消息段（合并转发、JSON 卡片等）由各平台适配器通过 extra 注入，
不进业务层分支。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from astrbot.api import logger

from .models import GroupRole, GuardianImage, GuardianMessage

_LOG = "[Guardian][Router]"

#: AstrBot 消息组件类型名 → 我们的处理方式。
_TEXT_TYPES = {"Plain"}
_IMAGE_TYPES = {"Image"}
_REPLY_TYPES = {"Reply"}
#: 只有平台扩展解析层关心的类型。
_PLATFORM_EXTRA_TYPES = {"Forward", "Node", "Nodes", "Json", "File", "At", "Poke", "Record", "Video"}


def _component_type(component: Any) -> str:
    """取组件类型名。永远返回字符串，未知类型返回空串。"""
    value = getattr(component, "type", None)
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def _first_http(*values: Any) -> str | None:
    """从若干候选字段中挑出可下载的 http(s) 地址。"""
    fallback = None
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        if value.startswith(("http://", "https://")):
            return value
        if fallback is None:
            fallback = value
    return fallback


def _parse_timestamp(raw: Any) -> datetime:
    """把平台时间戳解析成带时区的 datetime，失败则取当前时间。"""
    if isinstance(raw, (int, float)) and raw > 0:
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return datetime.now(timezone.utc)
    if isinstance(raw, str) and raw.isdigit():
        return _parse_timestamp(int(raw))
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


class GuardianEventRouter:
    """构造 GuardianMessage 的唯一入口。"""

    def __init__(
        self,
        adapter: Any,
        role_resolver: Callable[[Any, Any, GuardianMessage], Awaitable[GroupRole]] | None = None,
    ) -> None:
        self.adapter = adapter
        self.role_resolver = role_resolver

    async def normalize(self, event: Any) -> GuardianMessage | None:
        """把事件归一化。非群消息返回 None（本插件只治理群聊）。"""
        try:
            message_obj = getattr(event, "message_obj", None)
            group_id = str(event.get_group_id() or "")
            if not group_id:
                return None

            message = GuardianMessage(
                platform=str(event.get_platform_name() or getattr(self.adapter, "platform_name", "")),
                adapter_id=str(event.get_platform_id() or ""),
                unified_msg_origin=str(event.unified_msg_origin or ""),
                conversation_id=str(getattr(message_obj, "session_id", "") or event.get_group_id() or ""),
                group_id=group_id,
                message_id=str(getattr(message_obj, "message_id", "") or ""),
                sender_id=str(event.get_sender_id() or ""),
                sender_name=event.get_sender_name() or None,
                sender_role=GroupRole.UNKNOWN,
                text=str(event.message_str or ""),
                images=self._extract_images(event),
                reply_to_message_id=self._extract_reply_id(event),
                bot_id=str(event.get_self_id() or "") or None,
                timestamp=_parse_timestamp(getattr(message_obj, "timestamp", None)),
                raw_event=event,
            )
        except Exception as exc:  # 归一化失败不能让整条消息炸掉管线
            logger.debug(f"{_LOG} 归一化失败: {exc}")
            return None

        if self.role_resolver is not None:
            try:
                message.sender_role = await self.role_resolver(event, self.adapter, message)
            except Exception as exc:
                logger.debug(f"{_LOG} 角色解析失败，按 UNKNOWN 处理: {exc}")
                message.sender_role = GroupRole.UNKNOWN
        return message

    @staticmethod
    def _extract_images(event: Any) -> list[GuardianImage]:
        """从消息链提取图片。逐组件容错，单张坏图不影响整条消息。"""
        images: list[GuardianImage] = []
        try:
            components = event.get_messages() or []
        except Exception:
            return images
        for component in components:
            if _component_type(component) not in _IMAGE_TYPES:
                continue
            try:
                url = _first_http(
                    getattr(component, "url", None),
                    getattr(component, "file", None),
                )
                local_path = getattr(component, "path", None) or None
                if not url and isinstance(getattr(component, "file", None), str):
                    candidate = component.file
                    if candidate.startswith("file://"):
                        local_path = candidate[7:]
                images.append(
                    GuardianImage(
                        url=url if isinstance(url, str) and url.startswith("http") else None,
                        file_path=str(local_path) if local_path else None,
                        mime_type=getattr(component, "mime_type", None),
                        source="message",
                        raw=component,
                    )
                )
            except Exception as exc:
                logger.debug(f"{_LOG} 图片组件解析失败: {exc}")
                continue
        return images

    @staticmethod
    def _extract_reply_id(event: Any) -> str | None:
        for component in event.get_messages() or []:
            if _component_type(component) in _REPLY_TYPES:
                identifier = getattr(component, "id", None)
                if identifier is not None:
                    return str(identifier)
        return None

    @staticmethod
    def platform_extra_components(event: Any) -> list[Any]:
        """平台特有能力组件（合并转发/JSON 卡片等），仅平台扩展解析层使用。"""
        extras = []
        try:
            for component in event.get_messages() or []:
                if _component_type(component) in _PLATFORM_EXTRA_TYPES:
                    extras.append(component)
        except Exception:
            return []
        return extras
