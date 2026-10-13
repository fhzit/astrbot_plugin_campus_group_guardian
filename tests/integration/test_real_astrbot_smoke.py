# -*- coding: utf-8 -*-
"""对真实 AstrBot 的冒烟测试（可选）。

默认跳过；当环境提供 AstrBot 源码时（环境变量 ``ASTRBOT_SRC`` 指向源码目录，
或 ``astrbot`` 已安装）才运行，用来验证插件使用的 AstrBot API 在真实版本中确实存在：

- ``astrbot.api.event.filter`` 的 command / llm_tool / event_message_type / platform_adapter_type
- ``astrbot.api.star`` 的 Star / Context / StarTools / register
- ``astrbot.api.web`` 的 json_response / error_response / request
- 插件 main.py 能在真实 AstrBot 下导入并完成注册

这样可以在升级 AstrBot 后第一时间发现 API 破坏性变更。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ASTRBOT_SRC = os.environ.get("ASTRBOT_SRC")
if ASTRBOT_SRC and Path(ASTRBOT_SRC).exists() and ASTRBOT_SRC not in sys.path:
    sys.path.insert(0, ASTRBOT_SRC)


def _real_astrbot_importable() -> bool:
    """只要 tests/stubs 提供了 astrbot 桩，就视为「没有真实 AstrBot」。"""
    try:
        import astrbot
    except Exception:
        return False
    origin = str(getattr(astrbot, "__file__", "") or "").replace("\\", "/")
    if not origin or "/tests/stubs/" in origin:
        return False
    try:
        import astrbot.api.event  # noqa: F401
    except Exception:
        return False
    return True


REAL_ASTRBOT = _real_astrbot_importable()

requires_real_astrbot = pytest.mark.skipif(
    not REAL_ASTRBOT,
    reason="未提供真实 AstrBot（设置 ASTRBOT_SRC 指向源码目录或安装 astrbot）",
)


@requires_real_astrbot
def test_plugin_can_be_imported_under_real_astrbot():
    """在真实 AstrBot 环境下导入插件（不使用任何桩）。"""
    stubs = str(Path(__file__).resolve().parents[1] / "stubs")
    original = list(sys.path)
    try:
        # 只在本次测试内临时移除桩包路径，避免影响其它测试（botpy 桩只在 stubs 下）
        sys.path[:] = [item for item in original if item != stubs]
        module = importlib.import_module("astrbot_plugin_campus_group_guardian.main")
        assert module.PLUGIN_NAME == "astrbot_plugin_campus_group_guardian"

        from astrbot.api.event.filter import PlatformAdapterType

        for flag in (
            PlatformAdapterType.AIOCQHTTP,
            PlatformAdapterType.QQOFFICIAL,
            PlatformAdapterType.WECOM,
            PlatformAdapterType.DINGTALK,
        ):
            assert module.PLATFORM_FILTER & flag
    finally:
        sys.path[:] = original


@requires_real_astrbot
def test_registered_handlers_exist_under_real_astrbot():
    """注册装饰器在真实 AstrBot 下生效（handler 被登记到注册表）。"""
    from astrbot.core.star.star_handler import star_handlers_registry

    names = [getattr(handler, "handler_name", "") for handler in star_handlers_registry]
    assert any("on_group_message" in name for name in names), names[:20]


def test_config_schema_is_loadable():
    """_conf_schema.json 能被正常解析，且每个分组的每项都带 description。"""
    import json

    schema_path = Path(__file__).resolve().parents[2] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    for group, group_meta in schema.items():
        assert group_meta.get("type") == "object", group
        assert group_meta.get("description"), group
        for key, item in group_meta.get("items", {}).items():
            assert "description" in item, f"{group}.{key}"
            assert "type" in item, f"{group}.{key}"
            assert "default" in item, f"{group}.{key}"
