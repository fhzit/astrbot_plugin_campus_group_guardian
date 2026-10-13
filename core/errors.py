# -*- coding: utf-8 -*-
"""平台错误归一化：平台原始错误不得直接抛给用户。"""

from __future__ import annotations

from enum import Enum


class GuardianPlatformErrorType(Enum):
    """统一平台错误类型。"""

    PERMISSION_DENIED = "permission_denied"
    RATE_LIMITED = "rate_limited"
    NOT_SUPPORTED = "not_supported"
    TARGET_PROTECTED = "target_protected"
    MESSAGE_NOT_FOUND = "message_not_found"
    RECALL_TIME_LIMIT = "recall_time_limit"
    NETWORK = "network"
    UNKNOWN = "unknown"


class GuardianPlatformError(Exception):
    """平台调用异常：携带归一化类型与原始信息。"""

    def __init__(
        self,
        error_type: GuardianPlatformErrorType,
        raw: str = "",
        retcode: int | None = None,
    ) -> None:
        self.error_type = error_type
        self.raw = str(raw or "")
        self.retcode = retcode
        super().__init__(self.raw or error_type.value)


_USER_MESSAGES = {
    GuardianPlatformErrorType.PERMISSION_DENIED: "机器人没有执行该操作的权限。",
    GuardianPlatformErrorType.RATE_LIMITED: "平台限流，请稍后重试。",
    GuardianPlatformErrorType.NOT_SUPPORTED: "当前平台机器人接口不支持该操作。",
    GuardianPlatformErrorType.TARGET_PROTECTED: "该成员受平台保护（群主或其他管理员），无法执行此操作。",
    GuardianPlatformErrorType.MESSAGE_NOT_FOUND: "消息不存在或已被删除。",
    GuardianPlatformErrorType.RECALL_TIME_LIMIT: "消息已超出平台允许的撤回时限。",
    GuardianPlatformErrorType.NETWORK: "平台网络异常，操作未确认成功。",
    GuardianPlatformErrorType.UNKNOWN: "平台操作失败，请查看日志了解详情。",
}


# 关键字 → 错误类型。平台文案会更新，因此这里只做粗粒度归类，
# 未命中一律 UNKNOWN，绝不猜测成成功。
_PATTERNS: tuple[tuple[GuardianPlatformErrorType, tuple[str, ...]], ...] = (
    (
        GuardianPlatformErrorType.RECALL_TIME_LIMIT,
        ("超出消息撤回时限", "撤回时限", "40064004", "expired", "time limit"),
    ),
    (
        GuardianPlatformErrorType.TARGET_PROTECTED,
        ("群主", "owner", "other admin", "其他管理员", "protected"),
    ),
    (
        GuardianPlatformErrorType.PERMISSION_DENIED,
        ("无操作权限", "权限不足", "forbidden", "not allowed", "40062003", "11253", "no permission", "拒绝"),
    ),
    (
        GuardianPlatformErrorType.RATE_LIMITED,
        ("限流", "rate limit", "too many requests", "429", "频率"),
    ),
    (
        GuardianPlatformErrorType.MESSAGE_NOT_FOUND,
        ("消息不存在", "not found", "404", "unknown message", "msg not found"),
    ),
    (
        GuardianPlatformErrorType.NOT_SUPPORTED,
        ("不支持", "unsupported", "not implemented", "method not allowed", "405"),
    ),
    (
        GuardianPlatformErrorType.NETWORK,
        ("timeout", "timed out", "超时", "connection", "network", "os error", "websocket"),
    ),
)


def classify_platform_error(error: BaseException | str, retcode: int | None = None) -> GuardianPlatformErrorType:
    """把平台异常/错误文案归类为统一错误类型。

    Args:
        error: 平台异常对象或错误文本。
        retcode: 平台返回码（如有）。

    Returns:
        归一化后的错误类型。
    """
    text = f"{error}" if error is not None else ""
    lowered = text.lower()
    for error_type, keywords in _PATTERNS:
        for keyword in keywords:
            if keyword.lower() in lowered:
                return error_type
    if retcode is not None:
        if retcode == 100:
            return GuardianPlatformErrorType.NOT_SUPPORTED
        if retcode == 1400:
            return GuardianPlatformErrorType.MESSAGE_NOT_FOUND
        if retcode in {1401, 1403}:
            return GuardianPlatformErrorType.PERMISSION_DENIED
    return GuardianPlatformErrorType.UNKNOWN


def to_platform_error(error: BaseException | str, retcode: int | None = None) -> GuardianPlatformError:
    """把任意异常包装成 GuardianPlatformError。"""
    if isinstance(error, GuardianPlatformError):
        return error
    return GuardianPlatformError(classify_platform_error(error, retcode), raw=f"{error}", retcode=retcode)


def user_message(error_type: GuardianPlatformErrorType) -> str:
    """错误类型对应的用户可读提示。"""
    return _USER_MESSAGES.get(error_type, _USER_MESSAGES[GuardianPlatformErrorType.UNKNOWN])
