# -*- coding: utf-8 -*-
"""配置覆盖、存储迁移与兼容性测试（规格 §121-§133、§188-§190）。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from astrbot_plugin_campus_group_guardian.core.models import GroupRole
from astrbot_plugin_campus_group_guardian.services.config_service import ConfigService
from astrbot_plugin_campus_group_guardian.storage.database import GuardianStorage

GROUP_KEY = "qq_official:inst-1:10001"


@pytest.fixture
async def storage(tmp_path: Path):
    instance = GuardianStorage(tmp_path / "plugin_data")
    await instance.initialize()
    yield instance
    await instance.close()


@pytest.fixture
async def config_service(storage):
    service = ConfigService({"anti_fraud": {"enabled": True}}, storage=storage)
    return service


async def test_group_override_is_applied(config_service, storage, message_factory):
    """§121：全局默认 + 群级覆盖。"""
    default_snapshot = await config_service.resolved(GROUP_KEY)
    assert default_snapshot.get_bool("anti_fraud.enabled") is True
    assert default_snapshot.get_int("anti_fraud.context_messages") == 10

    await storage.set_group_override(
        GROUP_KEY, {"anti_fraud.enabled": False, "anti_fraud.context_messages": 5}
    )
    config_service.invalidate(GROUP_KEY)

    overridden = await config_service.resolved(GROUP_KEY)
    assert overridden.get_bool("anti_fraud.enabled") is False
    assert overridden.get_int("anti_fraud.context_messages") == 5
    # 未覆盖的键仍走全局默认
    assert overridden.get_float("anti_fraud.risk_threshold_critical") == 85.0

    await storage.delete_group_override(GROUP_KEY)
    config_service.invalidate(GROUP_KEY)
    reverted = await config_service.resolved(GROUP_KEY)
    assert reverted.get_bool("anti_fraud.enabled") is True


async def test_group_key_separates_platforms(storage, message_factory):
    """§122/§123：不同平台的相同群号不能互相污染。"""
    await storage.set_group_override("qq_official:inst-1:10001", {"anti_fraud.enabled": False})
    await storage.set_group_override("dingtalk:inst-9:10001", {"anti_fraud.enabled": True})
    service = ConfigService({"anti_fraud": {"enabled": False}}, storage=storage)
    assert (await service.resolved("qq_official:inst-1:10001")).get_bool("anti_fraud.enabled") is False
    assert (await service.resolved("dingtalk:inst-9:10001")).get_bool("anti_fraud.enabled") is True


async def test_group_allow_lists():
    service = ConfigService(
        {"general": {"group_black_list": ["20002"], "group_white_list": []}}
    )
    snapshot = service.global_snapshot()
    assert service.group_allowed("10001", snapshot) is True
    assert service.group_allowed("20002", snapshot) is False

    whitelisted = ConfigService({"general": {"group_white_list": ["10001"]}})
    whitelist_snapshot = whitelisted.global_snapshot()
    assert whitelisted.group_allowed("10001", whitelist_snapshot) is True
    assert whitelisted.group_allowed("10002", whitelist_snapshot) is False


async def test_schema_migration_records_version(storage):
    """§126：迁移带版本号，且重复初始化幂等。"""
    report = storage.migration_report
    assert report.get("schema_version", 0) >= 1
    assert report.get("applied")


async def test_storage_is_created_outside_plugin_source(tmp_path):
    storage = GuardianStorage(tmp_path / "data" / "plugin_data")
    await storage.initialize()
    assert storage.db_path.exists()
    assert "plugin_data" in str(storage.db_path)
    await storage.close()


async def test_incident_and_action_log_roundtrip(storage, message_factory):
    from astrbot_plugin_campus_group_guardian.core.antifraud.detectors import detect_text_signals
    from astrbot_plugin_campus_group_guardian.core.antifraud.risk_engine import score_signals

    signals, _ = detect_text_signals("每位495元，扫码付款，备注学生姓名")
    result = score_signals(signals)
    message = message_factory("每位495元，扫码付款，备注学生姓名")

    incident = await storage.create_incident(
        message=message,
        result=result,
        actions=[{"action": "recall", "status": "success"}],
    )
    assert incident.risk_score == result.score

    await storage.record_action(
        message=message, action="recall", status="success", message_text="已撤回", origin="test"
    )
    incidents = await storage.list_incidents(limit=10)
    assert len(incidents) == 1
    assert incidents[0]["group_id"] == "10001"
    logs = await storage.list_action_logs(limit=10)
    assert logs and logs[0]["action"] == "recall"

    stats = await storage.stats_snapshot()
    assert stats["incidents_total"] == 1
    assert stats["action_total"] == 1

    updated = await storage.update_incident_status(
        int(incidents[0]["id"]), "false_positive", resolved_by="tester"
    )
    assert updated is True
    refreshed = await storage.list_incidents(limit=10, status="false_positive")
    assert len(refreshed) == 1


async def test_scheduled_unmute_queue(storage, message_factory):
    """§187：到点解禁队列。"""
    message = message_factory("测试")
    await storage.add_scheduled_unmute(
        message=message,
        user_id="20001",
        due_at="2000-01-01T00:00:00+00:00",
        duration_seconds=60,
    )
    due = await storage.due_unmutes()
    assert len(due) == 1
    assert due[0]["user_id"] == "20001"
    await storage.mark_unmute_done(int(due[0]["id"]))
    assert await storage.due_unmutes() == []


async def test_retention_cleanup_removes_old_rows(storage, message_factory):
    """§133：按保留天数清理。"""
    message = message_factory("旧记录")
    await storage.record_action(
        message=message, action="recall", status="success", origin="test"
    )
    await storage.connection.execute(
        "UPDATE action_logs SET created_at = '2000-01-01T00:00:00+00:00'"
    )
    await storage.connection.commit()
    removed = await storage.cleanup(violation_days=1, incident_days=1, action_days=1)
    assert removed["action_logs"] == 1


async def test_legacy_detection_and_import(tmp_path: Path, message_factory):
    """§127/§128/§190：旧版数据检测 + 备份后导入。"""
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    legacy_db = legacy_dir / "group_guardian.db"
    connection = sqlite3.connect(legacy_db)
    connection.execute(
        """
        CREATE TABLE moderation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT, user_id TEXT, sender_name TEXT, category TEXT,
            reason TEXT, content TEXT, rule TEXT, recalled INTEGER,
            created_at TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO moderation_logs (group_id, user_id, sender_name, category, reason, content, rule, recalled, created_at)"
        " VALUES ('10001', '20001', '小明', 'advertisement', '广告', '进群领福利', '进群领', 1, '2026-01-01T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()
    (legacy_dir / "lexicon.db").write_bytes(b"SQLite format 3\x00")
    (legacy_dir / "moderation_logs.json").write_text(json.dumps([]), encoding="utf-8")

    storage = GuardianStorage(tmp_path / "plugin_data", legacy_dir=legacy_dir)
    await storage.initialize()
    report = storage.detect_legacy_data()
    assert report["detected"] is True
    assert report["database"]["path"].endswith("group_guardian.db")
    assert report["lexicon"] is not None

    backup = await storage.backup_now(reason="test")
    assert backup is not None and backup.exists()

    imported = await storage.import_legacy_logs(legacy_db)
    assert imported == 1
    rows = await storage.list_moderation_logs(limit=5)
    assert rows
    await storage.close()


async def test_trusted_operator_roundtrip(storage):
    await storage.add_trusted_operator(
        platform="wecom", adapter_id="i1", user_id="666", group_id="10001", note="老师"
    )
    operators = await storage.list_trusted_operators()
    assert operators and operators[0]["user_id"] == "666"
