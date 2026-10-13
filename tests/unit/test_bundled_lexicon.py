# -*- coding: utf-8 -*-
"""原 GroupGuardian lexicon.db 兼容性测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from astrbot_plugin_campus_group_guardian.core.moderation.matcher import RegexRule, RuleMatcher
from astrbot_plugin_campus_group_guardian.main import CampusGroupGuardian

REFERENCE_LEXICON = Path("/opt/data/workspace/astrbot_plugin_group_guardian_ref/lexicon.db")
BUNDLED_LEXICON = Path(__file__).resolve().parents[2] / "lexicon.db"


def test_legacy_lexicon_is_bundled_byte_for_byte():
    """词库从原插件精确复制，不删词、不重编码、不改表。"""
    assert REFERENCE_LEXICON.is_file()
    assert BUNDLED_LEXICON.is_file()
    assert BUNDLED_LEXICON.read_bytes() == REFERENCE_LEXICON.read_bytes()


def test_bundled_lexicon_loads_with_legacy_categories_and_regex_cap():
    guardian = CampusGroupGuardian.__new__(CampusGroupGuardian)
    keywords, rules, skipped = guardian._read_lexicon_db(BUNDLED_LEXICON)

    assert len(keywords) == 66_993  # 短词保留，确保内置库与上游检索结果完整。
    assert len(rules) == 800
    assert skipped == 23_731
    categories = {category for _, category in keywords}
    assert {"advertisement", "url", "political", "porn", "violence", "weapon"} <= categories
    assert all(isinstance(rule, RegexRule) and rule._compiled is not None for rule in rules)

    matcher = RuleMatcher(keywords[:500], rules[:20])
    assert matcher.keyword_count == 500
    assert matcher.regex_count == 20
    # 上游库中的单字关键词不可被新插件过滤。
    short_terms = {word for word, _category in keywords if len(word) == 1}
    assert short_terms
    full_matcher = RuleMatcher(keywords, [])
    assert any(match.keyword in short_terms for match in full_matcher.search(next(iter(short_terms))))
    # 测试合成验证文本，不输出词库条目本身。
    assert matcher.search("这条广告包含代理关键词")


def test_lexicon_database_integrity_and_expected_tables():
    with sqlite3.connect(f"file:{BUNDLED_LEXICON}?mode=ro", uri=True) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"meta", "lexicon_categories", "lexicon_keywords", "moderation_rules"} <= tables
        assert conn.execute("SELECT count(*) FROM lexicon_keywords").fetchone()[0] == 66_993
        assert conn.execute(
            "SELECT count(*) FROM moderation_rules WHERE enabled = 1"
        ).fetchone()[0] == 24_531
