# -*- coding: utf-8 -*-
"""文本标准化（§42）。

标准化只用于匹配，原始文本始终保留：日志与事件详情里同时给出“原文”和“匹配到了什么”。

本模块组合两部分：
- 本文件新增的通用标准化 normalize_for_matching()；
- 从 astrbot_plugin_group_guardian（MIT, Copyright (c) 2025 zhaisir）移植的
  URL 混淆还原与 Base 编码解码，它们只产出证据，不替换原文。
"""

from __future__ import annotations

import re
import unicodedata

from .encoded import decode_base_evidence as decode_base_evidence
from .obfuscation import extract_obfuscated_url_evidence as extract_obfuscated_url_evidence
from .obfuscation import normalize_url_obfuscation as normalize_url_obfuscation

#: 零宽与格式控制字符（含 BOM、软连字符、蒙古文元音分隔符）。\u200b-\u200f、\ufeff、\u2060-\u2064
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\u00ad]")
#: 常见同形字符 → ASCII。用于让“付费”类规则不被形近字绕过。
_CONFUSABLES = {
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
    "￥": "¥", "﹩": "$", "Ａ": "A", "０": "0",
    "元": "元", "圆": "元",
}
#: 全角/半角与各类标点归一。
_PUNCT_MAP = {
    "，": ",", "。": ".", "；": ";", "：": ":", "！": "!", "？": "?",
    "（": "(", "）": ")", "【": "[", "】": "]", "《": "<", "》": ">",
    "　": " ", "、": ",", "～": "~", "—": "-", "－": "-", "．": ".", "／": "/",
}
#: 匹配前统一字符表：把区间数字等也要覆盖。
_WORD_CHARS = "0-9a-zA-Z"


def strip_invisible(text: str) -> str:
    """去掉零宽字符与格式控制符。"""
    return _INVISIBLE_RE.sub("", str(text or ""))


def fold_confusables(text: str) -> str:
    """同形字符归一（NFKC + 自定义映射）。"""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    return "".join(_CONFUSABLES.get(char, char) for char in normalized)


def unify_punctuation(text: str) -> str:
    """标点统一为半角，避免“收，费”这类插入绕过。"""
    return "".join(_PUNCT_MAP.get(char, char) for char in str(text or ""))


def collapse_separators(text: str) -> str:
    """压缩用于拆词的空白与分隔符。

    只做保守压缩：把“收 费”“收*费”这类在词内部插入的分隔去掉，
    但保留正常词间空格，避免把两个正常词粘成违规词。
    """
    value = str(text or "")
    # 单字符之间被空格/点/星号分隔的模式：收 费 -> 收费
    value = re.sub(
        r"(?<=[\u4e00-\u9fff])(?:\s+|[.\-*_·•])(?=[\u4e00-\u9fff])",
        "",
        value,
    )
    return value


def normalize_for_matching(text: str) -> str:
    """生成用于匹配的标准化文本。

    顺序：去零宽 → 同形归一 → 标点统一 → 分隔压缩 → 小写。

    Args:
        text: 原始文本。

    Returns:
        标准化文本（原文不被修改）。
    """
    value = strip_invisible(text)
    value = fold_confusables(value)
    value = unify_punctuation(value)
    value = collapse_separators(value)
    return value.strip().lower()


def normalization_evidence(text: str, *, limit: int = 6) -> list[str]:
    """给出“标准化做了什么”的可解释证据。

    Returns:
        形如 ``["零宽字符 3 处", "混淆 URL: http://a.com"]`` 的说明列表。
    """
    evidence: list[str] = []
    original = str(text or "")
    if not original:
        return evidence
    invisible = len(original) - len(strip_invisible(original))
    if invisible:
        evidence.append(f"零宽/控制字符 {invisible} 处")
    folded = fold_confusables(original)
    if folded != unicodedata.normalize("NFKC", original):
        evidence.append("同形字符已归一")
    obfuscated = extract_obfuscated_url_evidence(original, limit=limit)
    for url in obfuscated:
        evidence.append(f"混淆 URL: {url}")
    decoded = decode_base_evidence(original)
    if decoded:
        evidence.append("检测到 Base 系列编码内容")
    return evidence


__all__ = [
    "normalize_for_matching",
    "normalization_evidence",
    "strip_invisible",
    "fold_confusables",
    "unify_punctuation",
    "collapse_separators",
    "normalize_url_obfuscation",
    "extract_obfuscated_url_evidence",
    "decode_base_evidence",
]
