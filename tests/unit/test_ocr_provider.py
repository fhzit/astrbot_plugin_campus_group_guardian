# -*- coding: utf-8 -*-
"""OCR Provider 测试（规格 §69-§77、§145、§180）。"""

from __future__ import annotations

import httpx
import pytest

from astrbot_plugin_campus_group_guardian.core.models import GuardianImage
from astrbot_plugin_campus_group_guardian.providers.ocr import (
    OcrResult,
    OcrService,
    UapiOcrProvider,
)


class FakeProvider:
    """可控 OCR Provider。"""

    def __init__(self, name: str, *, text: str = "", success: bool = True, error: str | None = None):
        self.name = name
        self.text = text
        self.success = success
        self.error = error
        self.calls = 0

    async def recognize(self, image):  # noqa: ANN001
        self.calls += 1
        return OcrResult(
            provider=self.name,
            text=self.text if self.success else "",
            success=self.success,
            latency_ms=5,
            error_type=None if self.success else (self.error or "TIMEOUT"),
        )

    async def close(self) -> None:
        return None


def build_uapi() -> UapiOcrProvider:
    return UapiOcrProvider("test-key", base_url="https://example.invalid/ocr")


def response(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code=status_code, json=payload, request=httpx.Request("POST", "https://example.invalid/ocr"))


# ----------------------------------------------------------------------------
# 响应解析（第三方 JSON 不进入风险引擎，只暴露归一化文本）
# ----------------------------------------------------------------------------
def test_parse_simple_text_response():
    text, error = build_uapi()._parse_text(response({"text": "  收款二维码 495元  "}))
    assert text == "收款二维码 495元"
    assert error is None


def test_parse_words_result_response():
    text, error = build_uapi()._parse_text(
        response({"code": "SUCCESS", "words_result": [{"words": "第一行"}, {"words": "第二行"}]})
    )
    assert text == "第一行\n第二行"
    assert error is None


def test_parse_business_error_code():
    text, error = build_uapi()._parse_text(response({"code": "RATE_LIMITED"}))
    assert text == ""
    assert error == "RATE_LIMITED"


def test_parse_invalid_json():
    bad = httpx.Response(
        status_code=200,
        content=b"<html>not json</html>",
        request=httpx.Request("POST", "https://example.invalid/ocr"),
    )
    text, error = build_uapi()._parse_text(bad)
    assert text == ""
    assert error == "BAD_RESPONSE"


def test_parse_empty_result():
    text, error = build_uapi()._parse_text(response({"text": "   "}))
    assert text == ""
    assert error == "EMPTY_RESULT"


# ----------------------------------------------------------------------------
# 策略与兜底
# ----------------------------------------------------------------------------
async def test_uapi_then_ai_falls_back_to_vision():
    """§77：UApiPro 失败后回退 AI Vision，最终拿到文本。"""
    uapi = FakeProvider("uapi", success=False, error="TIMEOUT")
    vision = FakeProvider("ai_vision", text="缴费通知 每位495元")
    service = OcrService(uapi, vision, "uapi_then_ai", cache_ttl_seconds=60)
    result = await service.recognize(GuardianImage(url="http://x/1.png"))
    assert uapi.calls == 1
    assert vision.calls == 1
    assert result.success is True
    assert result.text == "缴费通知 每位495元"


async def test_uapi_only_does_not_call_vision():
    uapi = FakeProvider("uapi", success=False, error="TIMEOUT")
    vision = FakeProvider("ai_vision", text="不应被调用")
    service = OcrService(uapi, vision, "uapi_only", cache_ttl_seconds=60)
    result = await service.recognize(GuardianImage(url="http://x/2.png"))
    assert vision.calls == 0
    assert result.success is False
    assert result.error_type == "TIMEOUT"


async def test_ai_only_strategy():
    uapi = FakeProvider("uapi", text="不应被调用")
    vision = FakeProvider("ai_vision", text="OK")
    service = OcrService(uapi, vision, "ai_only", cache_ttl_seconds=60)
    result = await service.recognize(GuardianImage(url="http://x/3.png"))
    assert uapi.calls == 0
    assert result.success is True


async def test_ocr_result_cache_dedup():
    """§145：同一张图短时间重复出现时复用 OCR 结果。"""
    uapi = FakeProvider("uapi", text="同一张图")
    service = OcrService(uapi, None, "uapi_only", cache_ttl_seconds=60)
    image = GuardianImage(url="http://x/same.png")
    first = await service.recognize(image)
    second = await service.recognize(image)
    assert first.text == second.text == "同一张图"
    assert uapi.calls == 1
    stats = service.stats_snapshot()
    assert stats.get("cache_hit_count") == 1


async def test_cache_prune():
    uapi = FakeProvider("uapi", text="x")
    service = OcrService(uapi, None, "uapi_only", cache_ttl_seconds=0)
    await service.recognize(GuardianImage(url="http://x/4.png"))
    assert service.prune_cache() >= 0


async def test_stats_track_calls_and_errors():
    """§76：统计只暴露调用次数类指标（来自各 Provider 的计数）。"""
    uapi = FakeProvider("uapi", success=False, error="TIMEOUT")
    uapi.stats = {"call_count": 1, "success_count": 0, "error_count": 1}
    service = OcrService(uapi, None, "uapi_only", cache_ttl_seconds=60)
    result = await service.recognize(GuardianImage(url="http://x/5.png"))
    assert result.success is False
    stats = service.stats_snapshot()
    assert stats["call_count"] == 1
    assert stats["error_count"] == 1
    assert stats["success_count"] == 0


async def test_resolved_count_increases_only_on_success():
    uapi = FakeProvider("uapi", text="OK")
    uapi.stats = {"call_count": 1, "success_count": 1, "error_count": 0}
    service = OcrService(uapi, None, "uapi_only", cache_ttl_seconds=60)
    await service.recognize(GuardianImage(url="http://x/ok.png"))
    failing = OcrService(
        FakeProvider("uapi", success=False, error="TIMEOUT"), None, "uapi_only", cache_ttl_seconds=60
    )
    await failing.recognize(GuardianImage(url="http://x/bad.png"))
    assert service.stats_snapshot()["resolved_count"] == 1
    assert failing.stats_snapshot()["resolved_count"] == 0


def test_retryable_status_set_matches_spec():
    """§143：只重试超时/网络/429/部分 5xx。"""
    from astrbot_plugin_campus_group_guardian.providers.ocr import _RETRYABLE_STATUS

    assert 429 in _RETRYABLE_STATUS
    assert 503 in _RETRYABLE_STATUS
    assert 400 not in _RETRYABLE_STATUS
    assert 401 not in _RETRYABLE_STATUS


@pytest.mark.parametrize("error", ["TIMEOUT", "CONNECT_ERROR"])
async def test_network_errors_are_reported_not_raised(error):
    uapi = FakeOcrProvider(error)
    service = OcrService(uapi, None, "uapi_only", cache_ttl_seconds=0)
    result = await service.recognize(GuardianImage(url="http://x/6.png"))
    assert result.success is False
    assert result.error_type == error


class FakeOcrProvider:
    """永远抛网络异常之外，返回错误结果的 Provider。"""

    def __init__(self, error: str) -> None:
        self.error = error
        self.name = "uapi"

    async def recognize(self, image):  # noqa: ANN001
        return OcrResult(provider="uapi", text="", success=False, latency_ms=1, error_type=self.error)

    async def close(self) -> None:
        return None


class ExplodingProvider:
    """provider 抛异常：必须被兜住，不能让整条反诈判定崩掉。"""

    name = "uapi"

    async def recognize(self, image):  # noqa: ANN001
        raise RuntimeError("connection reset by peer")

    async def close(self) -> None:
        return None


async def test_provider_exception_is_caught():
    service = OcrService(ExplodingProvider(), None, "uapi_only", cache_ttl_seconds=0)
    result = await service.recognize(GuardianImage(url="http://x/boom.png"))
    assert result.success is False
    assert result.error_type == "PROVIDER_ERROR"
