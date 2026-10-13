# -*- coding: utf-8 -*-
"""风险评分引擎（§64-§68）。

不做无限线性相加：先按信号给基础分，再按“组合”加权，
并对只有弱信号（金额/催促/图片）的消息封顶，避免“有二维码就是诈骗”。
"""

from __future__ import annotations

from decimal import Decimal

from .models import (
    FraudResult,
    FraudRiskLevel,
    FraudSignal,
    FraudSignalType,
    FraudThresholds,
)

#: 信号基础权重。
SIGNAL_WEIGHTS: dict[FraudSignalType, float] = {
    FraudSignalType.PAYMENT_INTENT: 18.0,
    FraudSignalType.AMOUNT: 20.0,
    FraudSignalType.PAYMENT_ACTION: 22.0,
    FraudSignalType.STUDENT_NAME_REQUEST: 15.0,
    FraudSignalType.PAYMENT_SCREENSHOT_REQUEST: 15.0,
    FraudSignalType.URGENCY: 6.0,
    FraudSignalType.TEACHER_IMPERSONATION: 20.0,
    FraudSignalType.PAYMENT_IMAGE: 20.0,
    FraudSignalType.OCR_PAYMENT_TEXT: 15.0,
    FraudSignalType.AI_PAYMENT_RISK: 25.0,
    FraudSignalType.MULTI_MESSAGE_PATTERN: 10.0,
}

#: 组合加权：键为必须同时出现的信号集合，值为乘数。
COMBINATION_BONUSES: tuple[tuple[frozenset[FraudSignalType], float, str], ...] = (
    (
        frozenset(
            {
                FraudSignalType.PAYMENT_INTENT,
                FraudSignalType.AMOUNT,
                FraudSignalType.PAYMENT_ACTION,
            }
        ),
        1.25,
        "收费语义 + 金额 + 付款动作 同时出现，风险显著提升",
    ),
    (
        frozenset({FraudSignalType.STUDENT_NAME_REQUEST}),
        1.15,
        "要求备注学生姓名，典型冒充收费话术",
    ),
    (
        frozenset({FraudSignalType.PAYMENT_SCREENSHOT_REQUEST}),
        1.15,
        "要求付款后截图，诱导形成从众效应",
    ),
    (
        frozenset({FraudSignalType.TEACHER_IMPERSONATION}),
        1.10,
        "昵称与教师姓名高度匹配",
    ),
    (
        frozenset({FraudSignalType.PAYMENT_IMAGE}),
        1.10,
        "消息包含付款类图片",
    ),
    (
        frozenset({FraudSignalType.AI_PAYMENT_RISK}),
        1.10,
        "AI 复核判定存在付款诱导",
    ),
)

#: 快速通道（§83）：普通成员 + 缴费 + 金额 + 付款动作 + 任一强诱导信号。
FAST_PATH_REQUIRED = frozenset(
    {
        FraudSignalType.PAYMENT_INTENT,
        FraudSignalType.AMOUNT,
        FraudSignalType.PAYMENT_ACTION,
    }
)
FAST_PATH_TRIGGERS = frozenset(
    {
        FraudSignalType.STUDENT_NAME_REQUEST,
        FraudSignalType.PAYMENT_SCREENSHOT_REQUEST,
        FraudSignalType.TEACHER_IMPERSONATION,
    }
)

#: 弱信号：单独出现时不允许进入高风险等级。
WEAK_SIGNALS = frozenset(
    {
        FraudSignalType.AMOUNT,
        FraudSignalType.URGENCY,
        FraudSignalType.PAYMENT_IMAGE,
        FraudSignalType.OCR_PAYMENT_TEXT,
    }
)

SIGNIFICANT_SIGNALS = FAST_PATH_REQUIRED


def score_signals(
    signals: list[FraudSignal],
    *,
    thresholds: FraudThresholds | None = None,
    sender_privileged: bool = False,
    bypass_reason: str = "ANTI_FRAUD_BYPASS",
    bypass_explanation: str = "群主/群管理员发布的缴费信息不进入 School Fraud Guard 判定",
) -> FraudResult:
    """对信号集合打分。

    Args:
        signals: 检测到的信号（可来自文本、OCR、图片、AI、上下文）。
        thresholds: 风险阈值。
        sender_privileged: 群主/管理员是否放行（放行时仍然返回结果，但等级为 SAFE）。
        bypass_reason: 放行原因代码（写入事件记录，便于区分角色放行与可信名单放行）。
        bypass_explanation: 放行的可读说明。

    Returns:
        FraudResult。
    """
    limits = thresholds or FraudThresholds()
    if sender_privileged:
        return FraudResult(
            score=0.0,
            level=FraudRiskLevel.SAFE,
            signals=[],
            explanation=[bypass_explanation],
            should_review=False,
            bypassed=True,
            bypass_reason=bypass_reason,
            thresholds=limits,
        )

    unique: dict[FraudSignalType, FraudSignal] = {}
    for signal in signals:
        existing = unique.get(signal.type)
        if existing is None:
            unique[signal.type] = signal
        elif signal.evidence and signal.evidence not in existing.evidence:
            unique[signal.type] = FraudSignal(
                type=existing.type,
                evidence=f"{existing.evidence}；{signal.evidence}".strip("；"),
                source=existing.source,
                message_id=existing.message_id,
            )

    present = set(unique.keys())
    non_weak = present & SIGNIFICANT_SIGNALS
    explanation: list[str] = []
    total = 0.0
    for signal_type, signal in unique.items():
        weight = SIGNAL_WEIGHTS.get(signal_type, 0.0)
        total += weight
        signal.weight = weight
        label = signal_type.label
        detail = f"{label}" + (f"（{signal.evidence}）" if signal.evidence else "")
        explanation.append(f"{detail} +{weight:g}")

    multiplier = 1.0
    for required, bonus, reason in COMBINATION_BONUSES:
        if required.issubset(present):
            multiplier *= bonus
            explanation.append(f"{reason} ×{bonus:g}")

    score = total * multiplier

    fast_path = False
    if FAST_PATH_REQUIRED.issubset(present) and (present & FAST_PATH_TRIGGERS):
        fast_path = True
        floor = limits.high
        if score < floor:
            score = floor
            explanation.append(f"本地快筛判定为高风险组合，直接抬升到 {floor:g} 分（不等待 AI）")

    weak_only = bool(present) and not non_weak
    if weak_only:
        # 只有弱信号：封顶在“可疑”阈值之下，避免正常费用讨论被误判。
        cap = max(0.0, limits.suspicious - 1)
        if score > cap:
            score = cap
            explanation.append("仅命中弱信号（金额/催促/图片），已封顶，不进入高风险等级")

    score = min(100.0, round(score, 2))
    level = limits.level_for(score)
    should_review = level in (FraudRiskLevel.HIGH, FraudRiskLevel.CRITICAL)

    if not present:
        explanation.append("未命中任何缴费诈骗特征")

    return FraudResult(
        score=score,
        level=level,
        signals=list(unique.values()),
        explanation=explanation,
        should_review=should_review,
        fast_path=fast_path,
        thresholds=limits,
    )


def format_amounts(amounts: list) -> str:
    """金额列表的展示文本。"""
    parts = []
    for amount in amounts:
        value = amount.amount if isinstance(amount.amount, Decimal) else Decimal(str(amount.amount))
        parts.append(f"{amount.currency} {value}")
    return "、".join(parts)
