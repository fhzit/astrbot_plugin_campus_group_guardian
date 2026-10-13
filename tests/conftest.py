"""pytest 公共装置。

关键点：AstrBot 以 ``data.plugins.<插件目录名>.main`` 的方式加载插件，
插件内部使用的是相对导入；因此测试也用「包限定」方式导入插件，
（把插件目录的**父目录**加入 sys.path，再 ``import astrbot_plugin_campus_group_guardian.*``），
这样测试与生产加载方式一致，不会出现 “relative import beyond top-level package”。

若环境没有真正的 AstrBot，则使用 tests/stubs 下的桩包。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGIN_PARENT = PLUGIN_DIR.parent
STUBS = Path(__file__).resolve().parent / "stubs"


def _real_astrbot_available() -> bool:
    """真实 AstrBot 是否可用：找到的 ``astrbot`` 不能是 tests/stubs 里的桩。

    环境变量 ``ASTRBOT_SRC`` 可指向 AstrBot 源码目录（用于升级 AstrBot 后的 API 冒烟测试）。
    """
    src = os.environ.get("ASTRBOT_SRC")
    if src and Path(src).exists() and src not in sys.path:
        sys.path.insert(0, src)
    # 导入真实 AstrBot 会按其 root（未设置时回退到 cwd）写出 data/ 目录，
    # 这里指到一个临时目录，避免污染插件仓库。
    os.environ.setdefault("ASTRBOT_ROOT", tempfile.mkdtemp(prefix="gg-astrbot-root-"))
    try:
        module = importlib.import_module("astrbot")
    except Exception:
        return False
    origin = str(getattr(module, "__file__", "") or "").replace("\\", "/")
    return bool(origin) and "/tests/stubs/" not in origin


REAL_ASTRBOT = _real_astrbot_available()

# 桩包目录始终加入 sys.path：真实 AstrBot 存在时它提供 botpy 桩；
# 不存在时还要提供 astrbot 桩（因此必须排在真实 AstrBot 源码之后）。
sys.path.insert(0, str(STUBS))
if not REAL_ASTRBOT:
    os.environ.setdefault("GG_TEST_DATA_DIR", "/tmp/ggnext-test-data")
else:  # 真实 AstrBot 需要排在最前面
    src = os.environ.get("ASTRBOT_SRC", "")
    if src and Path(src).exists():
        sys.path.remove(src)
        sys.path.insert(0, src)

if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot.api.event import AstrMessageEvent  # noqa: E402

from astrbot_plugin_campus_group_guardian.core.models import (  # noqa: E402
    GroupRole,
    GuardianImage,
    GuardianMessage,
)
from astrbot_plugin_campus_group_guardian.platforms.base import (  # noqa: E402
    GuardianPlatformAdapter,
)

PLUGIN_PACKAGE = "astrbot_plugin_campus_group_guardian"


def make_message(
    text: str = "",
    *,
    platform: str = "aiocqhttp",
    adapter_id: str = "inst-1",
    group_id: str = "10001",
    sender_id: str = "20001",
    sender_name: str = "普通成员",
    role: GroupRole = GroupRole.MEMBER,
    message_id: str = "m1",
    images: list[GuardianImage] | None = None,
    age_seconds: float = 5.0,
    bot_id: str = "9",
    raw_event: Any = None,
) -> GuardianMessage:
    """构造统一消息对象（各测试共用）。"""
    return GuardianMessage(
        platform=platform,
        adapter_id=adapter_id,
        unified_msg_origin=f"{platform}:GroupMessage:{group_id}",
        conversation_id=group_id,
        group_id=group_id,
        message_id=message_id,
        sender_id=sender_id,
        sender_name=sender_name,
        sender_role=role,
        text=text,
        images=list(images or []),
        timestamp=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        bot_id=bot_id,
        raw_event=raw_event,
    )


@pytest.fixture
def message_factory():
    return make_message


@pytest.fixture
def event_factory():
    def _make(**kwargs: Any) -> AstrMessageEvent:
        return AstrMessageEvent(**kwargs)

    return _make


@pytest.fixture
def adapter_cls():
    return GuardianPlatformAdapter
