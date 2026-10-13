# -*- coding: utf-8 -*-
"""QQ 官方机器人适配器「请求构造」契约测试。

校验的是方法/路径/请求体是否符合 QQ 机器人开放平台 API v2 文档；
不触网，使用 tests/stubs/botpy 的 Route 桩记录请求。
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from astrbot_plugin_campus_group_guardian.core.actions.models import ActionStatus
from astrbot_plugin_campus_group_guardian.core.models import GroupRole
from astrbot_plugin_campus_group_guardian.platforms.qq_official import (
    BATCH_KICK_MAX,
    MUTE_MAX_SECONDS,
    QQOfficialPlatformAdapter,
)


class FakeHttp:
    """记录 botpy 层发出的 HTTP 请求。"""

    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.response = response
        self.error = error

    async def request(self, route, **kwargs):  # noqa: ANN001
        self.requests.append(
            {
                "method": route.method,
                "path": route.path,
                "parameters": dict(route.parameters),
                **kwargs,
            }
        )
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, http: FakeHttp) -> None:
        self.api = types.SimpleNamespace(_http=http)


def build(raw_event: Any = None) -> QQOfficialPlatformAdapter:
    return QQOfficialPlatformAdapter(context=None)


def message_with_client(fake_http: FakeHttp, **overrides):
    from tests.conftest import make_message

    event = types.SimpleNamespace(bot=FakeClient(fake_http))
    data = {
        "platform": "qq_official",
        "group_id": "G123",
        "message_id": "MSG1",
        "sender_id": "U1",
        "raw_event": event,
    }
    data.update(overrides)
    return make_message(**data)


async def test_recall_uses_delete_message_endpoint():
    http = FakeHttp(response=None)
    adapter = build()
    result = await adapter.recall_message(message_with_client(http))
    assert result.status is ActionStatus.SUCCESS
    request = http.requests[0]
    assert request["method"] == "DELETE"
    assert request["path"] == "/v2/groups/{group_openid}/messages/{message_id}"
    assert request["parameters"] == {"group_openid": "G123", "message_id": "MSG1"}


async def test_mute_uses_restrict_chat_setting_with_expire_at():
    http = FakeHttp(response={})
    adapter = build()
    result = await adapter.mute_member(message_with_client(http), "U2", 600)
    assert result.status is ActionStatus.SUCCESS
    request = http.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/v2/groups/{group_openid}/restrict_chat_setting"
    member = request["json"]["members"][0]
    assert member["op"] == "add"
    assert member["member_openid"] == "U2"
    expire_at = datetime.fromisoformat(member["mute_expire_at"])
    delta = expire_at - datetime.now(timezone.utc)
    assert timedelta(minutes=9) < delta < timedelta(minutes=11)


async def test_mute_longer_than_platform_limit_is_clamped():
    """§23：单次禁言上限 30 天，超长不能谎称一次生效。"""
    http = FakeHttp(response={})
    adapter = build()
    await adapter.mute_member(message_with_client(http), "U2", 90 * 24 * 3600)
    member = http.requests[0]["json"]["members"][0]
    expire_at = datetime.fromisoformat(member["mute_expire_at"])
    delta = expire_at - datetime.now(timezone.utc)
    assert delta <= timedelta(seconds=MUTE_MAX_SECONDS + 60)
    assert delta > timedelta(seconds=MUTE_MAX_SECONDS - 3600)


async def test_zero_duration_mute_falls_back_to_unmute():
    http = FakeHttp(response={})
    adapter = build()
    await adapter.mute_member(message_with_client(http), "U2", 0)
    member = http.requests[0]["json"]["members"][0]
    assert member["op"] == "del"
    assert member["mute_expire_at"] == ""


async def test_unmute_uses_del_op():
    http = FakeHttp(response={})
    adapter = build()
    await adapter.unmute_member(message_with_client(http), "U2")
    assert http.requests[0]["json"]["members"][0] == {
        "op": "del",
        "member_openid": "U2",
        "mute_expire_at": "",
    }


async def test_kick_uses_batch_remove_members():
    http = FakeHttp(response={"remove_members_result": "success"})
    adapter = build()
    result = await adapter.kick_member(message_with_client(http), "U2", "违规")
    assert result.status is ActionStatus.SUCCESS
    request = http.requests[0]
    assert request["path"] == "/v2/groups/{group_openid}/batch_remove_members"
    assert request["json"] == {"member_openids": ["U2"], "add_to_member_blacklist": False}


async def test_batch_kick_is_capped():
    http = FakeHttp(response={"remove_members_result": "success"})
    adapter = build()
    targets = [f"U{i}" for i in range(50)]
    await adapter.batch_kick_members(message_with_client(http), targets)
    assert len(http.requests[0]["json"]["member_openids"]) == BATCH_KICK_MAX


async def test_blacklist_add_and_fallback():
    """先调 member_blacklist；失败时退回 batch_remove_members + 拉黑。"""
    http = FakeHttp(response={"fail_openids": ["U2"]})
    adapter = build()
    result = await adapter.add_blacklist(message_with_client(http), "U2")
    assert len(http.requests) == 2
    assert http.requests[0]["path"] == "/v2/groups/{group_openid}/member_blacklist"
    assert http.requests[0]["json"] == {"op": "add", "member_openids": ["U2"]}
    assert http.requests[1]["path"] == "/v2/groups/{group_openid}/batch_remove_members"
    assert http.requests[1]["json"]["add_to_member_blacklist"] is True
    # 第二次也返回 fail_openids，因此整体失败，但绝不上抛异常
    assert result.status is ActionStatus.FAILED


async def test_blacklist_query_uses_get():
    """GET /v2/groups/{group_openid}/member_blacklist 返回 users 列表（已核对官方文档）。"""
    http = FakeHttp(response={"users": [{"member_openid": "U2"}], "next_cursor": ""})
    adapter = build()
    openids = await adapter.query_blacklist(message_with_client(http))
    assert http.requests[0]["method"] == "GET"
    assert http.requests[0]["path"] == "/v2/groups/{group_openid}/member_blacklist"
    assert openids == ["U2"]


async def test_rate_limit_and_permission_errors_are_mapped():
    """§21/§173：平台报错必须归一化，不外抛。"""
    http = FakeHttp(error=RuntimeError("code=11253, robot not admin"))
    adapter = build()
    result = await adapter.recall_message(message_with_client(http))
    assert result.status in (ActionStatus.FAILED, ActionStatus.UNSUPPORTED)
    assert result.status is not ActionStatus.SUCCESS


async def test_missing_botpy_client_returns_unsupported():
    """没有 botpy 客户端时返回 UNSUPPORTED，而不是抛异常。"""
    from tests.conftest import make_message

    adapter = build()
    result = await adapter.recall_message(make_message("测试", platform="qq_official"))
    assert result.status is ActionStatus.UNSUPPORTED


async def test_full_message_diagnosis_hints_when_only_at_messages_seen():
    """§24/§159：只收到 @ 消息时给出提示。"""
    adapter = build()

    class Raw:
        pass

    event = types.SimpleNamespace(
        message_obj=types.SimpleNamespace(raw_message=Raw()), message_str="<@!123> 你好"
    )
    event.message_obj.raw_message.__class__ = type("GroupMessage", (), {})
    adapter.note_message_event(event)
    diagnosis = adapter.full_message_diagnosis()
    assert diagnosis["level"] == "warn"
    assert "全部群消息" in diagnosis["hint"]


async def test_role_unknown_when_member_api_unavailable():
    """§53：拿不到角色必须是 UNKNOWN，不得假定为管理员。"""
    from tests.conftest import make_message

    adapter = build()
    role = await adapter.get_member_role(
        make_message("测试", platform="qq_official"), "U1"
    )
    assert role is GroupRole.UNKNOWN
    assert await adapter.get_bot_role(make_message("测试", platform="qq_official")) is GroupRole.UNKNOWN
