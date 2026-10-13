# -*- coding: utf-8 -*-
"""OCR Provider：UApiPro 通用 OCR + AstrBot 视觉模型，两级可组合。

约束：
- 全部异步（httpx.AsyncClient），带超时、连接复用、有界重试；
- 只重试超时/网络/429/部分 5xx，不重试鉴权失败与请求非法；
- 第三方返回的原始 JSON 不进入风险引擎，只暴露归一化文本；
- API Key 不落日志。
"""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from astrbot.api import logger

from ..core.models import GuardianImage
from ..prompts.vision import VISION_OCR_PROMPT

_LOG = "[Guardian][OCR]"

#: UApiPro 通用 OCR（官方文档：POST https://uapis.cn/api/v1/image/ocr，multipart/form-data）。
UAPI_OCR_ENDPOINT = "https://uapis.cn/api/v1/image/ocr"

#: 可重试的状态码。
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
#: 可重试的错误码（UApiPro 机器码）。
_RETRYABLE_CODES = {"SERVICE_BUSY", "RATE_LIMITED", "UPSTREAM_TIMEOUT", "TIMEOUT"}


@dataclass
class OcrResult:
    """归一化 OCR 结果（§74）。"""

    provider: str
    text: str = ""
    success: bool = False
    latency_ms: int = 0
    error_type: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "text": self.text,
            "success": self.success,
            "latency_ms": self.latency_ms,
            "error_type": self.error_type,
        }


class OcrProvider:
    """OCR Provider 基类。"""

    name = "base"

    async def recognize(
        self,
        image: GuardianImage,
        *,
        need_location: bool = False,
        return_markdown: bool = False,
    ) -> OcrResult:
        raise NotImplementedError

    async def close(self) -> None:
        return None


def _image_to_base64(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            return base64.b64encode(handle.read()).decode()
    except OSError as exc:
        logger.debug(f"{_LOG} 读取图片失败: {exc}")
        return None


class UapiOcrProvider(OcrProvider):
    """UApiPro 通用 OCR。

    内容审核场景默认 ``need_location=false``、``return_markdown=false``，
    只在真正需要坐标时才打开，避免返回体积过大。
    """

    name = "uapi"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = UAPI_OCR_ENDPOINT,
        timeout: float = 15.0,
        max_retries: int = 2,
        max_concurrency: int = 2,
        enable_cls: bool = False,
        stats: dict[str, int] | None = None,
    ) -> None:
        self.api_key = str(api_key or "")
        self.base_url = base_url
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.enable_cls = bool(enable_cls)
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))
        self._client: httpx.AsyncClient | None = None
        self.stats = stats if stats is not None else {}

    def _get_client(self) -> httpx.AsyncClient:
        """惰性创建复用连接池的客户端。"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "User-Agent": "astrbot-group-guardian-next",
                },
                follow_redirects=False,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def recognize(
        self,
        image: GuardianImage,
        *,
        need_location: bool = False,
        return_markdown: bool = False,
    ) -> OcrResult:
        if not self.api_key:
            return OcrResult(provider=self.name, success=False, error_type="NO_API_KEY")

        started = time.monotonic()
        self.stats["call_count"] = self.stats.get("call_count", 0) + 1
        payload, files = await self._build_payload(image, need_location, return_markdown)
        if payload is None and files is None:
            self.stats["error_count"] = self.stats.get("error_count", 0) + 1
            return OcrResult(
                provider=self.name,
                success=False,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_type="NO_IMAGE_INPUT",
            )

        attempt = 0
        while True:
            attempt += 1
            try:
                async with self._semaphore:
                    client = self._get_client()
                    if files:
                        response = await client.post(self.base_url, data=payload, files=files)
                    else:
                        response = await client.post(self.base_url, data=payload)
                if response.status_code in _RETRYABLE_STATUS and attempt <= self.max_retries:
                    await asyncio.sleep(min(2.0, 0.5 * attempt))
                    continue
                latency = int((time.monotonic() - started) * 1000)
                if response.status_code != 200:
                    self.stats["error_count"] = self.stats.get("error_count", 0) + 1
                    return OcrResult(
                        provider=self.name,
                        success=False,
                        latency_ms=latency,
                        error_type=f"HTTP_{response.status_code}",
                    )
                text, error_type = self._parse_text(response)
                if error_type and attempt <= self.max_retries and error_type in _RETRYABLE_CODES:
                    await asyncio.sleep(min(2.0, 0.5 * attempt))
                    continue
                if error_type:
                    self.stats["error_count"] = self.stats.get("error_count", 0) + 1
                    return OcrResult(
                        provider=self.name,
                        success=False,
                        latency_ms=latency,
                        error_type=error_type,
                    )
                self.stats["success_count"] = self.stats.get("success_count", 0) + 1
                return OcrResult(provider=self.name, text=text, success=True, latency_ms=latency)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt <= self.max_retries:
                    await asyncio.sleep(min(2.0, 0.5 * attempt))
                    continue
                self.stats["error_count"] = self.stats.get("error_count", 0) + 1
                return OcrResult(
                    provider=self.name,
                    success=False,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    error_type="NETWORK",
                )
            except Exception as exc:  # 兜底：OCR 失败不得影响主流程
                logger.debug(f"{_LOG} UApi OCR 异常: {exc}")
                self.stats["error_count"] = self.stats.get("error_count", 0) + 1
                return OcrResult(
                    provider=self.name,
                    success=False,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    error_type="UNKNOWN",
                )

    async def _build_payload(
        self,
        image: GuardianImage,
        need_location: bool,
        return_markdown: bool,
    ) -> tuple[dict[str, str] | None, list[tuple[str, tuple[str, bytes, str]]] | None]:
        """构造请求体。优先本地文件，其次公网 URL，最后才用 Base64（减小体积）。"""
        common = {
            "need_location": "true" if need_location else "false",
            "return_markdown": "true" if return_markdown else "false",
            "enable_cls": "true" if self.enable_cls else "false",
        }
        if image.file_path:
            try:
                with open(image.file_path, "rb") as handle:
                    content = handle.read()
                name = image.file_path.rsplit("/", 1)[-1] or "image.png"
                return common, [("file", (name, content, image.mime_type or "application/octet-stream"))]
            except OSError as exc:
                logger.debug(f"{_LOG} 本地图片读取失败，退回 URL/Base64: {exc}")
        if image.url and image.url.startswith(("http://", "https://")):
            payload = dict(common)
            payload["url"] = image.url
            return payload, None
        raw = image.raw
        if raw is not None:
            # 平台只给了内存中的图片组件：先落盘再上传，失败才退回 Base64。
            if hasattr(raw, "convert_to_file_path"):
                try:
                    path = await raw.convert_to_file_path()
                    with open(path, "rb") as handle:
                        content = handle.read()
                    name = str(path).rsplit("/", 1)[-1] or "image.png"
                    return common, [("file", (name, content, image.mime_type or "application/octet-stream"))]
                except Exception as exc:
                    logger.debug(f"{_LOG} 图片落盘失败，改用 Base64: {exc}")
            if hasattr(raw, "convert_to_base64"):
                try:
                    encoded = await raw.convert_to_base64()
                    if encoded:
                        payload = dict(common)
                        payload["image_base64"] = str(encoded)
                        return payload, None
                except Exception as exc:
                    logger.debug(f"{_LOG} 图片 Base64 转换失败: {exc}")
        return None, None

    def _parse_text(self, response: httpx.Response) -> tuple[str, str | None]:
        """从响应里取出识别文本，屏蔽第三方 JSON 结构。"""
        try:
            data = response.json()
        except ValueError:
            return "", "BAD_RESPONSE"
        if not isinstance(data, dict):
            return "", "BAD_RESPONSE"
        code = data.get("code")
        if code and code not in {"SUCCESS", "OK", 200}:
            return "", str(code)
        for key in ("text", "plain_text"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), None
        words = data.get("words_result")
        if isinstance(words, list):
            parts = []
            for item in words:
                if isinstance(item, dict):
                    piece = item.get("words")
                    if isinstance(piece, str) and piece:
                        parts.append(piece)
            joined = "\n".join(parts).strip()
            if joined:
                return joined, None
        return "", "EMPTY_RESULT"


class AiVisionOcrProvider(OcrProvider):
    """用 AstrBot 已配置的视觉模型做图片文本提取。

    不要求用户在本插件里重新填写第三方 AI Key，只选择一个 Provider ID。
    """

    name = "ai_vision"

    def __init__(
        self,
        context: Any,
        provider_id: str,
        *,
        timeout: float = 30.0,
        max_concurrency: int = 2,
        stats: dict[str, int] | None = None,
    ) -> None:
        self.context = context
        self.provider_id = str(provider_id or "")
        self.timeout = float(timeout)
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))
        self.stats = stats if stats is not None else {}

    async def recognize(
        self,
        image: GuardianImage,
        *,
        need_location: bool = False,
        return_markdown: bool = False,
    ) -> OcrResult:
        _ = need_location, return_markdown
        if self.context is None or not self.provider_id:
            return OcrResult(provider=self.name, success=False, error_type="NO_PROVIDER")
        started = time.monotonic()
        self.stats["call_count"] = self.stats.get("call_count", 0) + 1
        image_url = await self._resolve_image_url(image)
        if not image_url:
            self.stats["error_count"] = self.stats.get("error_count", 0) + 1
            return OcrResult(provider=self.name, success=False, error_type="NO_IMAGE_INPUT")
        try:
            async with self._semaphore:
                response = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=self.provider_id,
                        prompt=VISION_OCR_PROMPT,
                        image_urls=[image_url],
                    ),
                    timeout=self.timeout,
                )
            text = str(getattr(response, "completion_text", "") or "").strip()
            latency = int((time.monotonic() - started) * 1000)
            if not text:
                self.stats["error_count"] = self.stats.get("error_count", 0) + 1
                return OcrResult(provider=self.name, success=False, latency_ms=latency, error_type="EMPTY_RESULT")
            self.stats["success_count"] = self.stats.get("success_count", 0) + 1
            return OcrResult(provider=self.name, text=text, success=True, latency_ms=latency)
        except asyncio.TimeoutError:
            self.stats["error_count"] = self.stats.get("error_count", 0) + 1
            return OcrResult(
                provider=self.name,
                success=False,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_type="TIMEOUT",
            )
        except Exception as exc:
            logger.debug(f"{_LOG} AI 视觉识别失败: {exc}")
            self.stats["error_count"] = self.stats.get("error_count", 0) + 1
            return OcrResult(
                provider=self.name,
                success=False,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_type="PROVIDER_ERROR",
            )

    async def _resolve_image_url(self, image: GuardianImage) -> str | None:
        """把统一图片对象转成模型可读的输入（URL / 本地路径 / data URL）。"""
        if image.url and image.url.startswith(("http://", "https://")):
            return image.url
        if image.file_path:
            encoded = _image_to_base64(image.file_path)
            if encoded:
                return f"data:image/png;base64,{encoded}"
        raw = image.raw
        if raw is not None:
            try:
                if hasattr(raw, "convert_to_file_path"):
                    path = await raw.convert_to_file_path()
                    encoded = _image_to_base64(path)
                    if encoded:
                        return f"data:image/png;base64,{encoded}"
                if hasattr(raw, "convert_to_base64"):
                    return await raw.convert_to_base64()
            except Exception as exc:
                logger.debug(f"{_LOG} AI 视觉图片转换失败: {exc}")
        return None

    async def close(self) -> None:
        return None


@dataclass
class OcrService:
    """OCR 编排：策略选择、结果去重、并发限制与统计。"""

    uapi: OcrProvider | None = None
    ai_vision: OcrProvider | None = None
    strategy: str = "uapi_then_ai"
    cache_ttl_seconds: int = 600
    strict_image_scan: bool = False
    _cache: dict[str, tuple[float, OcrResult]] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)

    async def recognize(self, image: GuardianImage) -> OcrResult:
        """按策略识别图片，命中缓存直接复用（§145）。"""
        cache_key = image.cache_key()
        if cache_key:
            cached = self._cache.get(cache_key)
            if cached and (time.monotonic() - cached[0]) < self.cache_ttl_seconds:
                self.stats["cache_hit_count"] = self.stats.get("cache_hit_count", 0) + 1
                return cached[1]

        result = await self._run_strategy(image)
        if cache_key and result.success:
            self._cache[cache_key] = (time.monotonic(), result)
        return result

    async def _run_strategy(self, image: GuardianImage) -> OcrResult:
        providers: list[OcrProvider] = []
        normalized = str(self.strategy or "").lower()
        if normalized == "uapi_only":
            providers = [self.uapi] if self.uapi else []
        elif normalized == "ai_only":
            providers = [self.ai_vision] if self.ai_vision else []
        elif normalized == "ai_then_uapi":
            providers = [provider for provider in (self.ai_vision, self.uapi) if provider]
        else:  # uapi_then_ai（默认）
            providers = [provider for provider in (self.uapi, self.ai_vision) if provider]

        if not providers:
            return OcrResult(provider="none", success=False, error_type="OCR_DISABLED")

        last = OcrResult(provider="none", success=False, error_type="OCR_DISABLED")
        for provider in providers:
            try:
                result = await provider.recognize(image)
            except Exception as exc:  # Provider 内部异常不得中断反诈判定（§21、§180）
                logger.debug(f"{_LOG} OCR Provider {getattr(provider, 'name', '?')} 异常: {exc}")
                last = OcrResult(
                    provider=str(getattr(provider, "name", "unknown")),
                    text="",
                    success=False,
                    error_type="PROVIDER_ERROR",
                )
                continue
            if result.success:
                self.stats["resolved_count"] = self.stats.get("resolved_count", 0) + 1
                return result
            last = result
        return last

    def prune_cache(self) -> int:
        """清理过期缓存，返回清理数量。"""
        now = time.monotonic()
        stale = [key for key, (stamp, _) in self._cache.items() if (now - stamp) >= self.cache_ttl_seconds]
        for key in stale:
            del self._cache[key]
        return len(stale)

    def stats_snapshot(self) -> dict[str, int]:
        """对外只暴露调用次数类指标，不暴露第三方价格（§76）。"""
        snapshot = {
            "call_count": 0,
            "success_count": 0,
            "error_count": 0,
            "cache_hit_count": self.stats.get("cache_hit_count", 0),
            "resolved_count": self.stats.get("resolved_count", 0),
        }
        for provider in (self.uapi, self.ai_vision):
            if provider is None:
                continue
            for key in ("call_count", "success_count", "error_count"):
                snapshot[key] += int(getattr(provider, "stats", {}).get(key, 0))
        return snapshot

    async def close(self) -> None:
        for provider in (self.uapi, self.ai_vision):
            if provider is not None:
                await provider.close()
