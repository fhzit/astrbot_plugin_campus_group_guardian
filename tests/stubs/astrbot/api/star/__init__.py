"""桩：``astrbot.api.star``。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Callable


class Context:
    """桩上下文：只实现插件用到的少量方法。"""

    def __init__(self) -> None:
        self.web_apis: list[tuple[str, Any, list[str], str]] = []
        self.sent: list[tuple[str, Any]] = []
        self.provider: Any = None

    def register_web_api(
        self, route: str, handler: Callable[..., Any], methods: list[str] | None = None, desc: str = ""
    ) -> None:
        self.web_apis.append((route, handler, list(methods or ["GET"]), desc))

    async def send_message(self, session: str, chain: Any) -> bool:
        self.sent.append((session, chain))
        return True

    def get_using_provider(self) -> Any:
        return self.provider

    def get_provider_by_id(self, provider_id: str) -> Any:
        return self.provider if provider_id else None

    def get_all_providers(self) -> list[Any]:
        return [self.provider] if self.provider is not None else []


class Star:
    def __init__(self, context: Context | None = None) -> None:
        self.context = context


class StarTools:
    @staticmethod
    def get_data_dir(plugin_name: str | None = None) -> str:
        override = os.environ.get("GG_TEST_DATA_DIR")
        if override:
            path = Path(override)
        else:
            path = Path(tempfile.gettempdir()) / "ggnext-stub-data"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)


class AstrBotConfig(dict):
    pass


def register(*args: Any, **kwargs: Any) -> Callable[[Any], Any]:
    def _decorator(cls: Any) -> Any:
        cls.__plugin_register_args__ = (args, kwargs)
        return cls

    return _decorator


__all__ = ["AstrBotConfig", "Context", "Star", "StarTools", "register"]
