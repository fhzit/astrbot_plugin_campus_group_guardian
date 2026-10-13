# -*- coding: utf-8 -*-
"""平台适配器注册表：按 AstrBot 平台名选择适配器，业务层不需要 if/elif 平台分支。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .base import GuardianPlatformAdapter
from .dingtalk import DingTalkPlatformAdapter
from .onebot import OneBotPlatformAdapter
from .qq_official import QQOfficialPlatformAdapter
from .wecom import WeComAiBotPlatformAdapter, WeComPlatformAdapter

_LOG = "[Guardian][Platform]"

#: AstrBot 平台适配器类型名 → 适配器类。
ADAPTER_MAP: dict[str, type[GuardianPlatformAdapter]] = {
    "aiocqhttp": OneBotPlatformAdapter,
    "qq_official": QQOfficialPlatformAdapter,
    "qq_official_webhook": QQOfficialPlatformAdapter,
    "wecom": WeComPlatformAdapter,
    "wecom_ai_bot": WeComAiBotPlatformAdapter,
    "dingtalk": DingTalkPlatformAdapter,
}

#: 插件声明支持的平台（metadata.yaml / §4、§11）。
SUPPORTED_PLATFORMS = tuple(ADAPTER_MAP.keys())


class PlatformAdapterRegistry:
    """适配器缓存与查找。"""

    def __init__(self, context: Any = None) -> None:
        self.context = context
        self._instances: dict[str, GuardianPlatformAdapter] = {}

    def create(self, platform_name: str) -> GuardianPlatformAdapter | None:
        """按平台名取得适配器实例（每个平台名复用同一实例）。"""
        key = str(platform_name or "")
        if not key:
            return None
        if key in self._instances:
            return self._instances[key]
        adapter_cls = ADAPTER_MAP.get(key)
        if adapter_cls is None:
            logger.debug(f"{_LOG} 未注册的平台适配器：{key}")
            return None
        adapter = adapter_cls(self.context)
        adapter.profile.name = key
        self._instances[key] = adapter
        return adapter

    def create_for_event(self, event: Any) -> GuardianPlatformAdapter | None:
        """按事件所属平台取得适配器。"""
        try:
            platform_name = str(event.get_platform_name() or "")
        except Exception:
            return None
        return self.create(platform_name)

    def supports_platform(self, platform_name: str) -> bool:
        return str(platform_name or "") in ADAPTER_MAP

    def supported_platform_names(self) -> tuple[str, ...]:
        """插件声明支持的平台名列表。"""
        return SUPPORTED_PLATFORMS

    def profiles(self) -> list[dict[str, Any]]:
        """所有已注册平台的静态能力画像（WebUI「平台能力」页）。"""
        rows: list[dict[str, Any]] = []
        for platform_name in SUPPORTED_PLATFORMS:
            adapter = self.create(platform_name)
            if adapter is None:
                continue
            rows.append(
                {
                    "platform": platform_name,
                    "adapter": type(adapter).__name__,
                    "loaded": platform_name in self._instances,
                    "capabilities": adapter.capability_matrix(),
                    "limits": adapter.limits.as_dict(),
                    "recall_policy": adapter.profile.recall_policy.as_dict(),
                    "notes": list(adapter.profile.notes),
                }
            )
        return rows

    async def close_all(self) -> None:
        for adapter in self._instances.values():
            try:
                await adapter.close()
            except Exception as exc:  # 关闭失败不影响卸载
                logger.debug(f"{_LOG} 适配器关闭失败：{exc}")
        self._instances.clear()
