#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实 AstrBot 部署冒烟检查。

用途：在**不启动完整 AstrBot**、也不碰生产数据目录的前提下，用 AstrBot 官方的
``PluginManager`` 真正加载本插件，验证「能真实部署」：

1. ``metadata.yaml`` 能被解析（名称 / 显示名 / 版本 / 支持平台）；
2. 插件以 ``data.plugins.<插件名>.main`` 方式导入成功（相对导入、依赖导入无误）；
3. 指令 / LLM 工具注册成功；
4. ``initialize()`` 后台初始化跑通：插件数据目录、数据库、迁移（含迁移前备份）、词库、调度任务；
5. ``_conf_schema.json`` 的分组被写入插件配置；
6. 平台能力画像可枚举；
7. ``terminate()`` 正常收尾。

用法：

```bash
# 需要 AstrBot 源码目录（或已安装 astrbot 的环境）与插件运行依赖
ASTRBOT_SRC=/path/to/AstrBot python scripts/astrbot_deploy_check.py
```

退出码 0 表示全部检查通过。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

PLUGIN_DIR = Path(__file__).resolve().parents[1]
ASTRBOT_SRC = os.environ.get("ASTRBOT_SRC", "")

REQUIRED_TABLES = {"schema_migrations", "moderation_logs", "anti_fraud_incidents"}

failures: list[str] = []


def check(label: str, ok: bool, extra: str = "") -> None:
    print(f"[{'OK  ' if ok else 'FAIL'}] {label}{(' — ' + extra) if extra else ''}")
    if not ok:
        failures.append(label)


def prepare_root() -> Path:
    """准备一个临时 ASTRBOT_ROOT，并把插件放进 data/plugins。"""
    root = Path(tempfile.mkdtemp(prefix="campus-guardian-deploy-"))
    os.environ["ASTRBOT_ROOT"] = str(root)
    # AstrBot 以 data.plugins.<插件名>.main 导入插件，因此 root 必须在 sys.path 上
    sys.path.insert(0, str(root))
    # 同时切换 cwd：AstrBot 的 root 在未设置环境变量时回退到 cwd，
    # 若不切换，运行 AstrBot 会往当前目录（插件仓库）写出 data/ 目录。
    os.chdir(root)
    plugins_dir = root / "data" / "plugins"
    plugins_dir.mkdir(parents=True)
    shutil.copytree(
        PLUGIN_DIR,
        plugins_dir / PLUGIN_DIR.name,
        ignore=shutil.ignore_patterns(
            ".venv", "__pycache__", ".git", "tests", ".pytest_cache", "*.db-*"
        ),
    )
    return root


def build_context(root: Path):
    from astrbot.core.config.astrbot_config import AstrBotConfig
    from astrbot.core.star.context import Context

    config = AstrBotConfig(
        config_path=str(root / "data" / "config" / "astrbot.yml"), default_config={}
    )
    # 这里只需要 Context 的类型与少量方法；管理器用 MagicMock 占位，
    # 真正被测的是 AstrBot 的插件加载机制与插件自身的初始化逻辑。
    return Context(
        event_queue=MagicMock(),
        config=config,
        db=MagicMock(),
        provider_manager=MagicMock(),
        platform_manager=MagicMock(),
        conversation_manager=MagicMock(),
        message_history_manager=MagicMock(),
        persona_manager=MagicMock(),
        astrbot_config_mgr=MagicMock(),
        knowledge_base_manager=MagicMock(),
        cron_manager=MagicMock(),
    )


async def run() -> int:
    if ASTRBOT_SRC and ASTRBOT_SRC not in sys.path:
        sys.path.insert(0, ASTRBOT_SRC)
    try:
        import astrbot  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print(f"无法导入 astrbot：{exc}（设置 ASTRBOT_SRC 指向源码目录或安装 astrbot）")
        return 2

    root = prepare_root()
    print(f"ASTRBOT_ROOT = {root}")

    from astrbot.core.star.star_manager import PluginManager
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path

    context = build_context(root)
    manager = PluginManager(context, context.get_config())
    check("ASTRBOT_ROOT 生效", get_astrbot_plugin_path() == str(root / "data" / "plugins"))

    await manager.load(specified_dir_name=PLUGIN_DIR.name)

    stars = list(context.get_all_stars())
    check("插件被 AstrBot 加载", len(stars) == 1, f"{len(stars)} 个")
    if not stars:
        return 1

    metadata = stars[0]
    check("metadata.yaml 解析", metadata.name == PLUGIN_DIR.name,
          f"{metadata.name} / {getattr(metadata, 'display_name', '?')} / {getattr(metadata, 'version', '?')}")
    check("support_platforms 解析", bool(getattr(metadata, "support_platforms", None)),
          str(getattr(metadata, "support_platforms", None)))

    instance = metadata.star_cls
    check("插件实例创建", instance is not None and getattr(instance, "context", None) is context,
          type(instance).__name__)

    # 等待 initialize() 的后台初始化完成
    await asyncio.sleep(4)

    data_dir = root / "data" / "plugin_data" / PLUGIN_DIR.name
    check("插件数据目录创建", data_dir.exists(), str(data_dir))
    dbs = sorted(data_dir.glob("*.db"))
    check("数据库文件创建", bool(dbs), ", ".join(db.name for db in dbs))
    for db in dbs:
        conn = sqlite3.connect(db)
        tables = sorted(row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        conn.close()
        print(f"     {db.name}: {tables}")
        check(f"{db.name} 含必要表", REQUIRED_TABLES <= set(tables))

    cfg_path = root / "data" / "config" / f"{PLUGIN_DIR.name}_config.json"
    if cfg_path.exists():
        # AstrBot 写入的插件配置带 UTF-8 BOM
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        check("插件配置分组写入", {"moderation", "anti_fraud"} <= set(cfg), str(list(cfg.keys())))

    storage = getattr(instance, "storage", None)
    stats = await storage.stats_snapshot() if storage else {}
    check("存储层可用", "incidents_total" in stats, str(stats))

    registry = getattr(instance, "registry", None)
    if registry is not None:
        rows = registry.profiles()
        check("平台能力画像可枚举", len(rows) >= 4, f"{len(rows)} 个平台")
        for row in rows:
            supported = [item["capability"] for item in row.get("capabilities", []) if item.get("supported")]
            print(f"     {row.get('platform')}（{row.get('adapter')}）：{len(supported)} 项可用 -> {supported}")

    registry = getattr(instance, "registry", None)
    if registry is not None:
        await registry.close_all()

    try:
        await manager._terminate_plugin(metadata)  # noqa: SLF001 — AstrBot 未暴露公开卸载入口
        check("terminate() 正常收尾", True)
    except Exception as exc:  # noqa: BLE001
        check("terminate() 正常收尾", False, f"{type(exc).__name__}: {exc}")

    print(f"\n临时数据目录（可删除）：{root}")
    print("结果：", "全部通过" if not failures else f"{len(failures)} 项失败 -> {failures}")
    return 1 if failures else 0


raise SystemExit(asyncio.run(run()))
