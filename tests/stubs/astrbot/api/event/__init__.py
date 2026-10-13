"""桩：``astrbot.api.event``。

提供 filter 命名空间（记录注册项，便于测试断言），以及 MessageChain / AstrMessageEvent。
"""

from __future__ import annotations

import enum
from typing import Any, Callable

#: 记录插件注册的处理器，供集成测试断言。
REGISTRY: dict[str, list[dict[str, Any]]] = {}


class PlatformAdapterType(enum.Flag):
    AIOCQHTTP = enum.auto()
    QQOFFICIAL = enum.auto()
    QQOFFICIAL_WEBHOOK = enum.auto()
    WECOM = enum.auto()
    WECOM_AI_BOT = enum.auto()
    DINGTALK = enum.auto()
    TELEGRAM = enum.auto()
    ALL = (
        AIOCQHTTP
        | QQOFFICIAL
        | QQOFFICIAL_WEBHOOK
        | WECOM
        | WECOM_AI_BOT
        | DINGTALK
        | TELEGRAM
    )


class EventMessageType(enum.Flag):
    GROUP_MESSAGE = enum.auto()
    PRIVATE_MESSAGE = enum.auto()
    OTHER_MESSAGE = enum.auto()
    ALL = GROUP_MESSAGE | PRIVATE_MESSAGE | OTHER_MESSAGE


class _FilterNamespace:
    """filter 命名空间：属性访问即返回装饰器工厂。"""

    PlatformAdapterType = PlatformAdapterType
    EventMessageType = EventMessageType

    def __getattr__(self, name: str) -> Callable[..., Callable[[Any], Any]]:
        def _factory(*args: Any, **kwargs: Any) -> Callable[[Any], Any]:
            def _decorator(func: Any) -> Any:
                REGISTRY.setdefault(name, []).append(
                    {
                        "args": args,
                        "kwargs": kwargs,
                        "handler": getattr(func, "__name__", str(func)),
                        "func": func,
                    }
                )
                return func

            return _decorator

        return _factory


filter = _FilterNamespace()


class MessageChain(list):
    """极简 MessageChain 桩。"""

    def __init__(self, components: list[Any] | None = None) -> None:
        super().__init__(components or [])

    def message(self, text: str) -> "MessageChain":
        from ..message_components import Plain

        self.append(Plain(text))
        return self


class AstrMessageEvent:
    """极简事件桩（集成测试用）。"""

    def __init__(
        self,
        *,
        message_str: str = "",
        group_id: str | None = None,
        sender_id: str = "1",
        sender_name: str = "测试成员",
        self_id: str = "9",
        platform_name: str = "aiocqhttp",
        platform_id: str = "inst-1",
        umo: str | None = None,
        role: str = "member",
        images: list[Any] | None = None,
        bot: Any = None,
        admin: bool = False,
    ) -> None:
        self.message_str = message_str
        self._group_id = group_id
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._self_id = self_id
        self._platform_name = platform_name
        self._platform_id = platform_id
        self._umo = umo or f"{platform_name}:GroupMessage:{group_id or 'private'}"
        self.role = role
        self.message_obj = _MessageObject(message_str, images or [], role)
        self.bot = bot
        self._admin = admin
        self.stopped = False
        self.extras: dict[str, Any] = {}

    # ---- AstrBot 事件接口 ----
    def get_message_str(self) -> str:
        return self.message_str

    def get_group_id(self) -> str | None:
        return self._group_id

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender_name

    def get_self_id(self) -> str:
        return self._self_id

    def get_platform_name(self) -> str:
        return self._platform_name

    def get_platform_id(self) -> str:
        return self._platform_id

    @property
    def unified_msg_origin(self) -> str:
        return self._umo

    def get_messages(self) -> list[Any]:
        return list(self.message_obj.message)

    def is_admin(self) -> bool:
        return self._admin

    def set_extra(self, key: str, value: Any) -> None:
        self.extras[key] = value

    def get_extra(self, key: str, default: Any = None) -> Any:
        return self.extras.get(key, default)

    def stop_event(self) -> None:
        self.stopped = True

    def plain_result(self, text: str) -> Any:
        return {"type": "plain", "text": text}


class _MessageObject:
    def __init__(self, text: str, images: list[Any], role: str) -> None:
        from ..message_components import Plain

        components: list[Any] = []
        if text:
            components.append(Plain(text))
        components.extend(images)
        self.message = components
        self.raw_message = None
        self.type = "group"
        self.message_str = text
        self.sender = _Sender(role)


class _Sender:
    def __init__(self, role: str) -> None:
        self.user_id = "1"
        self.nickname = "测试成员"
        self.role = role


__all__ = [
    "REGISTRY",
    "AstrMessageEvent",
    "EventMessageType",
    "MessageChain",
    "PlatformAdapterType",
    "filter",
]
