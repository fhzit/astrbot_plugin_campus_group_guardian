# -*- coding: utf-8 -*-
"""数据库迁移定义（§126）。

每次改表都追加一条 Migration，禁止在启动逻辑里散落 ALTER TABLE。
迁移前自动备份，迁移失败回滚。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    """一次 schema 迁移。"""

    version: int
    description: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        description="初始化：审核日志、违规记录、处罚、动作日志、群级覆盖、可信操作者、定时解禁",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS moderation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                adapter_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                sender_name TEXT,
                sender_role TEXT NOT NULL,
                category TEXT,
                reason TEXT,
                excerpt TEXT,
                matched TEXT,
                recalled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_moderation_logs_group_time
            ON moderation_logs (platform, adapter_id, group_id, created_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS violations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                adapter_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                category TEXT,
                score REAL NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'moderation',
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_violations_user
            ON violations (platform, adapter_id, group_id, user_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS punishments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                adapter_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                duration INTEGER NOT NULL DEFAULT 0,
                reason TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS action_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                adapter_id TEXT NOT NULL,
                group_id TEXT,
                message_id TEXT,
                target_id TEXT,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                message TEXT,
                origin TEXT NOT NULL DEFAULT 'auto',
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_action_logs_time ON action_logs (created_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS group_config_overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                adapter_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                config_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (platform, adapter_id, group_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS trusted_operators (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                adapter_id TEXT NOT NULL,
                group_id TEXT,
                user_id TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (platform, adapter_id, group_id, user_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS scheduled_unmutes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                adapter_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                session TEXT NOT NULL,
                due_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_scheduled_unmutes_due
            ON scheduled_unmutes (done, due_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_capability_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                adapter_id TEXT NOT NULL,
                capabilities_json TEXT NOT NULL,
                limits_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (platform, adapter_id)
            )
            """,
        ),
    ),
    Migration(
        version=2,
        description="反诈模块：事件表与信号表（§129、§130）",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS anti_fraud_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                adapter_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                sender_name TEXT,
                sender_role TEXT NOT NULL,
                risk_score REAL NOT NULL DEFAULT 0,
                risk_level TEXT NOT NULL,
                message_excerpt TEXT,
                ocr_excerpt TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                actions_json TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                resolved_by TEXT
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_incidents_group_time
            ON anti_fraud_incidents (platform, adapter_id, group_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_incidents_status
            ON anti_fraud_incidents (status, created_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS anti_fraud_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id INTEGER NOT NULL,
                signal_type TEXT NOT NULL,
                label TEXT,
                evidence TEXT,
                source TEXT,
                weight REAL NOT NULL DEFAULT 0,
                FOREIGN KEY (incident_id) REFERENCES anti_fraud_incidents (id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_signals_incident ON anti_fraud_signals (incident_id)
            """,
        ),
    ),
)

#: 当前 schema 版本。
SCHEMA_VERSION = MIGRATIONS[-1].version
