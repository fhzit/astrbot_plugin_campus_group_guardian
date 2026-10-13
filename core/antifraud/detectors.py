# -*- coding: utf-8 -*-
"""反诈检测器：本地低成本快筛（§54-§63、§75）。

检测器只产出信号，不参与打分；权重与组合逻辑在 risk_engine 中。
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from ..models import PaymentAmount
from .lexicon import (
    PAYMENT_ACTION_TERMS,
    PAYMENT_INTENT_TERMS,
    QRCODE_IMAGE_TERMS,
    SCREENSHOT_REQUEST_TERMS,
    STUDENT_NAME_REQUEST_TERMS,
    TEACHER_TITLE_TERMS,
    URGENCY_TERMS,
    extract_amounts,
    find_terms,
    teacher_name_matches,
)
from .models import ContextEntry, FraudSignal, FraudSignalType, SignalSource

#: 语境信号：出现这些信号才允许把裸数字当金额、才认为图片属于付款语境。
PAYMENT_CONTEXT_SIGNALS = (
    FraudSignalType.PAYMENT_INTENT,
    FraudSignalType.PAYMENT_ACTION,
)

#: 快筛优先级：只要命中前三类中的任意两类，就进入高风险组合。
STRONG_SIGNALS = (
    FraudSignalType.PAYMENT_INTENT,
    FraudSignalType.AMOUNT,
    FraudSignalType.PAYMENT_ACTION,
)


def _signal(
    signal_type: FraudSignalType,
    evidence: str,
    source: SignalSource = SignalSource.TEXT,
    message_id: str = "",
) -> FraudSignal:
    return FraudSignal(type=signal_type, evidence=evidence, source=source, message_id=message_id)


def detect_text_signals(
    text: str,
    *,
    source: SignalSource = SignalSource.TEXT,
    message_id: str = "",
    sender_name: str | None = None,
    teacher_names: list[str] | tuple[str, ...] = (),
) -> tuple[list[FraudSignal], list[PaymentAmount]]:
    """对一段文本做本地快筛。

    Args:
        text: 文本（应已标准化）。
        source: 信号来源（正文 / OCR）。
        message_id: 归属消息 id，便于事件详情回溯。
        sender_name: 发送者昵称，用于教师冒充判定。
        teacher_names: 配置的教师姓名。

    Returns:
        (信号列表, 金额列表)
    """
    signals: list[FraudSignal] = []
    haystack = str(text or "")

    intent_hits = find_terms(haystack, PAYMENT_INTENT_TERMS)
    if intent_hits:
        signals.append(
            _signal(FraudSignalType.PAYMENT_INTENT, "、".join(intent_hits[:5]), source, message_id)
        )

    action_hits = find_terms(haystack, PAYMENT_ACTION_TERMS)
    if action_hits:
        signals.append(
            _signal(FraudSignalType.PAYMENT_ACTION, "、".join(action_hits[:5]), source, message_id)
        )

    # 裸数字只在收费/付款语境下才算金额，避免把页码、时间当钱。
    action_hits = find_terms(haystack, PAYMENT_ACTION_TERMS)
    # 收费/付款语境内才允许把裸数字当金额；仅有收费语义时不做裸数字兜底，
    # 避免“校服是不是500左右”这类正常讨论被当成金额（§67、§68）。
    allow_bare = bool(action_hits)
    amounts = extract_amounts(haystack, allow_bare=allow_bare)
    if amounts:
        evidence = "、".join(str(amount) for amount in amounts[:4])
        signals.append(_signal(FraudSignalType.AMOUNT, evidence, source, message_id))

    name_hits = find_terms(haystack, STUDENT_NAME_REQUEST_TERMS)
    if name_hits:
        signals.append(
            _signal(FraudSignalType.STUDENT_NAME_REQUEST, "、".join(name_hits[:3]), source, message_id)
        )

    screenshot_hits = find_terms(haystack, SCREENSHOT_REQUEST_TERMS)
    if screenshot_hits:
        signals.append(
            _signal(
                FraudSignalType.PAYMENT_SCREENSHOT_REQUEST,
                "、".join(screenshot_hits[:3]),
                source,
                message_id,
            )
        )

    urgency_hits = find_terms(haystack, URGENCY_TERMS)
    if urgency_hits:
        signals.append(_signal(FraudSignalType.URGENCY, "、".join(urgency_hits[:3]), source, message_id))

    matched_teacher = teacher_name_matches(sender_name, teacher_names)
    if matched_teacher:
        title_hits = find_terms(sender_name or "", TEACHER_TITLE_TERMS)
        evidence = f"昵称“{sender_name}”与教师姓名“{matched_teacher}”匹配"
        if title_hits:
            evidence = f"{evidence}（含“{title_hits[0]}”）"
        signals.append(_signal(FraudSignalType.TEACHER_IMPERSONATION, evidence, source, message_id))

    return signals, amounts


def has_payment_context(signal_types: set[FraudSignalType]) -> bool:
    """是否处于付费用语语境。"""
    return any(signal_type in signal_types for signal_type in PAYMENT_CONTEXT_SIGNALS)


def should_ocr(
    *,
    image_count: int,
    signal_types: set[FraudSignalType],
    strict_image_scan: bool,
    sender_privileged: bool,
) -> bool:
    """是否值得对图片调用 OCR（§75）。

    - 没有图片：不调用；
    - 群主/管理员：反诈放行，不调用；
    - 严格模式：普通成员发图就扫描；
    - 普通模式：只有文本已经出现收费/支付信号才扫描，避免普通表情图也走 OCR。
    """
    if image_count <= 0 or sender_privileged:
        return False
    if strict_image_scan:
        return True
    return has_payment_context(signal_types)


def build_ocr_signals(
    ocr_text: str,
    *,
    message_id: str = "",
    sender_name: str | None = None,
    teacher_names: list[str] | tuple[str, ...] = (),
) -> tuple[list[FraudSignal], list[PaymentAmount]]:
    """对 OCR 文本做二次快筛，并标记来源为 OCR。"""
    signals, amounts = detect_text_signals(
        ocr_text,
        source=SignalSource.OCR,
        message_id=message_id,
        sender_name=None,  # 图片文本里不做教师冒充判定
        teacher_names=(),
    )
    if signals and amounts:
        signals.append(
            _signal(
                FraudSignalType.OCR_PAYMENT_TEXT,
                "OCR 文本命中付款信息",
                SignalSource.OCR,
                message_id,
            )
        )
    elif has_payment_context({signal.type for signal in signals}):
        signals.append(
            _signal(
                FraudSignalType.OCR_PAYMENT_TEXT,
                "OCR 文本命中收费/付款用词",
                SignalSource.OCR,
                message_id,
            )
        )
    _ = sender_name, teacher_names
    return signals, amounts


def is_qrcode_context(text: str) -> bool:
    """文本是否在描述二维码/收款码。"""
    return bool(find_terms(text, QRCODE_IMAGE_TERMS))


class FraudContextStore:
    """多消息诈骗联合判定的内存上下文（§62、§63）。

    按 ``群 + 发送者`` 维护最近若干条消息，只驻留内存并周期清理，
    不做聊天内容长期存储。
    """

    def __init__(self, max_messages: int = 10, ttl_seconds: int = 300) -> None:
        self.max_messages = max(1, int(max_messages))
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._entries: dict[tuple[str, str], deque[ContextEntry]] = defaultdict(
            lambda: deque(maxlen=self.max_messages)
        )

    def add(self, group_key: str, entry: ContextEntry) -> None:
        self._entries[(group_key, entry.sender_id)].append(entry)

    def window(self, group_key: str, sender_id: str, now: datetime | None = None) -> list[ContextEntry]:
        """取仍在时效内的上下文（不含已过期条目）。"""
        moment = now or datetime.now(timezone.utc)
        cutoff = moment - timedelta(seconds=self.ttl_seconds)
        entries = self._entries.get((group_key, sender_id))
        if not entries:
            return []
        fresh = [entry for entry in entries if entry.timestamp >= cutoff]
        self._entries[(group_key, sender_id)] = deque(fresh, maxlen=self.max_messages)
        return fresh

    def prune(self, now: datetime | None = None) -> int:
        """清理过期上下文，返回清理掉的条目数。"""
        moment = now or datetime.now(timezone.utc)
        cutoff = moment - timedelta(seconds=self.ttl_seconds)
        removed = 0
        for key in list(self._entries.keys()):
            entries = self._entries[key]
            fresh = [entry for entry in entries if entry.timestamp >= cutoff]
            removed += len(entries) - len(fresh)
            if fresh:
                self._entries[key] = deque(fresh, maxlen=self.max_messages)
            else:
                del self._entries[key]
        return removed

    def clear(self) -> None:
        self._entries.clear()

    def sender_count(self) -> int:
        return len(self._entries)


def merge_window_signals(entries: list[ContextEntry]) -> tuple[list[FraudSignal], bool]:
    """合并上下文窗口内的信号。

    同类型信号只保留一条（证据合并），跨多条消息出现时标记多消息组合模式。

    Returns:
        (合并后的信号列表, 是否构成多消息组合)
    """
    buckets: dict[FraudSignalType, list[FraudSignal]] = defaultdict(list)
    message_ids: set[str] = set()
    for entry in entries:
        for signal in entry.signals:
            buckets[signal.type].append(signal)
            if signal.message_id:
                message_ids.add(signal.message_id)

    merged: list[FraudSignal] = []
    for signal_type, items in buckets.items():
        evidence_parts: list[str] = []
        for item in items:
            if item.evidence and item.evidence not in evidence_parts:
                evidence_parts.append(item.evidence)
        representative = items[0]
        merged.append(
            FraudSignal(
                type=signal_type,
                evidence="；".join(evidence_parts[:4]),
                source=representative.source,
                message_id=representative.message_id,
            )
        )
    multi_message = len(message_ids) >= 2 and len(buckets) >= 2
    return merged, multi_message
