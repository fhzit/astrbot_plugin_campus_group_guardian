# -*- coding: utf-8 -*-
"""CampusGroupGuardian 插件入口。

装配顺序：配置 → 平台适配器注册表 → （后台）数据库/词库/引擎 → 事件处理器 / 指令 / LLM 工具 / WebUI。
启动不做重活：数据库迁移、词库加载都放在 initialize() 的后台任务里，避免拖慢 AstrBot 启动。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.star.filter.command import GreedyStr

from .commands.anti_fraud import (
    parse_toggle,
    render_antifraud_status,
    render_guardian_status,
    render_test_result,
    run_detector_test,
)
from .commands.management import (
    build_permission_context,
    format_action_results,
    parse_duration_minutes,
    parse_target,
)
from .commands.statistics import render_overview
from .core.actions.engine import ActionEngine
from .core.actions.models import ActionStatus, GuardianAction, GuardianActionType
from .core.actions.policy import build_fraud_actions, incident_required, should_stop_event
from .core.antifraud.engine import SchoolFraudGuard
from .core.event_router import GuardianEventRouter
from .core.models import GroupRole, GuardianMessage
from .core.moderation.engine import ContentModerationEngine
from .core.moderation.matcher import RegexRule, RuleMatcher
from .core.permissions import PermissionContext
from .platforms.registry import PlatformAdapterRegistry
from .providers.ai import AiReviewer, FraudAiReviewer
from .providers.ocr import (
    UAPI_OCR_ENDPOINT,
    AiVisionOcrProvider,
    OcrService,
    UapiOcrProvider,
)
from .services.config_service import ConfigService
from .services.notification_service import NotificationService
from .storage.database import GuardianStorage
from .tools.group_management import execute_management_tool
from .web_api import WebApiMixin

PLUGIN_NAME = "astrbot_plugin_campus_group_guardian"
PLUGIN_VERSION = "v0.1.0"
PLUGIN_REPO = "https://github.com/fhzit/astrbot_plugin_campus_group_guardian"
#: 原版插件（数据迁移来源）。
LEGACY_PLUGIN_NAME = "astrbot_plugin_group_guardian"

_LOG = "[Guardian]"

#: 只治理这些平台（§10）。
PLATFORM_FILTER = (
    filter.PlatformAdapterType.AIOCQHTTP
    | filter.PlatformAdapterType.QQOFFICIAL
    | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
    | filter.PlatformAdapterType.WECOM
    | filter.PlatformAdapterType.WECOM_AI_BOT
    | filter.PlatformAdapterType.DINGTALK
)

#: 原版词库分类 → 本插件分类。
LEGACY_CATEGORY_MAP = {
    "ad": "advertisement",
    "illegal_url": "url",
    "political": "political",
    "porn": "porn",
    "violent_terror": "violence",
    "weapons": "weapon",
    "swear": "abuse",
    "corruption": "other",
    "reactionary": "political",
    "livelihood": "other",
    "supplement": "other",
    "tencent_ban": "other",
    "other": "other",
}

#: 单次加载的正则规则上限（超出部分跳过并记录，避免逐条匹配拖慢事件循环）。
REGEX_RULE_LIMIT = 800


@register(PLUGIN_NAME, "fhzit", "跨平台群聊治理 + 家校群虚假缴费反诈", PLUGIN_VERSION, PLUGIN_REPO)
class CampusGroupGuardian(WebApiMixin, Star):
    """插件主类。"""

    plugin_name = PLUGIN_NAME

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.config_service = ConfigService(self.config)
        self.registry = PlatformAdapterRegistry(context)
        self.storage: GuardianStorage | None = None
        self.matcher: RuleMatcher | None = None
        self.moderation_engine: ContentModerationEngine | None = None
        self.fraud_guard: SchoolFraudGuard | None = None
        self.ocr_service: OcrService | None = None
        self.ai_reviewer: AiReviewer | None = None
        self.fraud_ai_reviewer: FraudAiReviewer | None = None
        self.notifier: NotificationService | None = None
        self.lexicon_source = "未加载"
        self._data_dir = Path(StarTools.get_data_dir())
        self._role_cache: dict[tuple[str, str, str], tuple[float, GroupRole]] = {}
        self._bot_role_cache: dict[tuple[str, str], tuple[float, GroupRole]] = {}
        self._ready = False
        self._init_task: asyncio.Task | None = None
        self._scheduler_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """注册 Web API 并启动后台初始化。"""
        self.register_web_apis()
        self._init_task = asyncio.create_task(self._bootstrap())
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        logger.info(f"{_LOG} 插件已加载，后台初始化中…")

    async def _bootstrap(self) -> None:
        """后台重活：数据库迁移、词库加载、引擎装配。"""
        try:
            legacy_dir = self._legacy_dir()
            self.storage = GuardianStorage(self._data_dir, legacy_dir=legacy_dir)
            await self.storage.initialize()
            logger.info(
                f"{_LOG}[Storage] 数据库就绪：{self.storage.db_path.name}"
                f"（schema v{self.storage.migration_report.get('schema_version')}）"
            )

            keywords, regex_rules, source = await self._load_lexicon()
            self.lexicon_source = source
            self.matcher = RuleMatcher(keywords, regex_rules)
            logger.info(
                f"{_LOG}[Moderation] 词库加载完成：关键词 {self.matcher.keyword_count} 条、"
                f"正则 {self.matcher.regex_count} 条（{source}）"
            )

            self._build_providers()
            self.moderation_engine = ContentModerationEngine(self.matcher, self.ai_reviewer)
            matcher_fast = self.config_service.global_snapshot().get_bool(
                "advanced.matcher_use_pyahocorasick", False
            )
            native_enabled = self.matcher.configure_pyahocorasick(matcher_fast)
            if matcher_fast and not native_enabled:
                logger.warning(
                    f"{_LOG}[Moderation] 配置启用了 pyahocorasick，但扩展未安装；回退到纯 Python AC。"
                )
            self.fraud_guard = SchoolFraudGuard(
                ocr_service=self.ocr_service,
                ai_reviewer=self.fraud_ai_reviewer,
            )
            self.notifier = NotificationService(
                self.context,
                config_service=self.config_service,
                storage=self.storage,
                warning_text=self.config_service.global_snapshot().get_str("actions.group_warning_text"),
            )
            self._ready = True
            logger.info(f"{_LOG} 初始化完成，开始接管群消息。")
        except Exception as exc:
            logger.error(f"{_LOG} 初始化失败，插件将以降级模式运行：{exc}")

    def _build_providers(self) -> None:
        """按配置装配 OCR / AI Provider。"""
        snapshot = self.config_service.global_snapshot()
        ocr_enabled = snapshot.get_bool("ocr.enabled", True)
        uapi = None
        ai_vision = None
        if ocr_enabled:
            api_key = snapshot.get_str("ocr.uapi_api_key")
            if api_key:
                uapi = UapiOcrProvider(
                    api_key,
                    base_url=snapshot.get_str("ocr.uapi_endpoint") or UAPI_OCR_ENDPOINT,
                    timeout=snapshot.get_float("ocr.timeout", 15.0),
                    max_retries=snapshot.get_int("ocr.max_retries", 2),
                    max_concurrency=snapshot.get_int("ocr.max_concurrency", 2),
                    enable_cls=snapshot.get_bool("ocr.enable_cls", False),
                )
            vision_provider = snapshot.get_str("ai.vision_provider_id")
            if vision_provider:
                ai_vision = AiVisionOcrProvider(
                    self.context,
                    vision_provider,
                    timeout=snapshot.get_float("ai.timeout", 20.0),
                    max_concurrency=snapshot.get_int("ai.max_concurrency", 2),
                )
        self.ocr_service = OcrService(
            uapi=uapi,
            ai_vision=ai_vision,
            strategy=snapshot.get_str("ocr.strategy", "uapi_then_ai"),
            cache_ttl_seconds=snapshot.get_int("ocr.cache_ttl_seconds", 600),
            strict_image_scan=snapshot.get_bool("ocr.strict_image_scan", False),
        )

        ai_enabled = snapshot.get_bool("ai.enabled", False)
        moderation_provider = snapshot.get_str("ai.moderation_provider_id")
        fraud_provider = snapshot.get_str("ai.fraud_provider_id") or moderation_provider
        self.ai_reviewer = AiReviewer(
            self.context if ai_enabled else None,
            moderation_provider,
            timeout=snapshot.get_float("ai.timeout", 20.0),
            max_concurrency=snapshot.get_int("ai.max_concurrency", 2),
        )
        self.fraud_ai_reviewer = FraudAiReviewer(
            self.context if ai_enabled else None,
            fraud_provider,
            timeout=snapshot.get_float("ai.timeout", 20.0),
            max_concurrency=snapshot.get_int("ai.max_concurrency", 2),
        )

    async def terminate(self) -> None:
        """卸载：取消任务、关闭 HTTP 客户端与数据库（§186）。"""
        for task in (self._init_task, self._scheduler_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        if self.ocr_service is not None:
            await self.ocr_service.close()
        if self.storage is not None:
            await self.storage.close()
        await self.registry.close_all()
        self._ready = False
        logger.info(f"{_LOG} 插件已卸载，资源已释放。")

    # ------------------------------------------------------------------
    # 词库
    # ------------------------------------------------------------------
    def _legacy_dir(self) -> Path | None:
        """原版插件的数据目录（用于迁移助手与词库复用）。"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            candidate = Path(get_astrbot_data_path()) / "plugin_data" / LEGACY_PLUGIN_NAME
            return candidate if candidate.exists() else None
        except Exception:
            return None

    async def _load_lexicon(self) -> tuple[list[tuple[str, str]], list[RegexRule], str]:
        """加载词库：优先配置路径，其次原版插件数据目录里的 lexicon.db。"""
        snapshot = self.config_service.global_snapshot()
        configured = snapshot.get_str("moderation.lexicon_path")
        candidates: list[Path] = []
        if configured:
            candidates.append(Path(configured))
        # 独立插件发布包内置上游原版词库；显式配置路径优先，可用于替换/测试词库。
        candidates.append(Path(__file__).resolve().parent / "lexicon.db")
        legacy = self._legacy_dir()
        if legacy is not None:
            candidates.append(legacy / "lexicon.db")
        for path in candidates:
            if not path.exists():
                continue
            keywords, rules, skipped = self._read_lexicon_db(path)
            source = f"{path}（正则跳过 {skipped} 条）" if skipped else str(path)
            return keywords, rules, source
        return [], [], "未配置词库（可在 WebUI 诊断页导入原版 lexicon.db）"

    def _read_lexicon_db(self, path: Path) -> tuple[list[tuple[str, str]], list[RegexRule], int]:
        """同步读取词库 SQLite（在后台任务里执行，不阻塞事件处理）。"""
        keywords: list[tuple[str, str]] = []
        rules: list[RegexRule] = []
        skipped = 0
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            logger.warning(f"{_LOG}[Moderation] 词库打开失败：{exc}")
            return keywords, rules, skipped
        try:
            try:
                rows = connection.execute(
                    "SELECT category, keyword FROM lexicon_keywords WHERE keyword IS NOT NULL"
                ).fetchall()
                for category, keyword in rows:
                    text = str(keyword or "").strip()
                    # 保持原词库兼容：不丢弃短词（如广告缩写），由统一匹配策略处理。
                    if not text:
                        continue
                    keywords.append((text, LEGACY_CATEGORY_MAP.get(str(category), "other")))
            except sqlite3.Error as exc:
                logger.debug(f"{_LOG}[Moderation] 关键词表读取失败：{exc}")
            try:
                rows = connection.execute(
                    "SELECT id, category, pattern FROM moderation_rules WHERE enabled = 1"
                ).fetchall()
                for rule_id, category, pattern in rows:
                    if len(rules) >= REGEX_RULE_LIMIT:
                        skipped += 1
                        continue
                    rule = RegexRule(
                        pattern=str(pattern),
                        category=LEGACY_CATEGORY_MAP.get(str(category), "other"),
                        rule_id=int(rule_id),
                    )
                    if rule.compile() is not None:
                        rules.append(rule)
                    else:
                        skipped += 1
            except sqlite3.Error as exc:
                logger.debug(f"{_LOG}[Moderation] 正则表读取失败：{exc}")
        finally:
            connection.close()
        if skipped:
            logger.info(
                f"{_LOG}[Moderation] 正则规则超过上限（{REGEX_RULE_LIMIT}）或无法编译，"
                f"已跳过 {skipped} 条。"
            )
        return keywords, rules, skipped

    # ------------------------------------------------------------------
    # 归一化与角色解析
    # ------------------------------------------------------------------
    async def normalize_event(self, event: AstrMessageEvent) -> GuardianMessage | None:
        """把事件转成 GuardianMessage（带角色解析）。"""
        adapter = self.registry.create_for_event(event)
        if adapter is None:
            return None
        if hasattr(adapter, "note_message_event"):
            try:
                adapter.note_message_event(event)
            except Exception:
                pass
        router = GuardianEventRouter(adapter, role_resolver=self._resolve_sender_role)
        return await router.normalize(event)

    async def _resolve_sender_role(
        self,
        event: AstrMessageEvent,
        adapter: Any,
        message: GuardianMessage,
    ) -> GroupRole:
        """解析发送者群角色（带 60 秒缓存）。"""
        snapshot = self.config_service.global_snapshot()
        ttl = max(5, snapshot.get_int("platform.role_cache_ttl_seconds", 60))
        key = (message.adapter_id, message.group_id or "", message.sender_id)
        cached = self._role_cache.get(key)
        now = time.monotonic()
        if cached and (now - cached[0]) < ttl:
            return cached[1]
        role = await adapter.get_member_role(message, message.sender_id)
        self._role_cache[key] = (now, role)
        return role

    async def bot_role_for(self, message: GuardianMessage) -> GroupRole:
        """机器人自身群角色（带缓存）。"""
        adapter = self.registry.create(message.platform)
        if adapter is None:
            return GroupRole.UNKNOWN
        key = (message.adapter_id, message.group_id or "")
        cached = self._bot_role_cache.get(key)
        now = time.monotonic()
        if cached and (now - cached[0]) < 60:
            return cached[1]
        role = await adapter.get_bot_role(message)
        self._bot_role_cache[key] = (now, role)
        return role

    async def permission_for(self, message: GuardianMessage, event: AstrMessageEvent | None = None) -> PermissionContext:
        """构造权限上下文。"""
        snapshot = await self.config_service.resolved(str(message.group_key))
        is_admin = bool(event.is_admin()) if event is not None and hasattr(event, "is_admin") else False
        return build_permission_context(
            message,
            config_service=self.config_service,
            is_astrbot_admin=is_admin,
            bot_role=await self.bot_role_for(message),
        )

    @staticmethod
    def message_with_id(message: GuardianMessage, message_id: str) -> GuardianMessage:
        """复制一份带指定 message_id 的消息（撤回指定消息时使用）。"""
        from dataclasses import replace

        return replace(message, message_id=str(message_id))

    # ------------------------------------------------------------------
    # 消息处理主流程
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.platform_adapter_type(PLATFORM_FILTER)
    async def on_group_message(self, event: AstrMessageEvent):
        """统一群消息入口：审核 + 反诈 + 处置。"""
        if not self._ready:
            return
        message = await self.normalize_event(event)
        if message is None or not message.is_group:
            return

        snapshot = await self.config_service.resolved(str(message.group_key))
        if not snapshot.get_bool("general.enabled", True):
            return
        if not self.config_service.group_allowed(message.group_id, snapshot):
            return
        if snapshot.get_bool("platform.ignore_bot_self_message", True) and message.bot_id:
            if message.sender_id == message.bot_id:
                return

        try:
            await self._run_antifraud(message, snapshot)
            await self._run_moderation(message, snapshot)
        except Exception as exc:  # 单条消息处理失败不得影响后续消息
            logger.error(f"{_LOG} 处理消息失败：{exc}", exc_info=True)

    async def _run_antifraud(self, message: GuardianMessage, snapshot: Any) -> None:
        """反诈流程。检测统一，动作按平台能力降级。"""
        if self.fraud_guard is None:
            return
        config = self.config_service.fraud_config(snapshot)
        if not config.enabled:
            return
        result = await self.fraud_guard.analyze(message, config)
        if result.bypassed:
            return
        if not incident_required(result):
            return

        adapter = self.registry.create(message.platform)
        if adapter is None:
            return
        bot_role = await self.bot_role_for(message)
        actions = build_fraud_actions(result, config)
        for action in actions:
            if action.action is GuardianActionType.WARN:
                action.metadata["text"] = (
                    snapshot.get_str("actions.group_warning_text")
                    or (self.notifier.warning_text if self.notifier else "")
                )
            action.metadata["fraud_result"] = result

        engine = ActionEngine(adapter, storage=self.storage, notifier=self.notifier)
        outcome = await engine.execute(message, actions, bot_role=bot_role, origin="antifraud")

        if self.storage is not None:
            summary = "、".join(
                f"{item.action}:{item.status.value}" for item in outcome.results
            )
            for action in actions:
                action.metadata["summary"] = summary
            await self.storage.create_incident(
                message=message,
                result=result,
                actions=outcome.to_list(),
            )
        if should_stop_event(result, config):
            event = message.raw_event
            if event is not None and hasattr(event, "stop_event"):
                event.stop_event()
            logger.info(f"{_LOG}[AntiFraud] 已阻断后续处理：群={message.group_id}")

    async def _run_moderation(self, message: GuardianMessage, snapshot: Any) -> None:
        """内容审核流程（与反诈独立）。"""
        if self.moderation_engine is None:
            return
        config = self.config_service.moderation_config(snapshot)
        if not config.enabled:
            return
        decision = await self.moderation_engine.analyze(message, config)
        if not decision.is_violation:
            return
        adapter = self.registry.create(message.platform)
        if adapter is None:
            return
        engine = ActionEngine(adapter, storage=self.storage, notifier=self.notifier)
        bot_role = await self.bot_role_for(message)
        outcome = await engine.execute(message, decision.actions, bot_role=bot_role, origin="moderation")
        if self.storage is not None:
            await self.storage.record_moderation_log(
                message=message,
                category=decision.category,
                reason=decision.reason,
                matched="、".join(match.keyword for match in decision.matches[:10]),
                recalled=bool(outcome.get(GuardianActionType.RECALL))
                and outcome.get(GuardianActionType.RECALL).ok,
            )
        event = message.raw_event
        if event is not None and hasattr(event, "stop_event"):
            event.stop_event()

    # ------------------------------------------------------------------
    # 定时任务（§187）
    # ------------------------------------------------------------------
    async def _scheduler_loop(self) -> None:
        """定时解禁、缓存清理、数据保留清理。"""
        interval = 60
        last_cleanup = 0.0
        while True:
            try:
                await asyncio.sleep(interval)
                snapshot = self.config_service.global_snapshot()
                if not snapshot.get_bool("advanced.scheduler_enabled", True):
                    continue
                await self._process_due_unmutes()
                if self.ocr_service is not None:
                    self.ocr_service.prune_cache()
                if self.fraud_ai_reviewer is not None:
                    self.fraud_ai_reviewer.prune_cache()
                if self.fraud_guard is not None:
                    self.fraud_guard.context_store.prune()
                cleanup_hours = max(1, snapshot.get_int("storage.cleanup_interval_hours", 12))
                if time.monotonic() - last_cleanup > cleanup_hours * 3600:
                    await self._cleanup(snapshot)
                    last_cleanup = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"{_LOG}[Scheduler] 循环异常：{exc}")

    async def _process_due_unmutes(self) -> None:
        """到点自动解禁：真正调用平台接口，失败则保留记录等待下次重试。"""
        if self.storage is None:
            return
        for record in await self.storage.due_unmutes():
            platform = str(record.get("platform", ""))
            adapter = self.registry.create(platform)
            if adapter is None:
                await self.storage.mark_unmute_done(int(record["id"]))
                continue
            message = GuardianMessage(
                platform=platform,
                adapter_id=str(record.get("adapter_id") or platform),
                unified_msg_origin=str(record.get("session") or ""),
                conversation_id=str(record.get("group_id") or ""),
                group_id=str(record.get("group_id") or ""),
                message_id=str(record.get("message_id") or ""),
                sender_id=str(record.get("user_id") or ""),
                sender_name=None,
                sender_role=GroupRole.UNKNOWN,
                text="",
                bot_id=str(record.get("bot_id") or "") or None,
            )
            result = await adapter.unmute_member(message, str(record.get("user_id") or ""))
            if result.ok:
                logger.info(
                    f"{_LOG}[Scheduler] 定时解禁成功：群={record.get('group_id')} 成员={record.get('user_id')}"
                )
                await self.storage.mark_unmute_done(int(record["id"]))
            elif result.status is ActionStatus.UNSUPPORTED:
                logger.info(
                    f"{_LOG}[Scheduler] 平台不支持主动解禁，跳过：{record.get('group_id')}"
                )
                await self.storage.mark_unmute_done(int(record["id"]))
            else:
                logger.debug(f"{_LOG}[Scheduler] 定时解禁失败，稍后重试：{result.message}")

    async def _cleanup(self, snapshot: Any) -> None:
        """按保留策略清理历史数据。"""
        if self.storage is None:
            return
        removed = await self.storage.cleanup(
            violation_days=snapshot.get_int("storage.violation_retention_days", 90),
            incident_days=snapshot.get_int("storage.incident_retention_days", 180),
            action_days=snapshot.get_int("storage.action_log_retention_days", 30),
        )
        if any(removed.values()):
            logger.info(f"{_LOG}[Storage] 数据保留清理：{removed}")

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    @filter.command("守护状态", alias={"guardian_status"})
    async def cmd_guardian_status(self, event: AstrMessageEvent):
        """用途：/守护状态 查看插件运行状态"""
        message = await self.normalize_event(event)
        if message is None:
            yield event.plain_result("该指令只能在群聊中使用。")
            return
        snapshot = await self.config_service.resolved(str(message.group_key))
        stats = await self.storage.stats_snapshot() if self.storage else {}
        yield event.plain_result(
            render_guardian_status(snapshot, self.registry.profiles(), stats)
        )

    @filter.command("守护统计", alias={"guardian_stats"})
    async def cmd_guardian_stats(self, event: AstrMessageEvent):
        """用途：/守护统计 查看审核与反诈统计"""
        stats = await self.storage.stats_snapshot() if self.storage else {}
        yield event.plain_result(
            render_overview(
                stats,
                self.ocr_service.stats_snapshot() if self.ocr_service else None,
                self.ai_reviewer.stats_snapshot() if self.ai_reviewer else None,
            )
        )

    @filter.command("反诈状态", alias={"antifraud_status"})
    async def cmd_antifraud_status(self, event: AstrMessageEvent):
        """用途：/反诈状态 查看反诈模块状态"""
        message = await self.normalize_event(event)
        group_key = str(message.group_key) if message else "global"
        snapshot = await self.config_service.resolved(group_key)
        stats = await self.storage.stats_snapshot() if self.storage else {}
        yield event.plain_result(render_antifraud_status(snapshot, stats))

    @filter.command("反诈开启", alias={"antifraud_on"})
    async def cmd_antifraud_on(self, event: AstrMessageEvent):
        """用途：/反诈开启 开启本群反诈检测"""
        yield event.plain_result(await self._toggle_antifraud(event, True))

    @filter.command("反诈关闭", alias={"antifraud_off"})
    async def cmd_antifraud_off(self, event: AstrMessageEvent):
        """用途：/反诈关闭 关闭本群反诈检测"""
        yield event.plain_result(await self._toggle_antifraud(event, False))

    async def _toggle_antifraud(self, event: AstrMessageEvent, enabled: bool) -> str:
        """写入群级覆盖，切换反诈开关。"""
        message = await self.normalize_event(event)
        if message is None:
            return "该指令只能在群聊中使用。"
        permission = await self.permission_for(message, event)
        if not permission.can_manage_group():
            return f"没有权限修改反诈开关（当前身份：{permission.level_label()}）。"
        if self.storage is None:
            return "数据库尚未就绪，请稍后重试。"
        await self.storage.set_group_override(
            str(message.group_key), {"anti_fraud.enabled": bool(enabled)}
        )
        self.config_service.invalidate(str(message.group_key))
        return f"本群反诈检测已{'开启' if enabled else '关闭'}。"

    @filter.command("反诈测试", alias={"antifraud_test"})
    async def cmd_antifraud_test(self, event: AstrMessageEvent, text: GreedyStr = ""):
        """用途：/反诈测试 <文本> 只检测不处置"""
        message = await self.normalize_event(event)
        group_key = str(message.group_key) if message else "global"
        snapshot = await self.config_service.resolved(group_key)
        payload = run_detector_test(
            str(text),
            sender_role=message.sender_role if message else GroupRole.MEMBER,
            sender_name=message.sender_name if message else None,
            teacher_names=[str(item) for item in snapshot.get_list("anti_fraud.teacher_names")],
            thresholds=self.config_service.fraud_config(snapshot).thresholds,
        )
        yield event.plain_result(render_test_result(payload))

    @filter.command("反诈", alias={"antifraud"})
    async def cmd_antifraud(self, event: AstrMessageEvent, action: str = ""):
        """用途：/反诈 <状态|开启|关闭>"""
        word = str(action or "").strip()
        toggle = parse_toggle(word)
        if toggle is not None:
            yield event.plain_result(await self._toggle_antifraud(event, toggle))
            return
        if word in {"状态", "status", ""}:
            message = await self.normalize_event(event)
            group_key = str(message.group_key) if message else "global"
            snapshot = await self.config_service.resolved(group_key)
            stats = await self.storage.stats_snapshot() if self.storage else {}
            yield event.plain_result(render_antifraud_status(snapshot, stats))
            return
        yield event.plain_result("用法：/反诈 状态 | /反诈 开启 | /反诈 关闭 | /反诈测试 <文本>")

    # ------------------------------------------------------------------
    # 管理指令（统一走平台适配层）
    # ------------------------------------------------------------------
    @filter.command("禁言", alias={"ban"})
    async def cmd_ban(self, event: AstrMessageEvent, target: str = "", minutes: int = 10):
        """用途：/禁言 <@成员|成员ID> [分钟]"""
        yield event.plain_result(await self._member_action(event, "mute", target, minutes=minutes))

    @filter.command("解禁", alias={"unban"})
    async def cmd_unban(self, event: AstrMessageEvent, target: str = ""):
        """用途：/解禁 <@成员|成员ID>"""
        yield event.plain_result(await self._member_action(event, "unmute", target))

    @filter.command("踢人", alias={"kick"})
    async def cmd_kick(self, event: AstrMessageEvent, target: str = ""):
        """用途：/踢人 <@成员|成员ID>"""
        yield event.plain_result(await self._member_action(event, "kick", target))

    async def _member_action(
        self,
        event: AstrMessageEvent,
        kind: str,
        target: str,
        *,
        minutes: int = 10,
    ) -> str:
        """统一成员管理指令入口：权限 → 能力 → 执行。"""
        message = await self.normalize_event(event)
        if message is None:
            return "该指令只能在群聊中使用。"
        permission = await self.permission_for(message, event)
        if not permission.can_manage_group():
            return f"没有权限执行该操作（当前身份：{permission.level_label()}）。"
        user_id = parse_target([target]) if target else None
        if not user_id:
            return "请使用 /禁言 @成员 10 或 /禁言 123456789 10 的形式指定成员。"
        adapter = self.registry.create(message.platform)
        if adapter is None:
            return f"当前平台（{message.platform}）暂不支持群管理操作。"
        action_type = {
            "mute": GuardianActionType.MUTE,
            "unmute": GuardianActionType.UNMUTE,
            "kick": GuardianActionType.KICK,
        }[kind]
        engine = ActionEngine(adapter, storage=self.storage, notifier=self.notifier)
        outcome = await engine.execute(
            message,
            [
                GuardianAction(
                    action=action_type,
                    duration=parse_duration_minutes([str(minutes)], 10) * 60,
                    reason="群管理指令",
                    target_id=user_id,
                )
            ],
            bot_role=await self.bot_role_for(message),
            origin="command",
        )
        return format_action_results(action_type, outcome.results)

    # ------------------------------------------------------------------
    # LLM 工具（每次执行都做后端权限校验）
    # ------------------------------------------------------------------
    @filter.llm_tool(name="antifraud_test_text")
    async def tool_antifraud_test(self, event: AstrMessageEvent, text: str):
        """检测一段文本是否存在家校群虚假缴费诈骗风险。当群管理员询问某条消息是否为诈骗时使用。

        Args:
            text(string): 需要检测的文本内容
        """
        message = await self.normalize_event(event)
        group_key = str(message.group_key) if message else "global"
        snapshot = await self.config_service.resolved(group_key)
        payload = run_detector_test(
            str(text),
            sender_role=message.sender_role if message else GroupRole.MEMBER,
            teacher_names=[str(item) for item in snapshot.get_list("anti_fraud.teacher_names")],
            thresholds=self.config_service.fraud_config(snapshot).thresholds,
        )
        yield event.plain_result(render_test_result(payload))

    @filter.llm_tool(name="ban_group_member")
    async def tool_ban_member(
        self, event: AstrMessageEvent, user_id: str, duration_minutes: int = 10
    ):
        """禁言群成员。当管理员要求禁言某人时使用。

        Args:
            user_id(string): 要禁言的成员 ID
            duration_minutes(number): 禁言时长（分钟），默认 10
        """
        text = await execute_management_tool(
            self,
            event,
            "ban_group_member",
            {"user_id": user_id},
            user_id=user_id,
            duration_minutes=int(duration_minutes),
        )
        yield event.plain_result(text)

    @filter.llm_tool(name="unban_group_member")
    async def tool_unban_member(self, event: AstrMessageEvent, user_id: str):
        """解除群成员禁言。当管理员要求解禁某人时使用。

        Args:
            user_id(string): 要解禁的成员 ID
        """
        text = await execute_management_tool(
            self, event, "unban_group_member", {"user_id": user_id}, user_id=user_id
        )
        yield event.plain_result(text)

    @filter.llm_tool(name="kick_group_member")
    async def tool_kick_member(self, event: AstrMessageEvent, user_id: str):
        """踢出群成员。当管理员要求移出某人时使用。

        Args:
            user_id(string): 要踢出的成员 ID
        """
        text = await execute_management_tool(
            self, event, "kick_group_member", {"user_id": user_id}, user_id=user_id
        )
        yield event.plain_result(text)

    @filter.llm_tool(name="recall_group_message")
    async def tool_recall_message(self, event: AstrMessageEvent, message_id: str):
        """撤回指定消息。当管理员要求撤回某条消息时使用。

        Args:
            message_id(string): 要撤回的消息 ID
        """
        text = await execute_management_tool(
            self, event, "recall_message", {"message_id": message_id}
        )
        yield event.plain_result(text)


__all__ = ["CampusGroupGuardian", "PLUGIN_NAME", "PLUGIN_VERSION"]
