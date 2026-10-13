# -*- coding: utf-8 -*-
"""平台无关的统一消息模型与基础枚举。

所有检测模块只依赖本文件里的对象，不得直接依赖任何平台的原始事件 JSON。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


class GroupRole(Enum):
    """统一群角色。平台无法判定时必须落到 UNKNOWN，不得当成管理员。"""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    UNKNOWN = "unknown"

    @property
    def is_privileged(self) -> bool:
        """群主或群管理员。反诈模块据此放行。"""
        return self in (GroupRole.OWNER, GroupRole.ADMIN)

    @classmethod
    def parse(cls, raw: Any) -> "GroupRole":
        """把平台返回的角色值归一化。

        Args:
            raw: 平台角色值，可能是 "owner"/"admin"/"member"、中文或 None。

        Returns:
            归一化后的 GroupRole，无法识别时返回 UNKNOWN。
        """
        if isinstance(raw, GroupRole):
            return raw
        if raw is None:
            return GroupRole.UNKNOWN
        value = str(raw).strip().lower()
        mapping = {
            "owner": GroupRole.OWNER,
            "群主": GroupRole.OWNER,
            "creator": GroupRole.OWNER,
            "admin": GroupRole.ADMIN,
            "administrator": GroupRole.ADMIN,
            "set": GroupRole.ADMIN,
            "管理员": GroupRole.ADMIN,
            "member": GroupRole.MEMBER,
            "member-普通成员": GroupRole.MEMBER,
            "普通成员": GroupRole.MEMBER,
            "members": GroupRole.MEMBER,
            "everyone": GroupRole.MEMBER,
        }
        return mapping.get(value, GroupRole.UNKNOWN)


@dataclass(frozen=True)
class GroupKey:
    """多平台群唯一键。

    只使用 group_id 会在多平台/多实例下冲突，因此固定带上平台与适配器实例。
    """

    platform: str
    adapter_id: str
    group_id: str

    def __str__(self) -> str:
        return f"{self.platform}:{self.adapter_id}:{self.group_id}"

    @classmethod
    def from_parts(cls, platform: str, adapter_id: str, group_id: str) -> "GroupKey":
        return cls(platform=str(platform or ""), adapter_id=str(adapter_id or ""), group_id=str(group_id or ""))

    @classmethod
    def from_umo(cls, umo: str, group_id: str) -> "GroupKey":
        """从 AstrBot UMO 推导 GroupKey。

        UMO 形如 ``<平台实例 id>:GroupMessage:<群 id>``，第一段是平台实例 id。
        """
        parts = str(umo or "").split(":", 2)
        platform = parts[0] if parts else ""
        adapter_id = parts[0] if parts else ""
        gid = str(group_id or "")
        if not gid and len(parts) == 3 and parts[1] == "GroupMessage":
            gid = parts[2]
        return cls(platform=platform, adapter_id=adapter_id, group_id=gid)


@dataclass
class GuardianImage:
    """统一图片对象。只保留下载所需信息与内容指纹。"""

    url: str | None = None
    file_path: str | None = None
    mime_type: str | None = None
    sha256: str | None = None
    source: str = "message"
    raw: Any = None

    def cache_key(self) -> str | None:
        """OCR 去重键：优先内容指纹，其次 URL。"""
        if self.sha256:
            return f"sha256:{self.sha256}"
        if self.url:
            return f"url:{self.url}"
        if self.file_path:
            return f"path:{self.file_path}"
        return None


@dataclass
class PaymentAmount:
    """从文本中提取出的金额。"""

    currency: str
    amount: Decimal
    raw: str

    def __str__(self) -> str:
        return f"{self.currency}{self.amount}"


@dataclass
class GuardianMessage:
    """统一消息对象。检测层唯一输入。"""

    platform: str
    adapter_id: str
    unified_msg_origin: str
    conversation_id: str
    group_id: str | None
    message_id: str
    sender_id: str
    sender_name: str | None
    sender_role: GroupRole
    text: str
    images: list[GuardianImage] = field(default_factory=list)
    reply_to_message_id: str | None = None
    bot_id: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_event: Any = None

    @property
    def is_group(self) -> bool:
        return bool(self.group_id)

    @property
    def group_key(self) -> GroupKey:
        return GroupKey.from_parts(self.platform, self.adapter_id, self.group_id or "")

    def age_seconds(self, now: datetime | None = None) -> float:
        """消息年龄（秒）。用于撤回时限判断。"""
        moment = now or datetime.now(timezone.utc)
        stamp = self.timestamp
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(0.0, (moment - stamp).total_seconds())
