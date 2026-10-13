# -*- coding: utf-8 -*-
"""企业微信（wecom / wecom_ai_bot）适配器。

企业微信当前可用的接口只有收发消息一类，因此这里 **不伪造** 群管理能力：
撤回、禁言、踢人、黑名单全部保持不支持，风险检测照常运行，
处置降级为「记录 + 告警 + 管理员通知」（§27、§28）。
"""

from __future__ import annotations

from typing import Any

from ..core.capabilities import PlatformCapability, PlatformLimits, PlatformProfile, readonly_recall_policy
from .base import GuardianPlatformAdapter

CAPABILITIES = (
    PlatformCapability.RECEIVE_GROUP_MESSAGE
    | PlatformCapability.RECEIVE_IMAGE
    | PlatformCapability.SEND_MESSAGE
    | PlatformCapability.SEND_IMAGE
    | PlatformCapability.PROACTIVE_MESSAGE
)


class WeComPlatformAdapter(GuardianPlatformAdapter):
    """企业微信适配器（含 AI Bot）。"""

    platform_name = "wecom"

    @classmethod
    def default_profile(cls) -> PlatformProfile:
        return PlatformProfile(
            name=cls.platform_name,
            capabilities=CAPABILITIES,
            limits=PlatformLimits(),
            recall_policy=readonly_recall_policy(),
            notes=[
                "企业微信当前不提供群成员消息撤回、禁言、踢人接口，相关动作为 UNSUPPORTED。",
                "检测能力与其他平台一致：文本审核、图片审核、OCR、反诈、AI 审核均可用。",
                "处置方式为记录 + 群内提示 + 管理员通知，不会因为无法撤回而丢弃检测结果。",
            ],
        )


class WeComAiBotPlatformAdapter(WeComPlatformAdapter):
    """企业微信 AI Bot（接口与 wecom 一致，推送走消息回调）。"""

    platform_name = "wecom_ai_bot"

    def __init__(self, context: Any = None, profile: PlatformProfile | None = None) -> None:
        super().__init__(context, profile)
        self.profile.notes = list(self.profile.notes) + [
            "主动消息依赖 AstrBot 中配置的企业微信消息推送回调，未配置时发送会明确报错。",
        ]
