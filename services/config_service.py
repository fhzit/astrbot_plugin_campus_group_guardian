# -*- coding: utf-8 -*-
"""配置服务（§134-§138、§121）。

- 配置按逻辑分组存放（general / platform / moderation / anti_flood / anti_fraud / ocr / ai / actions / storage / webui / advanced）；
- 支持「全局默认 + 群级覆盖」；
- 群级覆盖按点号键保存，缺省或空值一律回退全局值。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from ..core.antifraud.engine import AntiFraudConfig
from ..core.antifraud.models import FraudThresholds
from ..core.moderation.engine import ModerationConfig

_LOG = "[Guardian][Config]"

#: 全局默认配置（与 _conf_schema.json 保持一致）。
DEFAULT_CONFIG: dict[str, Any] = {
    "general": {
        "enabled": True,
        "group_white_list": [],
        "group_black_list": [],
        "plugin_admins": [],
        "trusted_sender_ids": [],
    },
    "platform": {
        "ignore_bot_self_message": True,
        "role_cache_ttl_seconds": 60,
        "warn_on_full_message_unavailable": True,
    },
    "moderation": {
        "enabled": True,
        "admin_exempt": False,
        "llm_verify": True,
        "llm_always": False,
        "llm_provider_id": "",
        "context_messages": 30,
        "recall_on_violation": True,
        "warn_on_violation": True,
        "mute_seconds": 0,
        "enabled_categories": [],
        "lexicon_path": "",
    },
    "anti_fraud": {
        "enabled": True,
        "risk_threshold_suspicious": 30,
        "risk_threshold_high": 60,
        "risk_threshold_critical": 85,
        "context_messages": 10,
        "context_seconds": 300,
        "teacher_names": [],
        "trusted_sender_ids": [],
        "auto_recall_high": False,
        "auto_recall_critical": True,
        "auto_mute_critical": True,
        "critical_mute_seconds": 600,
        "auto_kick_critical": False,
        "auto_blacklist_critical": False,
        "group_warning_enabled": True,
        "stop_event_on_critical": True,
        "notify_admin": True,
        "test_lab_enabled": True,
    },
    "ocr": {
        "enabled": True,
        "strategy": "uapi_then_ai",
        "uapi_api_key": "",
        "uapi_endpoint": "https://uapis.cn/api/v1/image/ocr",
        "timeout": 15,
        "max_retries": 2,
        "max_concurrency": 2,
        "cache_ttl_seconds": 600,
        "strict_image_scan": False,
        "enable_cls": False,
    },
    "ai": {
        "enabled": False,
        "moderation_provider_id": "",
        "vision_provider_id": "",
        "fraud_provider_id": "",
        "timeout": 20,
        "max_concurrency": 2,
        "only_when_uncertain": True,
    },
    "actions": {
        "recall_when_supported": True,
        "mute_when_supported": True,
        "notify_admin_session": [],
        "group_warning_text": "",
    },
    "storage": {
        "violation_retention_days": 90,
        "incident_retention_days": 180,
        "action_log_retention_days": 30,
        "cleanup_interval_hours": 12,
    },
    "webui": {
        "incident_page_size": 50,
        "show_capability_matrix": True,
    },
    "advanced": {
        "log_level": "info",
        "scheduler_enabled": True,
        "unmute_scan_interval_seconds": 60,
    },
}


def flatten(config: dict[str, Any] | None, prefix: str = "") -> dict[str, Any]:
    """把嵌套配置压平成点号键。"""
    result: dict[str, Any] = {}
    if not isinstance(config, dict):
        return result
    for key, value in config.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            result.update(flatten(value, path))
        else:
            result[path] = value
    return result


def unflatten(flat: dict[str, Any]) -> dict[str, Any]:
    """把点号键还原为嵌套结构。"""
    nested: dict[str, Any] = {}
    for path, value in flat.items():
        parts = str(path).split(".")
        cursor = nested
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return nested


def _is_unset(value: Any) -> bool:
    """群级覆盖里视为“未设置”的值。"""
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, dict, tuple)) and len(value) == 0:
        return True
    return False


def merge_overrides(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """把群级覆盖合并到全局配置上，未设置的值一律沿用全局值。"""
    merged = dict(base)
    for key, value in (overrides or {}).items():
        if _is_unset(value):
            continue
        merged[key] = value
    return merged


@dataclass
class ConfigSnapshot:
    """某群的生效配置。"""

    group_key: str
    values: dict[str, Any]

    def get(self, dotted: str, default: Any = None) -> Any:
        return self.values.get(dotted, default)

    def get_bool(self, dotted: str, default: bool = False) -> bool:
        return bool(self.values.get(dotted, default))

    def get_int(self, dotted: str, default: int = 0) -> int:
        try:
            return int(self.values.get(dotted, default))
        except (TypeError, ValueError):
            return default

    def get_float(self, dotted: str, default: float = 0.0) -> float:
        try:
            return float(self.values.get(dotted, default))
        except (TypeError, ValueError):
            return default

    def get_str(self, dotted: str, default: str = "") -> str:
        value = self.values.get(dotted, default)
        return str(value) if value is not None else default

    def get_list(self, dotted: str) -> list[Any]:
        value = self.values.get(dotted)
        if isinstance(value, (list, tuple, set)):
            return [item for item in value]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return []


class ConfigService:
    """配置读取与群级覆盖。"""

    def __init__(self, config: dict[str, Any] | None = None, storage: Any = None) -> None:
        self._raw = config if isinstance(config, dict) else {}
        self.storage = storage
        self._global_flat = merge_overrides(flatten(DEFAULT_CONFIG), flatten(self._raw))
        self._cache: dict[str, ConfigSnapshot] = {}

    def reload(self, config: dict[str, Any] | None = None) -> None:
        """重新读取配置（WebUI 保存后调用）。"""
        if isinstance(config, dict):
            self._raw = config
        self._global_flat = merge_overrides(flatten(DEFAULT_CONFIG), flatten(self._raw))
        self._cache.clear()

    @property
    def global_flat(self) -> dict[str, Any]:
        return dict(self._global_flat)

    def global_snapshot(self) -> ConfigSnapshot:
        return ConfigSnapshot(group_key="global", values=dict(self._global_flat))

    async def resolved(self, group_key: str) -> ConfigSnapshot:
        """取得某群的生效配置（全局默认 + 群级覆盖）。"""
        key = str(group_key or "global")
        if key in self._cache:
            return self._cache[key]
        overrides: dict[str, Any] = {}
        if self.storage is not None and key != "global":
            try:
                overrides = await self.storage.get_group_override(key)
            except Exception as exc:
                logger.debug(f"{_LOG} 读取群级覆盖失败：{exc}")
        values = merge_overrides(self._global_flat, overrides)
        snapshot = ConfigSnapshot(group_key=key, values=values)
        self._cache[key] = snapshot
        return snapshot

    def invalidate(self, group_key: str | None = None) -> None:
        if group_key is None:
            self._cache.clear()
        else:
            self._cache.pop(str(group_key), None)

    # ------------------------------------------------------------------
    # 派生配置
    # ------------------------------------------------------------------
    def plugin_admins(self, snapshot: ConfigSnapshot | None = None) -> set[str]:
        """插件管理员（只读 general.plugin_admins）。

        注意：可信操作者（trusted_sender_ids）是更窄的角色，
        绝不能并入插件管理员，否则群里的可信成员会拿到改配置/踢人的权限（§36、§37）。
        """
        values = snapshot or self.global_snapshot()
        return {str(item) for item in values.get_list("general.plugin_admins")}

    def group_allowed(self, group_id: str, snapshot: ConfigSnapshot | None = None) -> bool:
        """群白/黑名单判断。黑名单优先，白名单为空表示全部允许。"""
        values = snapshot or self.global_snapshot()
        gid = str(group_id or "")
        black = {str(item) for item in values.get_list("general.group_black_list")}
        if gid and gid in black:
            return False
        white = {str(item) for item in values.get_list("general.group_white_list")}
        if white and gid not in white:
            return False
        return True

    def fraud_config(self, snapshot: ConfigSnapshot) -> AntiFraudConfig:
        """构造反诈配置。"""
        thresholds = FraudThresholds(
            suspicious=snapshot.get_float("anti_fraud.risk_threshold_suspicious", 30.0),
            high=snapshot.get_float("anti_fraud.risk_threshold_high", 60.0),
            critical=snapshot.get_float("anti_fraud.risk_threshold_critical", 85.0),
        )
        trusted = list(snapshot.get_list("anti_fraud.trusted_sender_ids"))
        trusted.extend(snapshot.get_list("general.trusted_sender_ids"))
        return AntiFraudConfig(
            enabled=snapshot.get_bool("anti_fraud.enabled", True),
            thresholds=thresholds,
            context_messages=snapshot.get_int("anti_fraud.context_messages", 10),
            context_seconds=snapshot.get_int("anti_fraud.context_seconds", 300),
            teacher_names=[str(item) for item in snapshot.get_list("anti_fraud.teacher_names")],
            trusted_sender_ids=[str(item) for item in trusted],
            ocr_enabled=snapshot.get_bool("ocr.enabled", True),
            strict_image_scan=snapshot.get_bool("ocr.strict_image_scan", False),
            ai_enabled=snapshot.get_bool("ai.enabled", False),
            ai_only_when_uncertain=snapshot.get_bool("ai.only_when_uncertain", True),
            auto_recall_high=snapshot.get_bool("anti_fraud.auto_recall_high", False),
            auto_recall_critical=snapshot.get_bool("anti_fraud.auto_recall_critical", True),
            auto_mute_critical=snapshot.get_bool("anti_fraud.auto_mute_critical", True),
            critical_mute_seconds=snapshot.get_int("anti_fraud.critical_mute_seconds", 600),
            auto_kick_critical=snapshot.get_bool("anti_fraud.auto_kick_critical", False),
            auto_blacklist_critical=snapshot.get_bool("anti_fraud.auto_blacklist_critical", False),
            group_warning_enabled=snapshot.get_bool("anti_fraud.group_warning_enabled", True),
            stop_event_on_critical=snapshot.get_bool("anti_fraud.stop_event_on_critical", True),
        )

    def moderation_config(self, snapshot: ConfigSnapshot) -> ModerationConfig:
        return ModerationConfig(
            enabled=snapshot.get_bool("moderation.enabled", True),
            admin_exempt=snapshot.get_bool("moderation.admin_exempt", False),
            llm_verify=snapshot.get_bool("moderation.llm_verify", True),
            llm_always=snapshot.get_bool("moderation.llm_always", False),
            llm_provider_id=snapshot.get_str("moderation.llm_provider_id"),
            context_messages=snapshot.get_int("moderation.context_messages", 30),
            recall_on_violation=snapshot.get_bool("moderation.recall_on_violation", True),
            warn_on_violation=snapshot.get_bool("moderation.warn_on_violation", True),
            mute_seconds=snapshot.get_int("moderation.mute_seconds", 0),
            enabled_categories=set(snapshot.get_list("moderation.enabled_categories")),
        )


def stop_event_enabled(snapshot: ConfigSnapshot) -> bool:
    return snapshot.get_bool("anti_fraud.stop_event_on_critical", True)


def group_warning_enabled(snapshot: ConfigSnapshot) -> bool:
    return snapshot.get_bool("anti_fraud.group_warning_enabled", True)


def admin_notify_enabled(snapshot: ConfigSnapshot) -> bool:
    return snapshot.get_bool("anti_fraud.notify_admin", True)
