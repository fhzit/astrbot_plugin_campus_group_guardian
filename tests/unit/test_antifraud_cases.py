# -*- coding: utf-8 -*-
"""家校反诈检测用例（规格 §49-§89、§178-§180、§208-§213）。"""

from __future__ import annotations

import pytest

from astrbot_plugin_campus_group_guardian.core.antifraud.engine import (
    AntiFraudConfig,
    SchoolFraudGuard,
)
from astrbot_plugin_campus_group_guardian.core.antifraud.models import (
    FraudRiskLevel,
    FraudSignalType,
)
from astrbot_plugin_campus_group_guardian.core.models import GroupRole, GuardianImage

SCAM_TEXT = (
    "学校统一购买学习资料，每位495元，请扫码付款，"
    "付款时备注学生姓名，付款完成后截图方便统计。"
)


class FakeOcr:
    """可控 OCR：成功返回给定文本，失败返回错误类型。"""

    def __init__(self, text: str = "", *, success: bool = True, error: str | None = None) -> None:
        self.text = text
        self.success = success
        self.error = error
        self.calls = 0

    async def recognize(self, image):  # noqa: ANN001 - 与 OcrService 同签名
        from astrbot_plugin_campus_group_guardian.providers.ocr import OcrResult

        self.calls += 1
        if self.success:
            return OcrResult(provider="fake", text=self.text, success=True, latency_ms=3)
        return OcrResult(
            provider="fake",
            text="",
            success=False,
            latency_ms=3,
            error_type=self.error or "timeout",
        )


def config(**overrides) -> AntiFraudConfig:
    data = {"enabled": True}
    data.update(overrides)
    return AntiFraudConfig(**data)


@pytest.fixture
def guard() -> SchoolFraudGuard:
    return SchoolFraudGuard()


# ----------------------------------------------------------------------------
# §178 基础判定
# ----------------------------------------------------------------------------
async def test_safe_homework_message(guard, message_factory):
    result = await guard.analyze(message_factory("今天数学作业第5页"), config())
    assert result.level is FraudRiskLevel.SAFE
    assert result.score == 0
    assert result.signals == []


async def test_normal_cost_discussion_is_not_high_risk(guard, message_factory):
    """正常费用讨论：不得判为 HIGH/CRITICAL（§67、§68）。"""
    result = await guard.analyze(message_factory("校服是不是500左右？"), config())
    assert result.is_risky is False
    assert result.score < 60


async def test_urgency_alone_is_weak_signal(guard, message_factory):
    """§58：催促只能作为弱信号，不能单独判诈骗。"""
    result = await guard.analyze(message_factory("大家尽快完成，马上要交了"), config())
    assert result.level is FraudRiskLevel.SAFE
    assert result.score < 30


async def test_high_risk_message_reaches_critical(guard, message_factory):
    result = await guard.analyze(message_factory(SCAM_TEXT), config())
    assert result.level is FraudRiskLevel.CRITICAL
    assert result.score >= 85
    assert result.fast_path is True
    types = result.signal_types()
    assert {
        FraudSignalType.PAYMENT_INTENT,
        FraudSignalType.AMOUNT,
        FraudSignalType.PAYMENT_ACTION,
        FraudSignalType.STUDENT_NAME_REQUEST,
        FraudSignalType.PAYMENT_SCREENSHOT_REQUEST,
    } <= types
    assert result.amounts and str(result.amounts[0].amount) == "495"


async def test_screenshot_request_boosts_score(guard, message_factory):
    """组合加权：付款截图要求带来额外乘数（§67）。"""
    base = await guard.analyze(message_factory("每位495元，请扫码付款"), config())
    boosted = await guard.analyze(
        message_factory("每位495元，请扫码付款，付款后截图方便统计", message_id="m2"),
        config(),
    )
    assert boosted.score > base.score


# ----------------------------------------------------------------------------
# §51/§52/§179 群主与管理员放行
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("role", [GroupRole.OWNER, GroupRole.ADMIN])
async def test_admin_and_owner_are_bypassed(guard, message_factory, role):
    result = await guard.analyze(message_factory(SCAM_TEXT, role=role), config())
    assert result.bypassed is True
    assert result.bypass_reason == "ANTI_FRAUD_BYPASS"
    assert result.level is FraudRiskLevel.SAFE
    assert result.signals == []


async def test_bypass_only_affects_school_fraud(guard, message_factory):
    """§52：放行只针对家校缴费诈骗，管理员刷屏/敏感词由其他模块独立判定。"""
    from astrbot_plugin_campus_group_guardian.core.moderation.engine import (
        ContentModerationEngine,
        ModerationConfig,
    )
    from astrbot_plugin_campus_group_guardian.core.moderation.matcher import RuleMatcher

    matcher = RuleMatcher([("代开发票", "advertisement")], [])
    engine = ContentModerationEngine(matcher, None)
    decision = await engine.analyze(
        message_factory("代开发票", role=GroupRole.ADMIN),
        ModerationConfig(enabled=True, admin_exempt=False),
    )
    assert decision.is_violation is True


async def test_unknown_role_is_not_bypassed(guard, message_factory):
    """§53/§166：角色未知不得按管理员放行。"""
    result = await guard.analyze(message_factory(SCAM_TEXT, role=GroupRole.UNKNOWN), config())
    assert result.bypassed is False
    assert result.level in (FraudRiskLevel.HIGH, FraudRiskLevel.CRITICAL)


async def test_trusted_sender_ids_bypass(guard, message_factory):
    """§53：企微/钉钉等角色信息不足时可用可信名单放行。"""
    result = await guard.analyze(
        message_factory(SCAM_TEXT, sender_id="666", role=GroupRole.UNKNOWN),
        config(trusted_sender_ids=["666"]),
    )
    assert result.bypassed is True
    assert result.bypass_reason == "TRUSTED_SENDER_BYPASS"


# ----------------------------------------------------------------------------
# §62/§211 多消息联合判定
# ----------------------------------------------------------------------------
async def test_multi_message_scam_is_detected_across_messages(guard, message_factory):
    pieces = [
        "各位家长：学校统一购买学习资料，",
        "每位495元，",
        "请扫码付款，",
        "付款时备注学生姓名，",
        "付款完成后截图方便统计。",
    ]
    last = None
    for index, piece in enumerate(pieces):
        last = await guard.analyze(
            message_factory(piece, message_id=f"m{index}", role=GroupRole.MEMBER), config()
        )
    assert last is not None
    assert last.level is FraudRiskLevel.CRITICAL


async def test_context_does_not_leak_across_senders(guard, message_factory):
    """上下文按群 + 发送者隔离（§63）。"""
    await guard.analyze(message_factory("每位495元", sender_id="a"), config())
    result = await guard.analyze(
        message_factory("请扫码付款", sender_id="b", message_id="m9"), config()
    )
    # b 只发了付款动作，不构成完整缴费话术
    assert result.level is not FraudRiskLevel.CRITICAL


# ----------------------------------------------------------------------------
# §68/§69/§180 图片与 OCR
# ----------------------------------------------------------------------------
async def test_image_only_message_is_not_critical(guard, message_factory):
    """§68：只有二维码/图片不等于诈骗。"""
    result = await guard.analyze(
        message_factory("", images=[GuardianImage(url="http://x/1.png")]), config()
    )
    assert result.level is FraudRiskLevel.SAFE


async def test_ocr_text_adds_payment_image_signal(message_factory):
    ocr = FakeOcr("收款二维码 资料费 495 元")
    guard = SchoolFraudGuard(ocr_service=ocr)
    result = await guard.analyze(
        message_factory(
            "请看下面的收款码",
            images=[GuardianImage(url="http://x/1.png")],
            sender_name="班主任张老师",
        ),
        config(teacher_names=["张老师"]),
    )
    assert ocr.calls == 1
    assert FraudSignalType.PAYMENT_IMAGE in result.signal_types() or (
        FraudSignalType.OCR_PAYMENT_TEXT in result.signal_types()
    )
    assert result.level is not FraudRiskLevel.SAFE


async def test_ocr_failure_does_not_clear_local_risk(message_factory):
    """§180：OCR 超时不得自动判安全，本地风险信号继续保留。"""
    ocr = FakeOcr(success=False, error="timeout")
    guard = SchoolFraudGuard(ocr_service=ocr)
    result = await guard.analyze(
        message_factory(SCAM_TEXT, images=[GuardianImage(url="http://x/1.png")]),
        config(),
    )
    assert result.level is FraudRiskLevel.CRITICAL
    assert result.ocr_text == ""
    assert FraudSignalType.PAYMENT_IMAGE in result.signal_types()
    assert "识别失败" in " ".join(signal.evidence for signal in result.signals)


async def test_teacher_impersonation_is_signal_only(guard, message_factory):
    """§60：教师冒充只作为风险信号。"""
    result = await guard.analyze(
        message_factory(
            "各位家长好，我是班主任张老师，学校要收资料费",
            sender_name="班主任张老师",
        ),
        config(teacher_names=["张老师"]),
    )
    assert FraudSignalType.TEACHER_IMPERSONATION in result.signal_types()


# ----------------------------------------------------------------------------
# 关闭开关
# ----------------------------------------------------------------------------
async def test_disabled_module_returns_safe(guard, message_factory):
    result = await guard.analyze(message_factory(SCAM_TEXT), config(enabled=False))
    assert result.level is FraudRiskLevel.SAFE
    assert result.bypassed is False
