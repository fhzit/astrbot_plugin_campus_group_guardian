# -*- coding: utf-8 -*-
"""AI 审核 Provider：统一通过 AstrBot 已配置的 LLM Provider 调用。

设计要点（§46、§47、§48、§80-§82）：
- 不维护第二套 API Key，只接受 AstrBot Provider ID；
- 要求结构化 JSON，解析失败一律不判违规；
- LLM 只输出信号，不决定最终动作。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from astrbot.api import logger

from ..core.antifraud.models import FraudSignal, FraudSignalType, SignalSource
from ..prompts.fraud import FRAUD_REVIEW_PROMPT_TEMPLATE, FRAUD_REVIEW_SYSTEM_PROMPT

_LOG = "[Guardian][AI]"

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_structured_json(raw: str) -> dict[str, Any] | None:
    """从模型输出里解析 JSON。

    容忍 Markdown 代码块与前后解释性文字；解析失败返回 None（调用方必须视为“未判定”）。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    candidates = [text]
    match = _JSON_BLOCK_RE.search(text)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


class AiReviewer:
    """通用 AI 复核器。"""

    def __init__(
        self,
        context: Any,
        provider_id: str,
        *,
        timeout: float = 20.0,
        max_concurrency: int = 2,
        cache_ttl_seconds: int = 300,
    ) -> None:
        self.context = context
        self.provider_id = str(provider_id or "")
        self.timeout = float(timeout)
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))
        self.cache_ttl_seconds = int(cache_ttl_seconds)
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self.stats: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return self.context is not None and bool(self.provider_id)

    async def review_json(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        image_urls: list[str] | None = None,
        cache_key: str | None = None,
    ) -> dict[str, Any] | None:
        """调用 LLM 并返回结构化结果；任何失败都返回 None。"""
        if not self.enabled:
            return None
        if cache_key:
            cached = self._cache.get(cache_key)
            if cached and (time.monotonic() - cached[0]) < self.cache_ttl_seconds:
                return cached[1]
        self.stats["call_count"] = self.stats.get("call_count", 0) + 1
        try:
            async with self._semaphore:
                response = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=self.provider_id,
                        prompt=prompt,
                        system_prompt=system_prompt,
                        image_urls=image_urls,
                    ),
                    timeout=self.timeout,
                )
            parsed = parse_structured_json(str(getattr(response, "completion_text", "") or ""))
            if parsed is None:
                self.stats["parse_error_count"] = self.stats.get("parse_error_count", 0) + 1
            else:
                self.stats["success_count"] = self.stats.get("success_count", 0) + 1
            if cache_key:
                self._cache[cache_key] = (time.monotonic(), parsed)
            return parsed
        except asyncio.TimeoutError:
            self.stats["error_count"] = self.stats.get("error_count", 0) + 1
            logger.debug(f"{_LOG} AI 复核超时（{self.timeout}s）")
            return None
        except Exception as exc:
            self.stats["error_count"] = self.stats.get("error_count", 0) + 1
            logger.debug(f"{_LOG} AI 复核失败: {exc}")
            return None

    def prune_cache(self) -> int:
        now = time.monotonic()
        stale = [key for key, (stamp, _) in self._cache.items() if (now - stamp) >= self.cache_ttl_seconds]
        for key in stale:
            del self._cache[key]
        return len(stale)

    def stats_snapshot(self) -> dict[str, int]:
        return {
            "call_count": self.stats.get("call_count", 0),
            "success_count": self.stats.get("success_count", 0),
            "error_count": self.stats.get("error_count", 0),
            "parse_error_count": self.stats.get("parse_error_count", 0),
            "cache_size": len(self._cache),
        }


class FraudAiReviewer(AiReviewer):
    """家校反诈 AI 复核：只在本地规则不确定时调用（§80）。"""

    async def review(
        self,
        *,
        text: str,
        ocr_text: str = "",
        context_messages: list[str] | None = None,
        sender_role: str = "member",
        local_signals: list[str] | None = None,
        cache_key: str | None = None,
    ) -> tuple[FraudSignal | None, dict[str, Any] | None]:
        """返回 (AI 风险信号, 原始判定字典)。

        只有模型明确给出 fraud/suspicious 且置信度达到阈值时才产出信号；
        解析失败、模型报错、置信度过低都不产生信号（不得因此判定违规）。
        """
        if not self.enabled:
            return None, None
        context_text = "\n".join(f"- {line}" for line in (context_messages or [])[:10]) or "（无）"
        prompt = FRAUD_REVIEW_PROMPT_TEMPLATE.format(
            sender_role=sender_role,
            text=text or "（无文本）",
            ocr_text=ocr_text or "（无）",
            context=context_text,
            local_signals="、".join(local_signals or []) or "（无）",
        )
        verdict = await self.review_json(
            prompt,
            system_prompt=FRAUD_REVIEW_SYSTEM_PROMPT,
            cache_key=cache_key,
        )
        if not verdict:
            return None, None
        label = str(verdict.get("verdict", "")).strip().lower()
        try:
            confidence = float(verdict.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if label not in {"fraud", "suspicious"} or confidence < 0.5:
            return None, verdict
        reason = str(verdict.get("reason") or "").strip() or "AI 判定存在付款诱导"
        return (
            FraudSignal(
                type=FraudSignalType.AI_PAYMENT_RISK,
                evidence=f"{reason}（置信度 {confidence:.2f}）",
                source=SignalSource.AI,
            ),
            verdict,
        )
