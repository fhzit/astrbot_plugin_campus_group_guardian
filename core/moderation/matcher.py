# -*- coding: utf-8 -*-
"""本地匹配层：Aho-Corasick 多关键词匹配 + 受限正则回退（§41、§184）。

为什么不逐条遍历关键词：6~7 万条词库逐条 ``in`` 会让事件循环明显阻塞。
AC 自动机单次扫描是 O(n + 命中数)，与词库规模无关。

纯 Python 实现（默认），装了 pyahocorasick 时可切换。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

#: 单条文本参与正则匹配的最大长度，避免回溯爆炸。
MAX_REGEX_INPUT_CHARS = 5000


@dataclass
class Match:
    """一次词库/正则命中。"""

    keyword: str
    category: str = "default"
    source: str = "keyword"  # keyword | regex
    start: int = 0
    end: int = 0
    rule_id: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "keyword": self.keyword,
            "category": self.category,
            "source": self.source,
            "start": self.start,
            "end": self.end,
            "rule_id": self.rule_id,
        }


@dataclass
class RegexRule:
    """正则规则。"""

    pattern: str
    category: str = "default"
    rule_id: int | None = None
    description: str = ""
    _compiled: re.Pattern | None = field(default=None, repr=False)

    def compile(self) -> re.Pattern | None:
        try:
            self._compiled = re.compile(self.pattern, re.IGNORECASE)
        except re.error:
            self._compiled = None
        return self._compiled


class AhoCorasick:
    """朴素的 AC 自动机实现。

    节点结构：``goto`` 字典 + ``fail`` 指针 + ``output``（在该节点结束的模式列表）。
    """

    def __init__(self, patterns: Iterable[tuple[str, str]] = ()) -> None:
        self._goto: list[dict[str, int]] = [{}]
        self._fail: list[int] = [0]
        self._output: list[list[tuple[str, str]]] = [[]]
        self._built = False
        self._count = 0
        if patterns:
            self.build(patterns)

    def build(self, patterns: Iterable[tuple[str, str]]) -> None:
        """（重新）构建自动机。规则热更新后调用，无需重启插件。"""
        self._goto = [{}]
        self._fail = [0]
        self._output = [[]]
        self._count = 0
        for keyword, category in patterns:
            if not keyword:
                continue
            node = 0
            for char in keyword:
                nxt = self._goto[node].get(char)
                if nxt is None:
                    self._goto.append({})
                    self._fail.append(0)
                    self._output.append([])
                    nxt = len(self._goto) - 1
                    self._goto[node][char] = nxt
                node = nxt
            self._output[node].append((keyword, category))
            self._count += 1
        self._build_fail_links()
        self._built = True

    def _build_fail_links(self) -> None:
        queue: list[int] = []
        for node in self._goto[0].values():
            self._fail[node] = 0
            queue.append(node)
        while queue:
            current = queue.pop(0)
            for char, nxt in self._goto[current].items():
                queue.append(nxt)
                fallback = self._fail[current]
                while fallback and char not in self._goto[fallback]:
                    fallback = self._fail[fallback]
                self._fail[nxt] = self._goto[fallback].get(char, 0)
                if self._fail[nxt] == nxt:
                    self._fail[nxt] = 0
                self._output[nxt].extend(self._output[self._fail[nxt]])

    @property
    def pattern_count(self) -> int:
        return self._count

    @property
    def built(self) -> bool:
        return self._built

    def search(self, text: str) -> list[Match]:
        """扫描文本，返回所有命中的模式。"""
        if not self._built or not text:
            return []
        matches: list[Match] = []
        node = 0
        for index, char in enumerate(text):
            while node and char not in self._goto[node]:
                node = self._fail[node]
            node = self._goto[node].get(char, 0)
            for keyword, category in self._output[node]:
                start = index - len(keyword) + 1
                matches.append(
                    Match(keyword=keyword, category=category, start=start, end=index + 1)
                )
        return matches


class RuleMatcher:
    """词库 + 正则的统一匹配入口。"""

    def __init__(
        self,
        keywords: Iterable[tuple[str, str]] = (),
        regex_rules: Iterable[RegexRule] = (),
    ) -> None:
        self._automaton = AhoCorasick()
        self._pyahocorasick_enabled = False
        self._pyahocorasick_automaton = None
        self._keyword_items: list[tuple[str, str]] = []
        self._regex_rules: list[RegexRule] = []
        self.build(keywords, regex_rules)

    def build(
        self,
        keywords: Iterable[tuple[str, str]] = (),
        regex_rules: Iterable[RegexRule] = (),
    ) -> None:
        """重建匹配器（规则变更后热重建）。"""
        self._keyword_items = [(str(word), str(category)) for word, category in keywords if word]
        self._automaton.build(self._keyword_items)
        self._regex_rules = []
        for rule in regex_rules:
            if rule._compiled is None and rule.compile() is None:
                continue
            self._regex_rules.append(rule)
        if self._pyahocorasick_enabled:
            self.configure_pyahocorasick(True)

    def configure_pyahocorasick(self, enabled: bool) -> bool:
        """显式开关可选 pyahocorasick 后端（默认关闭，未安装时仍用纯 Python）。

        返回实际是否启用；每次词库重建都会重建扩展自动机，避免热更新后后端陈旧。
        """
        self._pyahocorasick_enabled = bool(enabled)
        self._pyahocorasick_automaton = None
        if not self._pyahocorasick_enabled:
            return False
        try:
            import ahocorasick  # type: ignore[import-not-found]
        except ImportError:
            return False
        automaton = ahocorasick.Automaton()
        for keyword, category in self._keyword_items:
            if keyword:
                automaton.add_word(keyword, (keyword, category))
        automaton.make_automaton()
        self._pyahocorasick_automaton = automaton
        return True

    @property
    def keyword_count(self) -> int:
        return self._automaton.pattern_count

    @property
    def regex_count(self) -> int:
        return len(self._regex_rules)

    def search(self, text: str) -> list[Match]:
        """执行关键词 + 正则匹配。"""
        haystack = str(text or "")
        if not haystack:
            return []
        matches = self._automaton.search(haystack)
        if self._pyahocorasick_automaton is not None:
            matches = []
            for end_index, (keyword, category) in self._pyahocorasick_automaton.iter(haystack):
                start = end_index - len(keyword) + 1
                matches.append(Match(keyword=keyword, category=category, start=start, end=end_index + 1))
        else:
            matches = self._automaton.search(haystack)
        if self._regex_rules:
            truncated = haystack[:MAX_REGEX_INPUT_CHARS]
            for rule in self._regex_rules:
                compiled = rule._compiled
                if compiled is None:
                    continue
                for found in compiled.finditer(truncated):
                    matches.append(
                        Match(
                            keyword=found.group(0),
                            category=rule.category,
                            source="regex",
                            start=found.start(),
                            end=found.end(),
                            rule_id=rule.rule_id,
                        )
                    )
        return matches

    def matched_categories(self, text: str) -> set[str]:
        return {match.category for match in self.search(text)}
