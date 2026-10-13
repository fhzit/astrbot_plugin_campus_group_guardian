# -*- coding: utf-8 -*-
"""反诈相关命令的纯逻辑（供 main.py 的指令处理器调用）。"""

from __future__ import annotations

from typing import Any

from ..core.antifraud.detectors import detect_text_signals
from ..core.antifraud.models import FraudRiskLevel
from ..core.antifraud.risk_engine import score_signals
from ..core.models import GroupRole

#: /反诈 的参数 → 动作。
TOGGLE_WORDS = {
    "开启": True,
    "开": True,
    "on": True,
    "启用": True,
    "关闭": False,
    "关": False,
    "off": False,
    "停用": False,
}

STATUS_WORDS = {"状态", "status", "查看"}


def parse_toggle(word: str) -> bool | None:
    """解析开关参数。无法识别返回 None。"""
    return TOGGLE_WORDS.get(str(word or "").strip().lower())


def render_antifraud_status(snapshot: Any, stats: dict[str, Any] | None = None) -> str:
    """/反诈状态 文案。"""
    enabled = snapshot.get_bool("anti_fraud.enabled", True)
    lines = [
        "【家校反诈状态】",
        f"开关：{'已开启' if enabled else '已关闭'}",
        f"风险阈值：可疑 {snapshot.get_float('anti_fraud.risk_threshold_suspicious', 30):g}"
        f" / 高风险 {snapshot.get_float('anti_fraud.risk_threshold_high', 60):g}"
        f" / 极高 {snapshot.get_float('anti_fraud.risk_threshold_critical', 85):g}",
        f"上下文窗口：最近 {snapshot.get_int('anti_fraud.context_messages', 10)} 条"
        f" / {snapshot.get_int('anti_fraud.context_seconds', 300)} 秒",
        "高危处置："
        f"撤回={'开' if snapshot.get_bool('anti_fraud.auto_recall_critical', True) else '关'}"
        f"、临时禁言={'开' if snapshot.get_bool('anti_fraud.auto_mute_critical', True) else '关'}"
        f"、踢出={'开' if snapshot.get_bool('anti_fraud.auto_kick_critical', False) else '关'}"
        f"、拉黑={'开' if snapshot.get_bool('anti_fraud.auto_blacklist_critical', False) else '关'}",
        f"OCR：{'已开启' if snapshot.get_bool('ocr.enabled', True) else '已关闭'}"
        f"（策略 {snapshot.get_str('ocr.strategy', 'uapi_then_ai')}）",
        f"AI 复核：{'已开启' if snapshot.get_bool('ai.enabled', False) else '已关闭'}",
    ]
    if stats:
        lines.append(
            "扫描统计："
            f"扫描 {stats.get('scanned_count', 0)} 次，事件 "
            f"{stats.get('incidents_total', 0)} 条，待处理 {stats.get('incidents_open', 0)} 条"
        )
    return "\n".join(lines)


def render_guardian_status(snapshot: Any, platforms: list[dict[str, Any]] | None = None, stats: dict[str, Any] | None = None) -> str:
    """/守护状态 文案。"""
    lines = [
        "【群聊智能守护者状态】",
        f"插件总开关：{'已开启' if snapshot.get_bool('general.enabled', True) else '已关闭'}",
        f"内容审核：{'已开启' if snapshot.get_bool('moderation.enabled', True) else '已关闭'}"
        f"（AI 复核 {'开' if snapshot.get_bool('moderation.llm_verify', True) else '关'}）",
        f"反诈模块：{'已开启' if snapshot.get_bool('anti_fraud.enabled', True) else '已关闭'}",
    ]
    if platforms:
        lines.append("平台：")
        for item in platforms:
            caps = [row["label"] for row in item.get("capabilities", []) if row.get("supported")]
            lines.append(f"  - {item['platform']}：支持 {len(caps)} 项能力")
    if stats:
        lines.append(
            f"动作：成功 {stats.get('action_success', 0)} / 不支持 "
            f"{stats.get('action_unsupported', 0)} / 失败 {stats.get('action_failed', 0)}"
        )
    return "\n".join(lines)


def run_detector_test(
    text: str,
    *,
    sender_role: GroupRole = GroupRole.MEMBER,
    sender_name: str | None = None,
    teacher_names: list[str] | None = None,
    thresholds=None,
) -> dict[str, Any]:
    """只运行检测器，不执行任何动作（§155）。

    Returns:
        含信号、分数、等级与“预计动作”的字典。
    """
    from ..core.antifraud.models import FraudThresholds
    from ..core.antifraud.policy_bridge import plan_actions_for_level

    signals, amounts = detect_text_signals(
        text,
        sender_name=sender_name,
        teacher_names=teacher_names or [],
    )
    result = score_signals(
        signals,
        thresholds=thresholds or FraudThresholds(),
        sender_privileged=sender_role.is_privileged,
    )
    result.amounts = amounts
    planned = plan_actions_for_level(result.level, result.bypassed)
    return {
        "score": result.score,
        "level": result.level.value,
        "level_label": result.level.label,
        "bypassed": result.bypassed,
        "bypass_reason": result.bypass_reason,
        "signals": [signal.to_dict() for signal in result.signals],
        "amounts": [
            {"currency": item.currency, "amount": str(item.amount), "raw": item.raw} for item in amounts
        ],
        "explanation": list(result.explanation),
        "planned_actions": planned,
        "is_risky": result.level in (FraudRiskLevel.HIGH, FraudRiskLevel.CRITICAL),
    }


def render_test_result(payload: dict[str, Any]) -> str:
    """/反诈测试 的文本输出。"""
    lines = [
        "【反诈测试结果】（仅检测，不执行任何处置）",
        f"风险分：{payload['score']:g}/100（{payload['level_label']}）",
    ]
    if payload.get("bypassed"):
        lines.append("发送者角色为群主/管理员：按设计放行，不进入反诈判定。")
        return "\n".join(lines)
    if payload["signals"]:
        lines.append("命中信号：")
        for signal in payload["signals"]:
            lines.append(f"  ✓ {signal['label']}（{signal['evidence']}）")
    else:
        lines.append("命中信号：无")
    if payload["amounts"]:
        amounts = "、".join(f"{item['currency']}{item['amount']}" for item in payload["amounts"])
        lines.append(f"金额：{amounts}")
    if payload.get("planned_actions"):
        lines.append("预计动作：" + "、".join(payload["planned_actions"]))
    else:
        lines.append("预计动作：无")
    return "\n".join(lines)
