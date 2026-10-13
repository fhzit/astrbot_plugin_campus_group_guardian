# -*- coding: utf-8 -*-
"""SQLite 持久化层（§124-§133）。

- 全部异步（aiosqlite），不阻塞事件循环；
- 数据库放在插件数据目录（AstrBot data/plugin_data/<plugin>/），
  更新插件不会清空数据；
- 迁移带 schema_version、迁移前自动备份、失败回滚；
- 保存最少必要证据，不长期保存原始付款图片与完整聊天记录。
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import aiosqlite
from astrbot.api import logger

from ..core.antifraud.models import AntiFraudIncident, FraudResult, IncidentStatus
from ..core.models import GuardianMessage
from .migrations import MIGRATIONS, SCHEMA_VERSION

_LOG = "[Guardian][Storage]"

#: 消息摘要长度上限：只留必要证据（§132）。
EXCERPT_LIMIT = 300


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _excerpt(text: str, limit: int = EXCERPT_LIMIT) -> str:
    value = " ".join(str(text or "").split())
    return value[:limit]


class GuardianStorage:
    """插件数据库。"""

    def __init__(self, data_dir: Path, *, legacy_dir: Path | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "campus_group_guardian.db"
        self.backup_dir = self.data_dir / "backups"
        self.legacy_dir = Path(legacy_dir) if legacy_dir else None
        self._conn: aiosqlite.Connection | None = None
        self.migration_report: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """连接数据库并执行迁移。"""
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("数据库尚未初始化")
        return self._conn

    # ------------------------------------------------------------------
    # 迁移
    # ------------------------------------------------------------------
    async def _applied_versions(self) -> set[int]:
        try:
            cursor = await self.connection.execute("SELECT version FROM schema_migrations")
            rows = await cursor.fetchall()
            await cursor.close()
            return {int(row[0]) for row in rows}
        except aiosqlite.Error:
            return set()

    async def _migrate(self) -> None:
        applied = await self._applied_versions()
        pending = [migration for migration in MIGRATIONS if migration.version not in applied]
        if not pending:
            self.migration_report = {"applied": [], "backup": None, "schema_version": SCHEMA_VERSION}
            return

        backup_path = await self._backup(reason=f"pre_migration_v{pending[0].version}")
        applied_now: list[int] = []
        for migration in pending:
            try:
                await self.connection.execute("BEGIN")
                for statement in migration.statements:
                    await self.connection.execute(statement)
                await self.connection.execute(
                    "INSERT OR REPLACE INTO schema_migrations (version, description, applied_at) VALUES (?, ?, ?)",
                    (migration.version, migration.description, _now()),
                )
                await self.connection.commit()
                applied_now.append(migration.version)
                logger.info(f"{_LOG} 迁移 v{migration.version} 完成：{migration.description}")
            except aiosqlite.Error as exc:
                await self.connection.rollback()
                logger.error(f"{_LOG} 迁移 v{migration.version} 失败并已回滚：{exc}")
                break
        self.migration_report = {
            "applied": applied_now,
            "backup": str(backup_path) if backup_path else None,
            "schema_version": SCHEMA_VERSION,
        }

    async def _backup(self, *, reason: str) -> Path | None:
        """迁移前备份当前数据库。备份失败视为迁移前置条件未满足。"""
        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        target = self.backup_dir / f"{self.db_path.stem}-{reason}-{int(time.time())}.db"
        try:
            await self.connection.execute("VACUUM INTO ?", (str(target),))
            logger.info(f"{_LOG} 迁移前备份完成：{target.name}")
            return target
        except aiosqlite.Error as exc:
            logger.warning(f"{_LOG} VACUUM INTO 备份失败，退回文件复制：{exc}")
            try:
                shutil.copy2(self.db_path, target)
                return target
            except OSError as copy_exc:
                raise RuntimeError(f"数据库备份失败，已中止迁移：{copy_exc}") from copy_exc

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    async def record_action(
        self,
        *,
        message: GuardianMessage,
        action: str,
        status: str,
        error: str | None = None,
        message_text: str | None = None,
        origin: str = "auto",
        target_id: str | None = None,
    ) -> None:
        await self.connection.execute(
            """
            INSERT INTO action_logs
            (platform, adapter_id, group_id, message_id, target_id, action, status, error,
             message, origin, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.platform,
                message.adapter_id,
                message.group_id,
                message.message_id,
                target_id or message.sender_id,
                action,
                status,
                error,
                message_text,
                origin,
                _now(),
            ),
        )
        await self.connection.commit()

    async def record_moderation_log(
        self,
        *,
        message: GuardianMessage,
        category: str | None,
        reason: str,
        matched: str = "",
        recalled: bool = False,
    ) -> None:
        await self.connection.execute(
            """
            INSERT INTO moderation_logs
            (platform, adapter_id, group_id, message_id, sender_id, sender_name, sender_role,
             category, reason, excerpt, matched, recalled, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.platform,
                message.adapter_id,
                message.group_id,
                message.message_id,
                message.sender_id,
                message.sender_name,
                message.sender_role.value,
                category,
                reason,
                _excerpt(message.text),
                matched,
                1 if recalled else 0,
                _now(),
            ),
        )
        await self.connection.commit()

    async def create_incident(
        self,
        *,
        message: GuardianMessage,
        result: FraudResult,
        actions: Iterable[dict[str, Any]] = (),
        status: IncidentStatus = IncidentStatus.OPEN,
    ) -> AntiFraudIncident:
        """建立反诈事件（只保存摘要，不保存原始图片）。"""
        incident = AntiFraudIncident(
            platform=message.platform,
            adapter_id=message.adapter_id,
            group_id=message.group_id or "",
            message_id=message.message_id,
            sender_id=message.sender_id,
            sender_name=message.sender_name,
            sender_role=message.sender_role,
            risk_score=result.score,
            risk_level=result.level,
            message_excerpt=_excerpt(message.text),
            ocr_excerpt=_excerpt(result.ocr_text),
            signals=list(result.signals),
            status=status,
            created_at=datetime.now(timezone.utc),
            actions=[dict(item) for item in actions],
        )
        cursor = await self.connection.execute(
            """
            INSERT INTO anti_fraud_incidents
            (platform, adapter_id, group_id, message_id, sender_id, sender_name, sender_role,
             risk_score, risk_level, message_excerpt, ocr_excerpt, status, actions_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                incident.platform,
                incident.adapter_id,
                incident.group_id,
                incident.message_id,
                incident.sender_id,
                incident.sender_name,
                incident.sender_role.value,
                incident.risk_score,
                incident.risk_level.value,
                incident.message_excerpt,
                incident.ocr_excerpt,
                incident.status.value,
                json.dumps(incident.actions, ensure_ascii=False),
                incident.created_at.isoformat(),
            ),
        )
        incident_id = cursor.lastrowid
        await cursor.close()
        for signal in incident.signals:
            await self.connection.execute(
                """
                INSERT INTO anti_fraud_signals
                (incident_id, signal_type, label, evidence, source, weight)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    incident_id,
                    signal.type.value,
                    signal.type.label,
                    signal.evidence,
                    signal.source.value,
                    signal.weight,
                ),
            )
        await self.connection.commit()
        incident.incident_id = incident_id
        return incident

    async def update_incident_status(
        self,
        incident_id: int,
        status: IncidentStatus | str,
        *,
        resolved_by: str | None = None,
    ) -> bool:
        """更新事件状态（人工复核：确认诈骗 / 标记误判 / 已处理）。"""
        if not isinstance(status, IncidentStatus):
            status = IncidentStatus(str(status))
        cursor = await self.connection.execute(
            """
            UPDATE anti_fraud_incidents
            SET status = ?, resolved_at = ?, resolved_by = ?
            WHERE id = ?
            """,
            (status.value, _now(), resolved_by, incident_id),
        )
        await self.connection.commit()
        updated = cursor.rowcount or 0
        await cursor.close()
        return updated > 0

    async def set_group_override(self, group_key: str, config: dict[str, Any]) -> None:
        platform, adapter_id, group_id = (group_key.split(":", 2) + ["", ""])[:3]
        await self.connection.execute(
            """
            INSERT INTO group_config_overrides (platform, adapter_id, group_id, config_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (platform, adapter_id, group_id)
            DO UPDATE SET config_json = excluded.config_json, updated_at = excluded.updated_at
            """,
            (platform, adapter_id, group_id, json.dumps(config, ensure_ascii=False), _now()),
        )
        await self.connection.commit()

    async def add_trusted_operator(
        self,
        *,
        platform: str,
        adapter_id: str,
        user_id: str,
        group_id: str | None = None,
        note: str = "",
    ) -> None:
        await self.connection.execute(
            """
            INSERT OR REPLACE INTO trusted_operators
            (platform, adapter_id, group_id, user_id, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (platform, adapter_id, group_id, user_id, note, _now()),
        )
        await self.connection.commit()

    async def schedule_unmute(
        self,
        *,
        message: GuardianMessage,
        user_id: str,
        due_at: datetime,
    ) -> int:
        cursor = await self.connection.execute(
            """
            INSERT INTO scheduled_unmutes
            (platform, adapter_id, group_id, user_id, session, due_at, created_at, done)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                message.platform,
                message.adapter_id,
                message.group_id or "",
                user_id,
                message.unified_msg_origin,
                due_at.isoformat(),
                _now(),
            ),
        )
        await self.connection.commit()
        return cursor.lastrowid or 0

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    async def list_incidents(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
        group_key: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM anti_fraud_incidents"
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if group_key:
            platform, adapter_id, group_id = (group_key.split(":", 2) + ["", ""])[:3]
            clauses.append("platform = ? AND adapter_id = ? AND group_id = ?")
            params.extend([platform, adapter_id, group_id])
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        cursor = await self.connection.execute(query, params)
        rows = await cursor.fetchall()
        await cursor.close()
        incidents = []
        for row in rows:
            item = dict(row)
            try:
                item["actions"] = json.loads(item.pop("actions_json") or "[]")
            except ValueError:
                item["actions"] = []
            item["signals"] = await self._signals_for(item["id"])
            incidents.append(item)
        return incidents

    async def _signals_for(self, incident_id: int) -> list[dict[str, Any]]:
        cursor = await self.connection.execute(
            "SELECT signal_type, label, evidence, source, weight FROM anti_fraud_signals WHERE incident_id = ?",
            (incident_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def get_group_override(self, group_key: str) -> dict[str, Any]:
        platform, adapter_id, group_id = (group_key.split(":", 2) + ["", ""])[:3]
        cursor = await self.connection.execute(
            """
            SELECT config_json FROM group_config_overrides
            WHERE platform = ? AND adapter_id = ? AND group_id = ?
            """,
            (platform, adapter_id, group_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return {}
        try:
            return json.loads(row["config_json"])
        except ValueError:
            return {}

    async def backup_now(self, *, reason: str = "manual") -> Path | None:
        """对外暴露的备份入口（迁移、人工操作前调用）。"""
        return await self._backup(reason=reason)

    async def delete_group_override(self, group_key: str) -> None:
        platform, adapter_id, group_id = (group_key.split(":", 2) + ["", ""])[:3]
        await self.connection.execute(
            """
            DELETE FROM group_config_overrides
            WHERE platform = ? AND adapter_id = ? AND group_id = ?
            """,
            (platform, adapter_id, group_id),
        )
        await self.connection.commit()

    async def list_action_logs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        cursor = await self.connection.execute(
            "SELECT * FROM action_logs ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def list_trusted_operators(self) -> list[dict[str, Any]]:
        cursor = await self.connection.execute(
            "SELECT platform, adapter_id, group_id, user_id, note, created_at FROM trusted_operators ORDER BY id DESC"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def due_unmutes(self, *, limit: int = 50) -> list[dict[str, Any]]:
        cursor = await self.connection.execute(
            """
            SELECT * FROM scheduled_unmutes
            WHERE done = 0 AND due_at <= ?
            ORDER BY due_at ASC LIMIT ?
            """,
            (_now(), max(1, int(limit))),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def mark_unmute_done(self, record_id: int) -> None:
        await self.connection.execute("UPDATE scheduled_unmutes SET done = 1 WHERE id = ?", (record_id,))
        await self.connection.commit()

    async def add_scheduled_unmute(
        self,
        *,
        message: GuardianMessage,
        user_id: str,
        due_at: str,
        duration_seconds: int = 0,
    ) -> None:
        """登记一条到点自动解禁任务（§99、§187）。

        Args:
            message: 触发处罚的消息（用于取平台、会话与群号）。
            user_id: 被禁言成员。
            due_at: 到期时间（ISO8601 字符串）。
            duration_seconds: 禁言时长，仅作留痕。
        """
        await self.connection.execute(
            """
            INSERT INTO scheduled_unmutes
            (platform, adapter_id, group_id, user_id, session, due_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.platform,
                message.adapter_id,
                message.group_id or "",
                str(user_id),
                message.unified_msg_origin,
                str(due_at),
                _now(),
            ),
        )
        await self.connection.commit()
        logger.info(
            f"{_LOG} 已登记定时解禁：群={message.group_id} 成员={user_id} "
            f"时长={duration_seconds}s 到期={due_at}"
        )

    async def list_moderation_logs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """最近的内容审核记录。"""
        cursor = await self.connection.execute(
            "SELECT * FROM moderation_logs ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def stats_snapshot(self) -> dict[str, int]:
        """总览页统计。"""
        result: dict[str, int] = {}
        queries = {
            "moderation_scanned": "SELECT COUNT(*) FROM moderation_logs",
            "moderation_violations": "SELECT COUNT(*) FROM moderation_logs WHERE recalled = 1",
            "action_total": "SELECT COUNT(*) FROM action_logs",
            "action_success": "SELECT COUNT(*) FROM action_logs WHERE status = 'success'",
            "action_unsupported": "SELECT COUNT(*) FROM action_logs WHERE status = 'unsupported'",
            "action_failed": "SELECT COUNT(*) FROM action_logs WHERE status = 'failed'",
            "incidents_total": "SELECT COUNT(*) FROM anti_fraud_incidents",
            "incidents_open": "SELECT COUNT(*) FROM anti_fraud_incidents WHERE status = 'open'",
            "incidents_confirmed": "SELECT COUNT(*) FROM anti_fraud_incidents WHERE status = 'confirmed_fraud'",
            "incidents_false_positive": "SELECT COUNT(*) FROM anti_fraud_incidents WHERE status = 'false_positive'",
        }
        for key, query in queries.items():
            cursor = await self.connection.execute(query)
            row = await cursor.fetchone()
            await cursor.close()
            result[key] = int(row[0]) if row else 0
        return result

    async def recent_incident_counts(self, *, days: int = 7) -> list[dict[str, Any]]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cursor = await self.connection.execute(
            """
            SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS total,
                   SUM(CASE WHEN risk_level = 'critical' THEN 1 ELSE 0 END) AS critical
            FROM anti_fraud_incidents
            WHERE created_at >= ?
            GROUP BY day ORDER BY day ASC
            """,
            (since,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 数据保留（§133）
    # ------------------------------------------------------------------
    async def cleanup(self, *, violation_days: int = 90, incident_days: int = 180, action_days: int = 30) -> dict[str, int]:
        """按保留策略清理历史数据。"""
        removed: dict[str, int] = {}
        now = datetime.now(timezone.utc)
        targets = (
            ("moderation_logs", violation_days),
            ("violations", violation_days),
            ("anti_fraud_incidents", incident_days),
            ("action_logs", action_days),
            ("scheduled_unmutes", action_days),
        )
        for table, days in targets:
            if days <= 0:
                removed[table] = 0
                continue
            cutoff = (now - timedelta(days=days)).isoformat()
            time_column = "due_at" if table == "scheduled_unmutes" else "created_at"
            where = f"{time_column} < ?"
            if table == "scheduled_unmutes":
                where = "done = 1 AND due_at < ?"
            cursor = await self.connection.execute(f"DELETE FROM {table} WHERE {where}", (cutoff,))
            removed[table] = cursor.rowcount or 0
            await cursor.close()
        await self.connection.commit()
        return removed

    # ------------------------------------------------------------------
    # 旧版数据迁移助手（§127、§128、§190）
    # ------------------------------------------------------------------
    def detect_legacy_data(self) -> dict[str, Any]:
        """检测原版 GroupGuardian 的数据文件。"""
        report: dict[str, Any] = {
            "legacy_dir": str(self.legacy_dir) if self.legacy_dir else None,
            "database": None,
            "lexicon": None,
            "json_logs": None,
            "detected": False,
        }
        if not self.legacy_dir or not self.legacy_dir.exists():
            return report
        candidates = {
            "database": ["group_guardian.db"],
            "lexicon": ["lexicon.db"],
            "json_logs": ["moderation_logs.json"],
        }
        for key, names in candidates.items():
            for name in names:
                path = self.legacy_dir / name
                if path.exists():
                    report[key] = {
                        "path": str(path),
                        "size": path.stat().st_size,
                    }
                    report["detected"] = True
        return report

    async def import_legacy_logs(self, source_db: Path, *, limit: int = 5000) -> int:
        """把原版 moderation_logs 表的记录导入新库（列名不同的字段留空）。

        Args:
            source_db: 原版 group_guardian.db 路径。
            limit: 单次最多导入条数，避免一次性写入过大。

        Returns:
            实际导入条数。
        """
        source_db = Path(source_db)
        if not source_db.exists():
            return 0
        imported = 0
        legacy = await aiosqlite.connect(f"file:{source_db}?mode=ro", uri=True)
        legacy.row_factory = aiosqlite.Row
        try:
            cursor = await legacy.execute("SELECT * FROM moderation_logs ORDER BY id DESC LIMIT ?", (limit,))
            rows = await cursor.fetchall()
            await cursor.close()
        except aiosqlite.Error as exc:
            logger.warning(f"{_LOG} 旧库 moderation_logs 读取失败（可能结构不同）：{exc}")
            await legacy.close()
            return 0
        for row in rows:
            data = dict(row)
            await self.connection.execute(
                """
                INSERT INTO moderation_logs
                (platform, adapter_id, group_id, message_id, sender_id, sender_name, sender_role,
                 category, reason, excerpt, matched, recalled, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(data.get("platform", "aiocqhttp")),
                    str(data.get("adapter_id", "legacy")),
                    str(data.get("group_id", "")),
                    str(data.get("message_id", "")),
                    str(data.get("user_id") or data.get("sender_id") or ""),
                    data.get("sender_name"),
                    str(data.get("sender_role", "unknown")),
                    data.get("category"),
                    data.get("reason"),
                    _excerpt(data.get("content") or data.get("message") or ""),
                    str(data.get("rule") or data.get("matched") or ""),
                    1 if data.get("recalled") else 0,
                    str(data.get("created_at") or _now()),
                ),
            )
            imported += 1
        await self.connection.commit()
        await legacy.close()
        logger.info(f"{_LOG} 旧库日志导入完成：{imported} 条")
        return imported
