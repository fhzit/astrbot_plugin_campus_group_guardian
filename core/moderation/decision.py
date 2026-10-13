# -*- coding: utf-8 -*-
"""内容审核决策模型（平台无关）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..actions.models import GuardianAction, GuardianActionType
from ..moderation.matcher import Match


class ModerationVerdict(Enum):
    """审核结论。"""

    PASS = "pass"
    VIOLATION = "violation"
    NEEDS_REVIEW = "needs_review"
    SKIPPED = "skipped"


#: 分类中文名（与原插件类别保持兼容，可继续扩展）。
CATEGORY_LABELS = {
    "advertisement": "广告",
    "url": "违规网址",
    "abuse": "辱骂",
    "porn": "色情",
    "violence": "暴恐",
    "weapon": "危险物品",
    "privacy": "隐私侵犯",
    "scam": "诈骗",
    "political": "政治敏感",
    "other": "其他违规",
    "default": "违规",
}


def category_label(category: str | None) -> str:
    if not category:
        return "违规"
    return CATEGORY_LABELS.get(category, category)


@dataclass
class ModerationDecision:
    """统一审核决策（§101）。"""

    verdict: ModerationVerdict
    category: str | None = None
    score: float | None = None
    reason: str = ""
    actions: list[GuardianAction] = field(default_factory=list)
    matches: list[Match] = field(default_factory=list)
    normalized_text: str = ""
    signals: list[str] = field(default_factory=list)
    source: str = "local"  # local | ai | full_ai

    @property
    def is_violation(self) -> bool:
        return self.verdict is ModerationVerdict.VIOLATION

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict.value,
            "category": self.category,
            "category_label": category_label(self.category),
            "score": self.score,
            "reason": self.reason,
            "actions": [action.action.value for action in self.actions],
            "matches": [match.to_dict() for match in self.matches[:20]],
            "signals": list(self.signals),
            "source": self.source,
        }


def build_default_actions(
    *,
    recall: bool = True,
    warn: bool = True,
    mute_seconds: int = 0,
) -> list[GuardianAction]:
    """构造默认处罚动作序列。"""
    actions: list[GuardianAction] = []
    if recall:
        actions.append(GuardianAction(action=GuardianActionType.RECALL, reason="违规内容"))
    if mute_seconds > 0:
        actions.append(
            GuardianAction(action=GuardianActionType.MUTE, duration=mute_seconds, reason="违规内容")
        )
    if warn:
        actions.append(GuardianAction(action=GuardianActionType.WARN, reason="违规内容"))
    return actions
