# -*- coding: utf-8 -*-
"""School Fraud Guard：家校群虚假缴费诈骗检测引擎（§49-§89）。

检测顺序（先便宜后昂贵）：
1. 角色解析 → 群主/管理员直接放行（ANTI_FRAUD_BYPASS）
2. 本地快筛（收费语义 / 金额 / 付款动作 / 备注学生姓名 / 付款截图 / 催促 / 教师冒充）
3. 上下文窗口联合判断（多消息拆分诈骗）
4. 图片：按策略决定是否 OCR / 视觉识别，OCR 失败不自动判安全
5. AI 复核（仅在本地不确定时，且可按配置关闭）
6. 组合加权打分 → FraudResult
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from astrbot.api import logger

from ...providers.ai import FraudAiReviewer
from ...providers.ocr import OcrService
from ..models import GroupRole, GuardianImage, GuardianMessage, PaymentAmount
from .detectors import (
    FraudContextStore,
    build_ocr_signals,
    detect_text_signals,
    has_payment_context,
    is_qrcode_context,
    merge_window_signals,
    should_ocr,
)
from .models import (
    ContextEntry,
    FraudResult,
    FraudRiskLevel,
    FraudSignal,
    FraudSignalType,
    FraudThresholds,
    SignalSource,
)
from .risk_engine import score_signals

_LOG = "[Guardian][AntiFraud]"


@dataclass
class AntiFraudConfig:
    """反诈配置（可由群级覆盖，§136）。"""

    enabled: bool = True
    thresholds: FraudThresholds = field(default_factory=FraudThresholds)
    context_messages: int = 10
    context_seconds: int = 300
    teacher_names: list[str] = field(default_factory=list)
    trusted_sender_ids: list[str] = field(default_factory=list)
    ocr_enabled: bool = True
    strict_image_scan: bool = False
    ai_enabled: bool = False
    ai_only_when_uncertain: bool = True
    auto_recall_high: bool = False
    auto_recall_critical: bool = True
    auto_mute_critical: bool = True
    critical_mute_seconds: int = 600
    auto_kick_critical: bool = False
    auto_blacklist_critical: bool = False
    group_warning_enabled: bool = True
    #: CRITICAL 时是否阻断 AstrBot 后续处理（§103）。群主/管理员放行时不会触发。
    stop_event_on_critical: bool = True

    def is_trusted(self, sender_id: str) -> bool:
        return bool(sender_id) and sender_id in self.trusted_sender_ids


class SchoolFraudGuard:
    """反诈检测引擎。检测与平台无关，处置交给 ActionEngine。"""

    def __init__(
        self,
        *,
        ocr_service: OcrService | None = None,
        ai_reviewer: FraudAiReviewer | None = None,
        context_store: FraudContextStore | None = None,
    ) -> None:
        self.ocr_service = ocr_service
        self.ai_reviewer = ai_reviewer
        self.context_store = context_store or FraudContextStore()
        self.stats: dict[str, int] = {}

    def configure_context(self, max_messages: int, ttl_seconds: int) -> None:
        """按配置调整上下文窗口。"""
        if (
            self.context_store.max_messages != max(1, int(max_messages))
            or self.context_store.ttl_seconds != max(1, int(ttl_seconds))
        ):
            self.context_store = FraudContextStore(max_messages, ttl_seconds)

    async def analyze(
        self,
        message: GuardianMessage,
        config: AntiFraudConfig,
    ) -> FraudResult:
        """对一条消息做反诈判定。"""
        self.stats["scanned_count"] = self.stats.get("scanned_count", 0) + 1
        if not config.enabled:
            return FraudResult(
                level=FraudRiskLevel.SAFE,
                explanation=["反诈模块未开启"],
                thresholds=config.thresholds,
            )

        # 1) 角色放行：群主/管理员发布的缴费信息不进入反诈判定（§51）
        if message.sender_role.is_privileged:
            logger.debug(f"{_LOG} 发送者为{message.sender_role.value}，School Fraud 放行")
            self.stats["bypass_count"] = self.stats.get("bypass_count", 0) + 1
            return score_signals([], thresholds=config.thresholds, sender_privileged=True)

        # 1.1) 可信名单放行：企微/钉钉等拿不到完整群角色的平台可用（§53）
        if config.is_trusted(message.sender_id):
            logger.debug(f"{_LOG} 发送者 {message.sender_id} 在可信名单中，School Fraud 放行")
            self.stats["trusted_bypass_count"] = self.stats.get("trusted_bypass_count", 0) + 1
            return score_signals(
                [],
                thresholds=config.thresholds,
                sender_privileged=True,
                bypass_reason="TRUSTED_SENDER_BYPASS",
                bypass_explanation="发送者在可信名单中，反诈模块放行（群角色信息不足平台的兜底）",
            )

        # 2) 本地快筛
        text_signals, amounts = detect_text_signals(
            message.text,
            message_id=message.message_id,
            sender_name=message.sender_name,
            teacher_names=config.teacher_names,
        )

        # 3) 上下文窗口：UNKNOWN 角色按普通成员处理，但可信名单可放行（§53）
        group_key = str(message.group_key)
        window_signals, multi_message = self._update_context(message, config, text_signals, amounts)
        combined: list[FraudSignal] = list(window_signals)
        present_types = {signal.type for signal in combined}

        # 4) 图片：按策略决定是否识别
        ocr_text = ""
        image_signal: FraudSignal | None = None
        if message.images:
            need_ocr = should_ocr(
                image_count=len(message.images),
                signal_types=present_types,
                strict_image_scan=config.strict_image_scan and config.ocr_enabled,
                sender_privileged=False,
            )
            if need_ocr and self.ocr_service is not None:
                ocr_text, image_signal = await self._analyze_images(message)
                if ocr_text:
                    ocr_signals, ocr_amounts = build_ocr_signals(
                        ocr_text, message_id=message.message_id
                    )
                    combined.extend(ocr_signals)
                    amounts.extend(
                        amount for amount in ocr_amounts if amount not in amounts
                    )
            elif message.images and self._has_payment_evidence(present_types):
                image_signal = FraudSignal(
                    type=FraudSignalType.PAYMENT_IMAGE,
                    evidence=f"消息包含 {len(message.images)} 张图片，且文本处于付款语境",
                    source=SignalSource.IMAGE,
                    message_id=message.message_id,
                )
            if image_signal is not None:
                combined.append(image_signal)

        if multi_message:
            combined.append(
                FraudSignal(
                    type=FraudSignalType.MULTI_MESSAGE_PATTERN,
                    evidence=f"近 {config.context_seconds} 秒内同一发送者多条消息组合出现缴费特征",
                    source=SignalSource.CONTEXT,
                    message_id=message.message_id,
                )
            )

        # 5) 先算一次分，决定是否需要 AI 复核
        result = score_signals(combined, thresholds=config.thresholds)
        result.amounts = amounts
        result.ocr_text = ocr_text

        if self._needs_ai(result, config):
            ai_signal, verdict = await self._ai_review(message, config, result, ocr_text)
            if ai_signal is not None:
                combined.append(ai_signal)
                result = score_signals(combined, thresholds=config.thresholds)
                result.amounts = amounts
                result.ocr_text = ocr_text
                result.explanation.append("AI 复核支持本地判定")
            elif verdict is not None:
                result.explanation.append(
                    f"AI 复核未确认风险：{verdict.get('verdict', 'unknown')}"
                )

        if config.is_trusted(message.sender_id) and result.level is FraudRiskLevel.SUSPICIOUS:
            result.explanation.append("发送者在可信名单中，反诈降级为记录")
            result.level = FraudRiskLevel.SAFE
            result.should_review = False
        if result.is_risky:
            logger.info(
                f"{_LOG} 风险 {result.score:g}/{result.level.value} "
                f"群={message.group_id} 成员={message.sender_id} 信号="
                f"{','.join(sorted(signal.type.value for signal in result.signals))}"
            )
        return result

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _update_context(
        self,
        message: GuardianMessage,
        config: AntiFraudConfig,
        text_signals: list[FraudSignal],
        amounts: list[PaymentAmount],
    ) -> tuple[list[FraudSignal], bool]:
        """写入并读取上下文窗口，返回（合并后的窗口信号, 是否为多消息组合）。"""
        self.configure_context(config.context_messages, config.context_seconds)
        entry = ContextEntry(
            message_id=message.message_id,
            sender_id=message.sender_id,
            text=message.text,
            timestamp=message.timestamp or datetime.now(timezone.utc),
            signals=list(text_signals),
            has_image=bool(message.images),
        )
        self.context_store.add(str(message.group_key), entry)
        window = self.context_store.window(str(message.group_key), message.sender_id)
        merged, multi_message = merge_window_signals(window)
        _ = amounts
        return merged, multi_message

    async def _analyze_images(self, message: GuardianMessage) -> tuple[str, FraudSignal | None]:
        """对消息里的图片做识别，返回（合并后的 OCR 文本, 图片信号）。"""
        texts: list[str] = []
        failed = False
        for image in message.images:
            result = await self.ocr_service.recognize(image)
            if result.success and result.text:
                texts.append(result.text)
            else:
                failed = True
                logger.debug(
                    f"{_LOG} OCR 未成功（provider={result.provider} error={result.error_type}），"
                    "保留本地风险信号继续判定"
                )
        combined_text = "\n".join(texts).strip()
        signal = None
        if combined_text:
            signal = FraudSignal(
                type=FraudSignalType.PAYMENT_IMAGE,
                evidence=f"消息包含 {len(message.images)} 张图片（已识别到文本）",
                source=SignalSource.IMAGE,
                message_id=message.message_id,
            )
        elif failed:
            # OCR 失败绝不等于安全：保留弱信号，交由后续策略决定是否提醒管理员。
            signal = FraudSignal(
                type=FraudSignalType.PAYMENT_IMAGE,
                evidence=f"消息包含 {len(message.images)} 张图片，识别失败，无法确认内容",
                source=SignalSource.IMAGE,
                message_id=message.message_id,
            )
        return combined_text, signal

    @staticmethod
    def _has_payment_evidence(signal_types: set[FraudSignalType]) -> bool:
        return has_payment_context(signal_types) or bool(
            signal_types & {FraudSignalType.AMOUNT, FraudSignalType.OCR_PAYMENT_TEXT}
        )

    def _needs_ai(
        self,
        result: FraudResult,
        config: AntiFraudConfig,
    ) -> bool:
        """是否需要 AI 复核。"""
        if not config.ai_enabled or self.ai_reviewer is None or not self.ai_reviewer.enabled:
            return False
        if config.ai_only_when_uncertain:
            # 只有落到“可疑”区间才需要模型补证据；已确定的高风险不等待 AI。
            return result.level in (FraudRiskLevel.SUSPICIOUS, FraudRiskLevel.HIGH) and not result.fast_path
        return True

    async def _ai_review(
        self,
        message: GuardianMessage,
        config: AntiFraudConfig,
        result: FraudResult,
        ocr_text: str,
    ) -> tuple[FraudSignal | None, dict[str, Any] | None]:
        window = self.context_store.window(str(message.group_key), message.sender_id)
        context_lines = [entry.text for entry in window[-5:] if entry.text]
        return await self.ai_reviewer.review(
            text=message.text,
            ocr_text=ocr_text,
            context_messages=context_lines,
            sender_role=message.sender_role.value,
            local_signals=[signal.type.label for signal in result.signals],
            cache_key=f"fraud:{message.sender_id}:{message.text[:120]}:{ocr_text[:60]}",
        )

    @staticmethod
    def is_qrcode_message(message: GuardianMessage) -> bool:
        """消息是否在描述二维码（仅用于人工复核提示，不单独作为判定依据）。"""
        return is_qrcode_context(message.text)

    def collect_images_for_review(self, message: GuardianMessage) -> list[GuardianImage]:
        return list(message.images)
