# -*- coding: utf-8 -*-
"""家校反诈词表与金额/身份匹配规则。

词表刻意保持小规模、可解释：命中即可在事件详情里说明“匹配到了什么”。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from ..models import PaymentAmount

# 收费语义（§54）
PAYMENT_INTENT_TERMS: tuple[str, ...] = (
    "收费",
    "缴费",
    "交费",
    "学费",
    "资料费",
    "教材费",
    "书费",
    "书本费",
    "班费",
    "校服费",
    "校服",
    "住宿费",
    "伙食费",
    "餐费",
    "报名费",
    "课后服务费",
    "服务费",
    "学杂费",
    "补课费",
    "试卷费",
    "印刷费",
    "保险费",
    "活动费",
    "研学费",
    "社会实践费",
    "代收",
    "统一收取",
    "统一购买",
    "统一订购",
    "统一收费",
    "购买资料",
    "买资料",
    "订购资料",
    "征订",
    "收取费用",
    "费用收取",
    "收费标准",
)

# 付款动作（§55）
PAYMENT_ACTION_TERMS: tuple[str, ...] = (
    "扫码",
    "扫一扫",
    "扫描二维码",
    "二维码",
    "收款码",
    "付款码",
    "收付款",
    "付款",
    "支付",
    "转账",
    "收款",
    "缴纳",
    "汇款",
    "打款",
    "发红包",
    "红包",
    "转给我",
    "转到",
    "交到",
    "扫这个码",
    "付款方式",
)

# 身份备注要求（§56）
STUDENT_NAME_REQUEST_TERMS: tuple[str, ...] = (
    "备注学生姓名",
    "备注孩子姓名",
    "备注姓名",
    "备注班级",
    "备注学生",
    "备注孩子",
    "备注一下姓名",
    "填写学生姓名",
    "填写孩子姓名",
    "写上学生姓名",
    "注明学生姓名",
    "注明姓名",
    "标上学生姓名",
    "留下学生姓名",
    "备注时写上",
)

# 付款后反馈（§57）
SCREENSHOT_REQUEST_TERMS: tuple[str, ...] = (
    "付款后截图",
    "付款完成后截图",
    "完成后截图",
    "付款完截图",
    "交完后截图",
    "付款截图",
    "支付截图",
    "缴费截图",
    "交费截图",
    "转账截图",
    "截图接龙",
    "截图发群",
    "截图发群里",
    "截图给老师",
    "截图私发",
    "交完回复",
    "收到回复",
    "付完回复",
    "付款后回复",
)

# 催促（弱信号，§58）
URGENCY_TERMS: tuple[str, ...] = (
    "尽快",
    "抓紧",
    "马上",
    "立即",
    "今天完成",
    "今日完成",
    "尚未缴费",
    "还没交的",
    "还没交费",
    "最后期限",
    "截止今天",
    "逾期",
    "速交",
    "着急",
    "尽快交",
)

# 教师/校方身份词（§60，仅作 Risk Signal）
TEACHER_TITLE_TERMS: tuple[str, ...] = (
    "老师",
    "班主任",
    "辅导员",
    "年级主任",
    "教务处",
    "财务老师",
    "主任",
    "校长",
    "家委会",
    "班长",
    "学习委员",
)

# 二维码/收款类图片描述词
QRCODE_IMAGE_TERMS: tuple[str, ...] = ("二维码", "收款码", "付款码", "扫码", "收款", "转账")

_CURRENCY_SYMBOL_RE = re.compile(r"[¥￥]\s*(\d{1,7}(?:\.\d{1,2})?)")
_YUAN_RE = re.compile(r"(\d{1,7}(?:\.\d{1,2})?)\s*(?:元|块钱|圆)")
_PER_PERSON_RE = re.compile(r"每(?:位|人|个|名)\s*[学生孩子家长]?\s*(\d{1,7}(?:\.\d{1,2})?)")
_BARE_NUMBER_RE = re.compile(r"(?<![0-9.])(\d{2,5})(?:\.\d{1,2})?(?![0-9])")
#: 明显的非金额数字，避免把时间/页码当成金额。
_NON_AMOUNT_CONTEXT = ("页", "点", "分", "秒", "楼", "题", "第", "号", "班", "年级", "座位")

_TOKEN_SPLIT_RE = re.compile(r"[\s,，。.、;；:：!！?？~～\-—_/\\()（）\[\]【】\"'“”‘’]+")


def find_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    """返回文本中命中的词（去重，保持词表顺序）。"""
    haystack = str(text or "")
    if not haystack:
        return []
    lowered = haystack.casefold()
    hits: list[str] = []
    for term in terms:
        if term.casefold() in lowered:
            hits.append(term)
    return hits


def extract_amounts(text: str, *, allow_bare: bool = False) -> list[PaymentAmount]:
    """提取金额。

    Args:
        text: 待分析文本。
        allow_bare: 是否允许在收费语境下把裸数字当作金额
            （如“每位495”之外的“校服500左右”）。

    Returns:
        提取到的金额列表（按出现顺序，去重）。
    """
    haystack = str(text or "")
    if not haystack:
        return []
    results: list[PaymentAmount] = []
    seen: set[str] = set()

    def add(raw_value: str, raw_text: str, currency: str = "CNY") -> None:
        try:
            amount = Decimal(raw_value)
        except (InvalidOperation, ValueError):
            return
        if amount <= 0 or amount > Decimal("200000"):
            return
        key = f"{currency}:{amount}"
        if key in seen:
            return
        seen.add(key)
        results.append(PaymentAmount(currency=currency, amount=amount, raw=raw_text))

    for match in _CURRENCY_SYMBOL_RE.finditer(haystack):
        add(match.group(1), match.group(0))
    for match in _YUAN_RE.finditer(haystack):
        add(match.group(1), match.group(0))
    for match in _PER_PERSON_RE.finditer(haystack):
        add(match.group(1), match.group(0))
    if allow_bare:
        for match in _BARE_NUMBER_RE.finditer(haystack):
            start, end = match.span(1)
            window = haystack[max(0, start - 2): min(len(haystack), end + 2)]
            if any(marker in window for marker in _NON_AMOUNT_CONTEXT):
                continue
            add(match.group(1), match.group(0))
    return results


def teacher_name_matches(sender_name: str | None, teacher_names: list[str] | tuple[str, ...]) -> str | None:
    """发送者昵称/群名片是否高度匹配已配置的教师姓名。

    Returns:
        命中的教师姓名，未命中返回 None。
    """
    if not sender_name or not teacher_names:
        return None
    name = str(sender_name).strip()
    if not name:
        return None
    compact = re.sub(r"[\s]+", "", name).casefold()
    for teacher in teacher_names:
        candidate = re.sub(r"[\s]+", "", str(teacher or "")).casefold()
        if not candidate:
            continue
        if candidate == compact:
            return teacher
        # 昵称通常是“班主任张老师”“张老师（三年二班）”，包含姓名即视为高度匹配。
        if len(candidate) >= 2 and candidate in compact:
            return teacher
    return None
