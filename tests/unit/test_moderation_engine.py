# -*- coding: utf-8 -*-
"""文本标准化与内容审核测试（规格 §40-§43、§96、§120、§146-§148、§152）。"""

from __future__ import annotations

from astrbot_plugin_campus_group_guardian.core.models import GroupRole
from astrbot_plugin_campus_group_guardian.core.moderation.decision import ModerationVerdict
from astrbot_plugin_campus_group_guardian.core.moderation.engine import (
    ContentModerationEngine,
    ModerationConfig,
)
from astrbot_plugin_campus_group_guardian.core.moderation.matcher import RegexRule, RuleMatcher
from astrbot_plugin_campus_group_guardian.core.moderation.normalize import normalize_for_matching

KEYWORDS = [("代开发票", "advertisement"), ("菠菜", "advertisement"), ("傻逼", "abuse")]


def build_engine() -> ContentModerationEngine:
    rules = [RegexRule(pattern=r"\d{5,}群", category="advertisement", rule_id=1)]
    rules[0].compile()
    return ContentModerationEngine(RuleMatcher(KEYWORDS, rules), None)


async def test_keyword_ac_match(message_factory):
    engine = build_engine()
    decision = await engine.analyze(
        message_factory("有需要代开发票的联系我"), ModerationConfig(enabled=True)
    )
    assert decision.is_violation is True
    assert decision.category == "advertisement"
    assert "代开发票" in " ".join(match.keyword for match in decision.matches)


async def test_zero_width_obfuscation_is_normalized(message_factory):
    """§42：零宽字符混淆后仍要能命中，同时保留原文。"""
    engine = build_engine()
    decision = await engine.analyze(
        message_factory("代开\u200b发票，私聊"), ModerationConfig(enabled=True)
    )
    assert decision.is_violation is True
    assert decision.normalized_text == normalize_for_matching("代开\u200b发票，私聊")
    assert "\u200b" not in decision.normalized_text


async def test_full_width_and_spacing_obfuscation(message_factory):
    engine = build_engine()
    decision = await engine.analyze(
        message_factory("代 开 发 票"), ModerationConfig(enabled=True)
    )
    assert decision.is_violation is True


async def test_regex_rule_matches_ad_url(message_factory):
    engine = build_engine()
    decision = await engine.analyze(
        message_factory("速来 88123群 领取福利"), ModerationConfig(enabled=True)
    )
    assert decision.is_violation is True


async def test_admin_exempt_skips_moderation(message_factory):
    """§95：moderation_admin_exempt 只影响内容审核。"""
    engine = build_engine()
    decision = await engine.analyze(
        message_factory("代开发票", role=GroupRole.ADMIN),
        ModerationConfig(enabled=True, admin_exempt=True),
    )
    assert decision.verdict is ModerationVerdict.SKIPPED
    assert decision.is_violation is False


async def test_disabled_category_is_ignored(message_factory):
    engine = build_engine()
    decision = await engine.analyze(
        message_factory("代开发票"),
        ModerationConfig(enabled=True, enabled_categories={"abuse"}),
    )
    assert decision.is_violation is False


async def test_clean_message_passes(message_factory):
    engine = build_engine()
    decision = await engine.analyze(
        message_factory("老师，今天的作业是什么？"), ModerationConfig(enabled=True)
    )
    assert decision.verdict is ModerationVerdict.PASS
    assert decision.is_violation is False


def test_matcher_hot_rebuild():
    """§120：词库变更后 AC 自动机热重建，不需要重启插件。"""
    matcher = RuleMatcher([("广告", "advertisement")], [])
    assert matcher.search(normalize_for_matching("这是广告")) != []
    matcher.build([("代练", "advertisement")], [])
    assert matcher.search(normalize_for_matching("这是广告")) == []
    assert matcher.search(normalize_for_matching("专业代练")) != []


def test_matcher_scales_without_scanning_every_keyword():
    """§41/§184：匹配用 AC 自动机，单次扫描即可命中任意规模词库。"""
    keywords = [(f"敏感词{i}", "other") for i in range(20000)]
    matcher = RuleMatcher(keywords, [])
    assert matcher.keyword_count == 20000
    hits = {hit.keyword for hit in matcher.search(normalize_for_matching("这里包含敏感词19999"))}
    # AC 会命中所有“确实是文本子串”的词，这里必须包含目标词
    assert "敏感词19999" in hits
    assert "敏感词8888" not in hits


def test_normalize_preserves_original_text_contract():
    """§42：只做匹配用标准化，原始文本由上层保留。"""
    raw = "ＱＱ ８８１２３"
    normalized = normalize_for_matching(raw)
    assert raw != normalized
    assert "88" in normalized
