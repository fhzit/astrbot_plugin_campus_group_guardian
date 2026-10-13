# -*- coding: utf-8 -*-
"""钉钉（dingtalk）适配器。

内容层可用：文本、图片、OCR、AI 审核、反诈检测、群内风险提醒、主动通知。
管理动作严格按钉钉开放能力与 AstrBot 适配器实际支持情况给出，
不存在的禁言/撤回一律 UNSUPPORTED（§29）。
"""

from __future__ import annotations

from ..core.capabilities import PlatformCapability, PlatformLimits, PlatformProfile, readonly_recall_policy
from .base import GuardianPlatformAdapter

CAPABILITIES = (
    PlatformCapability.RECEIVE_GROUP_MESSAGE
    | PlatformCapability.RECEIVE_IMAGE
    | PlatformCapability.SEND_MESSAGE
    | PlatformCapability.SEND_IMAGE
    | PlatformCapability.PROACTIVE_MESSAGE
)


class DingTalkPlatformAdapter(GuardianPlatformAdapter):
    """钉钉适配器。"""

    platform_name = "dingtalk"

    @classmethod
    def default_profile(cls) -> PlatformProfile:
        return PlatformProfile(
            name=cls.platform_name,
            capabilities=CAPABILITIES,
            limits=PlatformLimits(),
            recall_policy=readonly_recall_policy(),
            notes=[
                "钉钉机器人没有成员禁言/撤回群成员消息的开放接口，相关动作为 UNSUPPORTED。",
                "检测层不得因为平台不是 QQ 而跳过：反诈与内容审核对其他平台同样运行。",
                "处置降级为记录 + 群内提示 + 管理员通知。",
            ],
        )
