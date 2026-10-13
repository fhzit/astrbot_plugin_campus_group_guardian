# -*- coding: utf-8 -*-
"""Plugin Page 后端 Web API（§107-§110、§169）。

全部通过 ``context.register_web_api`` 注册，复用 Dashboard 鉴权；
不启动独立 Web 服务，不占用新端口。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .commands.anti_fraud import run_detector_test
from .core.antifraud.models import FraudThresholds, IncidentStatus
from .core.models import GroupRole

_LOG = "[Guardian][WebAPI]"

ALLOWED_INCIDENT_STATUS = {
    "confirmed_fraud": IncidentStatus.CONFIRMED_FRAUD,
    "false_positive": IncidentStatus.FALSE_POSITIVE,
    "resolved": IncidentStatus.RESOLVED,
    "open": IncidentStatus.OPEN,
}


class WebApiMixin:
    """Web API 路由集合（由 main.py 的插件类继承）。"""

    plugin_name: str = "astrbot_plugin_campus_group_guardian"

    def register_web_apis(self) -> None:
        """注册全部路由。"""
        routes = [
            (f"/{self.plugin_name}/overview", self.web_overview, ["GET"], "反诈与审核总览"),
            (f"/{self.plugin_name}/capabilities", self.web_capabilities, ["GET"], "平台能力矩阵"),
            (f"/{self.plugin_name}/incidents", self.web_incidents, ["GET"], "反诈事件列表"),
            (f"/{self.plugin_name}/incidents/status", self.web_incident_status, ["POST"], "更新事件状态"),
            (f"/{self.plugin_name}/config", self.web_config, ["GET"], "读取生效配置"),
            (f"/{self.plugin_name}/config/override", self.web_config_override, ["POST"], "保存群级覆盖"),
            (f"/{self.plugin_name}/config/override/delete", self.web_config_override_delete, ["POST"], "删除群级覆盖"),
            (f"/{self.plugin_name}/logs", self.web_logs, ["GET"], "动作日志"),
            (f"/{self.plugin_name}/diagnostics", self.web_diagnostics, ["GET"], "平台与迁移诊断"),
            (f"/{self.plugin_name}/test", self.web_test, ["POST"], "反诈测试器（只检测）"),
            (f"/{self.plugin_name}/trusted", self.web_trusted, ["GET"], "可信操作者列表"),
            (f"/{self.plugin_name}/trusted/add", self.web_trusted_add, ["POST"], "新增可信操作者"),
            (f"/{self.plugin_name}/migrate/legacy", self.web_migrate_legacy, ["POST"], "旧版数据迁移"),
        ]
        for route, handler, methods, desc in routes:
            try:
                self.context.register_web_api(route, handler, methods, desc)
            except Exception as exc:  # 注册失败不影响插件其他功能
                logger.warning(f"{_LOG} 注册 {route} 失败：{exc}")

    # ------------------------------------------------------------------
    # 只读接口
    # ------------------------------------------------------------------
    async def web_overview(self):
        """总览：统计 + 平台 + 反诈概况。"""
        stats = await self.storage.stats_snapshot() if self.storage else {}
        trend = await self.storage.recent_incident_counts(days=7) if self.storage else []
        return json_response(
            {
                "stats": stats,
                "trend": trend,
                "ocr": self.ocr_service.stats_snapshot() if self.ocr_service else {},
                "ai": self.ai_reviewer.stats_snapshot() if self.ai_reviewer else {},
                "antifraud_stats": dict(self.fraud_guard.stats) if self.fraud_guard else {},
                "moderation_stats": dict(self.moderation_engine.stats) if self.moderation_engine else {},
                "platforms": [item["platform"] for item in self.registry.profiles()],
            }
        )

    async def web_capabilities(self):
        """平台能力矩阵（含限制与撤回策略）。"""
        return json_response(
            {
                "platforms": self.registry.profiles(),
                "loaded_adapters": sorted(self.registry._instances.keys()),  # noqa: SLF001
                "supported_platforms": list(self.registry.supported_platform_names()),
            }
        )

    async def web_incidents(self):
        """反诈事件列表。"""
        status = request.query.get("status") or None
        group_key = request.query.get("group_key") or None
        limit = request.query.get("limit", 50, type=int)
        if not self.storage:
            return error_response("数据库未就绪", status_code=503)
        incidents = await self.storage.list_incidents(limit=limit, status=status, group_key=group_key)
        return json_response({"incidents": incidents, "count": len(incidents)})

    async def web_logs(self):
        """动作日志（最近若干条）。"""
        limit = request.query.get("limit", 100, type=int)
        if not self.storage:
            return error_response("数据库未就绪", status_code=503)
        return json_response({"logs": await self.storage.list_action_logs(limit=limit)})

    async def web_config(self):
        """读取某群（或全局）的生效配置。"""
        group_key = request.query.get("group_key") or "global"
        snapshot = await self.config_service.resolved(group_key)
        return json_response(
            {
                "group_key": snapshot.group_key,
                "values": snapshot.values,
                "global_defaults": self.config_service.global_flat,
            }
        )

    async def web_diagnostics(self):
        """平台诊断 + 迁移状态（§158、§159、§190）。"""
        diagnostics: dict[str, Any] = {
            "platforms": self.registry.profiles(),
            "legacy": self.storage.detect_legacy_data() if self.storage else {},
            "migration": getattr(self.storage, "migration_report", {}) if self.storage else {},
            "lexicon": {
                "keywords": self.matcher.keyword_count if self.matcher else 0,
                "regex_rules": self.matcher.regex_count if self.matcher else 0,
                "source": self.lexicon_source,
            },
            "qq_official": self._qq_full_message_diagnosis(),
        }
        return json_response(diagnostics)

    async def web_trusted(self):
        """可信操作者列表。"""
        if not self.storage:
            return error_response("数据库未就绪", status_code=503)
        return json_response({"operators": await self.storage.list_trusted_operators()})

    # ------------------------------------------------------------------
    # 写接口
    # ------------------------------------------------------------------
    async def web_incident_status(self):
        """人工复核：确认诈骗 / 标记误判 / 标记已处理。"""
        payload = await request.json(default={})
        incident_id = payload.get("id")
        target = str(payload.get("status") or "").strip()
        if not isinstance(incident_id, int) or target not in ALLOWED_INCIDENT_STATUS:
            return error_response("参数错误：需要 id 与合法的 status", status_code=400)
        if not self.storage:
            return error_response("数据库未就绪", status_code=503)
        updated = await self.storage.update_incident_status(
            incident_id,
            ALLOWED_INCIDENT_STATUS[target],
            resolved_by=request.username or "dashboard",
        )
        if not updated:
            return error_response("事件不存在", status_code=404)
        return json_response({"updated": True, "status": target})

    async def web_config_override(self):
        """保存群级覆盖（只覆盖显式给出的键）。"""
        payload = await request.json(default={})
        group_key = str(payload.get("group_key") or "")
        overrides = payload.get("overrides")
        if not group_key or ":" not in group_key:
            return error_response("group_key 需要形如 platform:adapter:group", status_code=400)
        if not isinstance(overrides, dict):
            return error_response("overrides 必须是对象", status_code=400)
        unknown = [key for key in overrides if key not in self.config_service.global_flat]
        if unknown:
            return error_response(f"未知配置键：{unknown}", status_code=400)
        if not self.storage:
            return error_response("数据库未就绪", status_code=503)
        await self.storage.set_group_override(group_key, overrides)
        self.config_service.invalidate(group_key)
        return json_response({"saved": True, "group_key": group_key})

    async def web_config_override_delete(self):
        payload = await request.json(default={})
        group_key = str(payload.get("group_key") or "")
        if not group_key:
            return error_response("缺少 group_key", status_code=400)
        if not self.storage:
            return error_response("数据库未就绪", status_code=503)
        await self.storage.delete_group_override(group_key)
        self.config_service.invalidate(group_key)
        return json_response({"deleted": True, "group_key": group_key})

    async def web_test(self):
        """测试器：只运行检测器，绝不执行撤回/禁言/踢人（§155、§156、§157）。"""
        payload = await request.json(default={})
        text = str(payload.get("text") or "")
        if not text.strip():
            return error_response("请输入待检测文本", status_code=400)
        role = GroupRole.parse(payload.get("role") or "member")
        sender_name = payload.get("sender_name") or None
        group_key = str(payload.get("group_key") or "global")
        snapshot = await self.config_service.resolved(group_key)
        thresholds = FraudThresholds(
            suspicious=snapshot.get_float("anti_fraud.risk_threshold_suspicious", 30.0),
            high=snapshot.get_float("anti_fraud.risk_threshold_high", 60.0),
            critical=snapshot.get_float("anti_fraud.risk_threshold_critical", 85.0),
        )
        result = run_detector_test(
            text,
            sender_role=role,
            sender_name=sender_name,
            teacher_names=[str(item) for item in snapshot.get_list("anti_fraud.teacher_names")],
            thresholds=thresholds,
        )
        result["executed"] = False
        return json_response(result)

    async def web_trusted_add(self):
        payload = await request.json(default={})
        platform = str(payload.get("platform") or "")
        adapter_id = str(payload.get("adapter_id") or "")
        user_id = str(payload.get("user_id") or "")
        if not platform or not user_id:
            return error_response("platform 与 user_id 必填", status_code=400)
        if not self.storage:
            return error_response("数据库未就绪", status_code=503)
        await self.storage.add_trusted_operator(
            platform=platform,
            adapter_id=adapter_id,
            user_id=user_id,
            group_id=payload.get("group_id"),
            note=str(payload.get("note") or ""),
        )
        return json_response({"added": True})

    async def web_migrate_legacy(self):
        """旧版数据迁移：先备份，再导入日志（§128、§190）。"""
        if not self.storage:
            return error_response("数据库未就绪", status_code=503)
        payload = await request.json(default={})
        report = self.storage.detect_legacy_data()
        if not report.get("detected"):
            return error_response("未检测到旧版 GroupGuardian 数据", status_code=404)
        if not payload.get("confirm"):
            return json_response({"detected": True, "report": report, "need_confirm": True})
        legacy_db = (report.get("database") or {}).get("path")
        if not legacy_db:
            return error_response("未找到旧版数据库", status_code=404)
        backup = await self.storage.backup_now(reason="pre_legacy_import")
        imported = await self.storage.import_legacy_logs(legacy_db)
        return json_response(
            {
                "imported": imported,
                "backup": str(backup) if backup else None,
                "report": report,
            }
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _qq_full_message_diagnosis(self) -> dict[str, Any]:
        """QQ 官方全量消息诊断（仅当存在 QQ 官方实例时才有意义）。"""
        adapter = self.registry.create("qq_official")
        if adapter is None or not hasattr(adapter, "full_message_diagnosis"):
            return {"level": "n/a", "hint": "未使用 QQ 官方机器人适配器。"}
        return adapter.full_message_diagnosis()
