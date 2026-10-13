# -*- coding: utf-8 -*-
"""内容审核引擎（平台无关，§40、§94、§95）。

流程：标准化 → AC 初筛 / 正则 → （可选）LLM 二次判断 → 决策 + 动作。
管理员豁免（moderation_admin_exempt）只影响内容审核，与反诈模块的
“群主/管理员固定放行”是两个独立概念。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from ...providers.ai import AiReviewer
from ...prompts.moderation import MODERATION_PROMPT_TEMPLATE, MODERATION_SYSTEM_PROMPT
from ..models import GuardianMessage
from .decision import ModerationDecision, ModerationVerdict, build_default_actions
from .matcher import RuleMatcher
from .normalize import normalization_evidence, normalize_for_matching

_LOG = "[Guardian][Moderation]"


@dataclass
class ModerationConfig:
    """内容审核配置（可由群级覆盖）。"""

    enabled: bool = True
    admin_exempt: bool = False
    llm_verify: bool = True
    llm_always: bool = False
    llm_provider_id: str = ""
    context_messages: int = 30
    recall_on_violation: bool = True
    warn_on_violation: bool = True
    mute_seconds: int = 0
    enabled_categories: set[str] = field(default_factory=set)
    min_keyword_length: int = 2

    def category_enabled(self, category: str) -> bool:
        if not self.enabled_categories:
            return True
        return category in self.enabled_categories


class ContentModerationEngine:
    """内容审核引擎。规则热重建不需要重启插件（§120）。"""

    def __init__(self, matcher: RuleMatcher, ai_reviewer: AiReviewer | None = None) -> None:
        self.matcher = matcher
        self.ai_reviewer = ai_reviewer
        self.stats: dict[str, int] = {}

    def update_rules(self, keywords, regex_rules) -> None:
        """重建匹配器（词库变更后调用）。"""
        self.matcher.build(keywords, regex_rules)
        logger.info(
            f"{_LOG} 规则已重建：关键词 {self.matcher.keyword_count} 条，"
            f"正则 {self.matcher.regex_count} 条"
        )

    async def analyze(
        self,
        message: GuardianMessage,
        config: ModerationConfig,
        *,
        context_lines: list[str] | None = None,
    ) -> ModerationDecision:
        """审核一条消息。"""
        self.stats["scanned_count"] = self.stats.get("scanned_count", 0) + 1
        if not config.enabled:
            return ModerationDecision(verdict=ModerationVerdict.SKIPPED, reason="内容审核未开启")

        if config.admin_exempt and message.sender_role.is_privileged:
            return ModerationDecision(
                verdict=ModerationVerdict.SKIPPED,
                reason="管理员内容审核豁免已开启",
            )

        normalized = normalize_for_matching(message.text)
        matches = [
            match
            for match in self.matcher.search(normalized)
            if config.category_enabled(match.category) and len(match.keyword) >= config.min_keyword_length
        ]
        evidence = normalization_evidence(message.text)

        if not matches and not config.llm_always:
            return ModerationDecision(
                verdict=ModerationVerdict.PASS,
                normalized_text=normalized,
                signals=evidence,
                reason="本地规则未命中",
            )

        hits = "、".join(sorted({f"{m.keyword}({m.category})" for m in matches})) or "（无）"
        if config.llm_verify and self.ai_reviewer is not None and self.ai_reviewer.enabled:
            verdict = await self._llm_verify(
                message,
                config,
                hits=hits,
                context_lines=context_lines or [],
            )
            if verdict is not None:
                return verdict

        primary = self._primary_category(matches)
        self.stats["violation_count"] = self.stats.get("violation_count", 0) + 1
        reason = f"命中{len(matches)}条规则（{hits}）"
        return ModerationDecision(
            verdict=ModerationVerdict.VIOLATION,
            category=primary,
            reason=reason,
            actions=build_default_actions(
                recall=config.recall_on_violation,
                warn=config.warn_on_violation,
                mute_seconds=config.mute_seconds,
            ),
            matches=matches,
            normalized_text=normalized,
            signals=evidence,
        )

    async def _llm_verify(
        self,
        message: GuardianMessage,
        config: ModerationConfig,
        *,
        hits: str,
        context_lines: list[str],
    ) -> ModerationDecision | None:
        """LLM 二次判断。解析失败时返回 None，由调用方按本地规则处置。"""
        context_text = "\n".join(f"- {line}" for line in context_lines[: config.context_messages]) or "（无）"
        prompt = MODERATION_PROMPT_TEMPLATE.format(hits=hits, text=message.text, context=context_text)
        data = await self.ai_reviewer.review_json(
            prompt,
            system_prompt=MODERATION_SYSTEM_PROMPT,
            cache_key=f"mod:{message.message_id}:{message.text[:120]}",
        )
        if not data:
            self.stats["llm_fallback_count"] = self.stats.get("llm_fallback_count", 0) + 1
            return None
        verdict_label = str(data.get("verdict", "")).strip().lower()
        if verdict_label not in {"violation", "pass"}:
            return None
        if verdict_label == "pass":
            self.stats["llm_passed_count"] = self.stats.get("llm_passed_count", 0) + 1
            return ModerationDecision(
                verdict=ModerationVerdict.PASS,
                normalized_text=normalize_for_matching(message.text),
                reason=f"LLM 判断不违规：{data.get('reason', '')}",
                source="ai",
            )
        category = str(data.get("category") or "other")
        try:
            score = float(data.get("confidence") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        self.stats["violation_count"] = self.stats.get("violation_count", 0) + 1
        return ModerationDecision(
            verdict=ModerationVerdict.VIOLATION,
            category=category,
            score=score,
            reason=f"LLM 判定违规：{data.get('reason', '')}",
            actions=build_default_actions(
                recall=config.recall_on_violation,
                warn=config.warn_on_violation,
                mute_seconds=config.mute_seconds,
            ),
            normalized_text=normalize_for_matching(message.text),
            source="ai",
        )

    @staticmethod
    def _primary_category(matches) -> str:
        """取优先级最高的分类（越靠前越严重）。"""
        priority = [
            "porn",
            "violence",
            "weapon",
            "political",
            "scam",
            "advertisement",
            "url",
            "abuse",
            "privacy",
        ]
        found = {match.category for match in matches}
        for category in priority:
            if category in found:
                return category
        return next(iter(found), "other")


def build_moderation_engine(matcher: RuleMatcher, ai_reviewer: AiReviewer | None) -> ContentModerationEngine:
    return ContentModerationEngine(matcher, ai_reviewer)


def moderation_config_from(raw: dict[str, Any]) -> ModerationConfig:
    """从（可能被群级覆盖的）配置字典构造 ModerationConfig。"""
    return ModerationConfig(
        enabled=bool(raw.get("enabled", True)),
        admin_exempt=bool(raw.get("admin_exempt", False)),
        llm_verify=bool(raw.get("llm_verify", True)),
        llm_always=bool(raw.get("llm_always", False)),
        llm_provider_id=str(raw.get("llm_provider_id", "") or ""),
        context_messages=int(raw.get("context_messages", 30) or 30),
        recall_on_violation=bool(raw.get("recall_on_violation", True)),
        warn_on_violation=bool(raw.get("warn_on_violation", True)),
        mute_seconds=int(raw.get("mute_seconds", 0) or 0),
        enabled_categories=set(raw.get("enabled_categories") or []),
    )
