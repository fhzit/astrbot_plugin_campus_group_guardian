# -*- coding: utf-8 -*-
"""家校反诈模块的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..models import GroupKey, GroupRole, PaymentAmount


class FraudRiskLevel(Enum):
    """风险等级。等级阈值可配置。"""

    SAFE = "safe"
    SUSPICIOUS = "suspicious"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _LEVEL_RANK[self]

    @property
    def label(self) -> str:
        return _LEVEL_LABELS[self]

    def __ge__(self, other: object) -> bool:
        if isinstance(other, FraudRiskLevel):
            return self.rank >= other.rank
        return NotImplemented

    def __gt__(self, other: object) -> bool:
        if isinstance(other, FraudRiskLevel):
            return self.rank > other.rank
        return NotImplemented

    def __le__(self, other: object) -> bool:
        if isinstance(other, FraudRiskLevel):
            return self.rank <= other.rank
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, FraudRiskLevel):
            return self.rank < other.rank
        return NotImplemented


_LEVEL_RANK = {
    FraudRiskLevel.SAFE: 0,
    FraudRiskLevel.SUSPICIOUS: 1,
    FraudRiskLevel.HIGH: 2,
    FraudRiskLevel.CRITICAL: 3,
}

_LEVEL_LABELS = {
    FraudRiskLevel.SAFE: "安全",
    FraudRiskLevel.SUSPICIOUS: "可疑",
    FraudRiskLevel.HIGH: "高风险",
    FraudRiskLevel.CRITICAL: "极高风险",
}


class FraudSignalType(Enum):
    """反诈风险信号类型。"""

    PAYMENT_INTENT = "payment_intent"
    AMOUNT = "amount"
    PAYMENT_ACTION = "payment_action"
    STUDENT_NAME_REQUEST = "student_name_request"
    PAYMENT_SCREENSHOT_REQUEST = "payment_screenshot_request"
    URGENCY = "urgency"
    TEACHER_IMPERSONATION = "teacher_impersonation"
    PAYMENT_IMAGE = "payment_image"
    OCR_PAYMENT_TEXT = "ocr_payment_text"
    AI_PAYMENT_RISK = "ai_payment_risk"
    MULTI_MESSAGE_PATTERN = "multi_message_pattern"

    @property
    def label(self) -> str:
        return SIGNAL_LABELS[self]


SIGNAL_LABELS: dict[FraudSignalType, str] = {
    FraudSignalType.PAYMENT_INTENT: "收费语义",
    FraudSignalType.AMOUNT: "金额",
    FraudSignalType.PAYMENT_ACTION: "付款/扫码动作",
    FraudSignalType.STUDENT_NAME_REQUEST: "索要学生姓名备注",
    FraudSignalType.PAYMENT_SCREENSHOT_REQUEST: "索要付款截图",
    FraudSignalType.URGENCY: "催促（弱信号）",
    FraudSignalType.TEACHER_IMPERSONATION: "教师身份冒充",
    FraudSignalType.PAYMENT_IMAGE: "含付款类图片",
    FraudSignalType.OCR_PAYMENT_TEXT: "OCR 识别到付款信息",
    FraudSignalType.AI_PAYMENT_RISK: "AI 判定存在付款诱导",
    FraudSignalType.MULTI_MESSAGE_PATTERN: "多消息组合诈骗模式",
}


class SignalSource(Enum):
    """信号来源，用于日志解释与人工复核。"""

    TEXT = "text"
    OCR = "ocr"
    IMAGE = "image"
    AI = "ai"
    CONTEXT = "context"


@dataclass
class FraudSignal:
    """单个风险信号。"""

    type: FraudSignalType
    evidence: str = ""
    source: SignalSource = SignalSource.TEXT
    message_id: str = ""
    weight: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "type": self.type.value,
            "label": self.type.label,
            "evidence": self.evidence,
            "source": self.source.value,
            "message_id": self.message_id,
            "weight": round(self.weight, 2),
        }


@dataclass
class FraudThresholds:
    """风险阈值。"""

    suspicious: float = 30.0
    high: float = 60.0
    critical: float = 85.0

    def level_for(self, score: float) -> FraudRiskLevel:
        if score >= self.critical:
            return FraudRiskLevel.CRITICAL
        if score >= self.high:
            return FraudRiskLevel.HIGH
        if score >= self.suspicious:
            return FraudRiskLevel.SUSPICIOUS
        return FraudRiskLevel.SAFE


@dataclass
class FraudResult:
    """反诈判定结果。"""

    score: float = 0.0
    level: FraudRiskLevel = FraudRiskLevel.SAFE
    signals: list[FraudSignal] = field(default_factory=list)
    explanation: list[str] = field(default_factory=list)
    should_review: bool = False
    bypassed: bool = False
    bypass_reason: str = ""
    amounts: list[PaymentAmount] = field(default_factory=list)
    ocr_text: str = ""
    fast_path: bool = False
    thresholds: FraudThresholds = field(default_factory=FraudThresholds)

    @property
    def is_risky(self) -> bool:
        return self.level in (FraudRiskLevel.HIGH, FraudRiskLevel.CRITICAL)

    def signal_types(self) -> set[FraudSignalType]:
        return {signal.type for signal in self.signals}

    def to_dict(self) -> dict[str, object]:
        return {
            "score": round(self.score, 2),
            "level": self.level.value,
            "level_label": self.level.label,
            "signals": [signal.to_dict() for signal in self.signals],
            "explanation": list(self.explanation),
            "should_review": self.should_review,
            "bypassed": self.bypassed,
            "bypass_reason": self.bypass_reason,
            "amounts": [{"currency": a.currency, "amount": str(a.amount), "raw": a.raw} for a in self.amounts],
            "ocr_text": self.ocr_text,
            "fast_path": self.fast_path,
        }


@dataclass
class ContextEntry:
    """反诈上下文中的一条消息（仅内存，不落库）。"""

    message_id: str
    sender_id: str
    text: str
    timestamp: datetime
    signals: list[FraudSignal] = field(default_factory=list)
    has_image: bool = False


class IncidentStatus(Enum):
    """反诈事件状态。"""

    OPEN = "open"
    CONFIRMED_FRAUD = "confirmed_fraud"
    FALSE_POSITIVE = "false_positive"
    RESOLVED = "resolved"

    @property
    def label(self) -> str:
        return INCIDENT_STATUS_LABELS[self]


INCIDENT_STATUS_LABELS: dict[IncidentStatus, str] = {
    IncidentStatus.OPEN: "待处理",
    IncidentStatus.CONFIRMED_FRAUD: "确认诈骗",
    IncidentStatus.FALSE_POSITIVE: "误判",
    IncidentStatus.RESOLVED: "已处理",
}


@dataclass
class AntiFraudIncident:
    """反诈事件记录（§130）。"""

    platform: str
    adapter_id: str
    group_id: str
    message_id: str
    sender_id: str
    sender_name: str | None
    sender_role: GroupRole
    risk_score: float
    risk_level: FraudRiskLevel
    message_excerpt: str
    ocr_excerpt: str = ""
    signals: list[FraudSignal] = field(default_factory=list)
    status: IncidentStatus = IncidentStatus.OPEN
    created_at: datetime | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    actions: list[dict[str, object]] = field(default_factory=list)
    incident_id: int | None = None

    @property
    def group_key(self) -> GroupKey:
        return GroupKey.from_parts(self.platform, self.adapter_id, self.group_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.incident_id,
            "platform": self.platform,
            "adapter_id": self.adapter_id,
            "group_id": self.group_id,
            "message_id": self.message_id,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "sender_role": self.sender_role.value,
            "risk_score": round(self.risk_score, 2),
            "risk_level": self.risk_level.value,
            "risk_level_label": self.risk_level.label,
            "message_excerpt": self.message_excerpt,
            "ocr_excerpt": self.ocr_excerpt,
            "signals": [signal.to_dict() for signal in self.signals],
            "status": self.status.value,
            "status_label": self.status.label,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_by": self.resolved_by,
            "actions": list(self.actions),
        }
