# -*- coding: utf-8 -*-
"""统计类命令文案。"""

from __future__ import annotations

from typing import Any


def render_overview(stats: dict[str, Any], ocr_stats: dict[str, Any] | None = None, ai_stats: dict[str, Any] | None = None) -> str:
    """总览统计文案。"""
    lines = [
        "【守护者统计】",
        f"审核日志：{stats.get('moderation_scanned', 0)} 条（其中已处置 {stats.get('moderation_violations', 0)} 条）",
        f"反诈事件：{stats.get('incidents_total', 0)} 条"
        f"（待处理 {stats.get('incidents_open', 0)}、确认诈骗 {stats.get('incidents_confirmed', 0)}、"
        f"误判 {stats.get('incidents_false_positive', 0)}）",
        f"动作执行：成功 {stats.get('action_success', 0)}、平台不支持 "
        f"{stats.get('action_unsupported', 0)}、失败 {stats.get('action_failed', 0)}",
    ]
    if ocr_stats:
        lines.append(
            f"OCR：调用 {ocr_stats.get('call_count', 0)} 次，成功 "
            f"{ocr_stats.get('success_count', 0)} 次，缓存命中 {ocr_stats.get('cache_hit_count', 0)} 次"
        )
    if ai_stats:
        lines.append(
            f"AI 审核：调用 {ai_stats.get('call_count', 0)} 次，成功 "
            f"{ai_stats.get('success_count', 0)} 次，解析失败 {ai_stats.get('parse_error_count', 0)} 次"
        )
    return "\n".join(lines)
