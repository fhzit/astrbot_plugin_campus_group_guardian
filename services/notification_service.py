# -*- coding: utf-8 -*-
"""通知服务：群内反诈预警 + 管理员通知（§89、§118）。

文案只做风险提示，不公开指认个人诈骗。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from ..core.antifraud.models import FraudResult
from ..core.models import GuardianMessage

_LOG = "[Guardian][Notify]"

#: 群内预警文案（§89）。
GROUP_WARNING_TEMPLATE = (
    "⚠️ 检测到一条高风险缴费信息，系统已暂时进行处理。\n"
    "请勿立即扫码、转账或付款。\n"
    "学校收费请优先通过群管理员或官方渠道再次确认。\n"
    "如已付款，请立刻联系班主任与家长群管理员。"
)

#: 管理员通知模板。
ADMIN_NOTICE_TEMPLATE = (
    "【GroupGuardian 反诈提醒】\n"
    "平台：{platform}\n"
    "群：{group_id}\n"
    "成员：{sender_name}({sender_id}) 角色：{sender_role}\n"
    "风险：{score:g}/100（{level}）\n"
    "消息：{excerpt}\n"
    "信号：{signals}\n"
    "动作：{actions}"
)


class NotificationService:
    """群内与管理员通知。"""

    def __init__(
        self,
        context: Any,
        *,
        config_service: Any = None,
        storage: Any = None,
        warning_text: str = "",
    ) -> None:
        self.context = context
        self.config_service = config_service
        self.storage = storage
        self.warning_text = warning_text or GROUP_WARNING_TEMPLATE

    # ------------------------------------------------------------------
    # 群内预警
    # ------------------------------------------------------------------
    async def send_group_warning(self, adapter: Any, message: GuardianMessage, result: FraudResult) -> bool:
        """发送群内反诈预警。平台不支持发消息时返回 False（不是异常）。"""
        text = self.warning_text
        action_result = await adapter.send_message(message.unified_msg_origin, text)
        if not action_result.ok:
            logger.info(f"{_LOG} 群内预警未发送：{action_result.message}")
            return False
        logger.info(f"{_LOG} 已向群 {message.group_id} 发送反诈预警")
        _ = result
        return True

    # ------------------------------------------------------------------
    # 管理员通知
    # ------------------------------------------------------------------
    async def notify_admin(self, message: GuardianMessage, action: Any) -> bool:
        """通知插件管理员。返回是否有会话成功收到消息。"""
        sessions = self._admin_sessions()
        if not sessions or self.context is None:
            return False
        text = self.render_admin_notice(message, action)
        delivered = False
        for session in sessions:
            try:
                from astrbot.api.event import MessageChain
                from astrbot.api.message_components import Plain

                ok = await self.context.send_message(session, MessageChain([Plain(text)]))
                delivered = delivered or bool(ok)
            except Exception as exc:
                logger.debug(f"{_LOG} 管理员通知发送失败（{session}）：{exc}")
        if delivered:
            logger.info(f"{_LOG} 已通知管理员，会话数={len(sessions)}")
        return delivered

    def _admin_sessions(self) -> list[str]:
        """管理员会话列表来自配置（用户手工填写的 UMO 列表）。"""
        if self.config_service is None:
            return []
        snapshot = self.config_service.global_snapshot()
        return [str(item) for item in snapshot.get_list("actions.notify_admin_session") if item]

    def render_admin_notice(self, message: GuardianMessage, action: Any) -> str:
        """渲染管理员通知正文。"""
        result: FraudResult | None = getattr(action, "metadata", {}).get("fraud_result")
        summary = getattr(action, "metadata", {}).get("summary")
        if isinstance(result, FraudResult):
            signals = "、".join(signal.type.label for signal in result.signals) or "无"
            excerpt = " ".join(str(message.text or "").split())[:160]
            return ADMIN_NOTICE_TEMPLATE.format(
                platform=f"{message.platform}/{message.adapter_id}",
                group_id=message.group_id,
                sender_name=message.sender_name or "",
                sender_id=message.sender_id,
                sender_role=message.sender_role.value,
                score=result.score,
                level=result.level.label,
                excerpt=excerpt,
                signals=signals,
                actions=summary or "见插件面板",
            )
        excerpt = " ".join(str(message.text or "").split())[:160]
        return (
            f"【GroupGuardian 提醒】\n群：{message.group_id}\n"
            f"成员：{message.sender_name}({message.sender_id})\n"
            f"消息：{excerpt}\n原因：{getattr(action, 'reason', '')}"
        )
