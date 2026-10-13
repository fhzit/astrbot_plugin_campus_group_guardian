# -*- coding: utf-8 -*-
"""QQ 官方机器人撤回策略测试（规格 §18-§21、§160-§163、§177、§208-§210）。"""

from __future__ import annotations

import pytest

from astrbot_plugin_campus_group_guardian.core.capabilities import (
    RecallDecision,
    RecallDenyReason,
    decide_recall,
    qq_official_recall_policy,
)
from astrbot_plugin_campus_group_guardian.core.models import GroupRole

POLICY = qq_official_recall_policy()


# ----------------------------------------------------------------------------
# §177 Case 1-7：直接覆盖规格点名的七个场景
# ----------------------------------------------------------------------------
def test_case1_member_bot_own_message_30s_allowed():
    check = decide_recall(
        POLICY,
        bot_role=GroupRole.MEMBER,
        target_role=GroupRole.MEMBER,
        is_bot_message=True,
        age_seconds=30,
    )
    assert check.decision is RecallDecision.ALLOW


def test_case2_member_bot_own_message_180s_denied():
    check = decide_recall(
        POLICY,
        bot_role=GroupRole.MEMBER,
        target_role=GroupRole.MEMBER,
        is_bot_message=True,
        age_seconds=180,
    )
    assert check.decision is RecallDecision.DENY_BY_POLICY
    assert check.reason is RecallDenyReason.AGE_LIMIT


def test_case3_member_bot_cannot_recall_member_message():
    check = decide_recall(
        POLICY,
        bot_role=GroupRole.MEMBER,
        target_role=GroupRole.MEMBER,
        is_bot_message=False,
        age_seconds=10,
    )
    assert check.decision is RecallDecision.DENY_BY_ROLE
    assert check.reason is RecallDenyReason.BOT_NOT_PRIVILEGED


def test_case4_admin_bot_own_message_one_day_allowed():
    check = decide_recall(
        POLICY,
        bot_role=GroupRole.ADMIN,
        target_role=GroupRole.MEMBER,
        is_bot_message=True,
        age_seconds=86400,
    )
    assert check.decision is RecallDecision.ALLOW


def test_case5_admin_bot_member_message_one_day_allowed():
    """§85/§208：管理员撤回普通成员消息不设 2 分钟预检查。"""
    check = decide_recall(
        POLICY,
        bot_role=GroupRole.ADMIN,
        target_role=GroupRole.MEMBER,
        is_bot_message=False,
        age_seconds=86400,
    )
    assert check.decision is RecallDecision.ALLOW


def test_case6_admin_bot_cannot_recall_other_admin():
    check = decide_recall(
        POLICY,
        bot_role=GroupRole.ADMIN,
        target_role=GroupRole.ADMIN,
        is_bot_message=False,
        age_seconds=10,
    )
    assert check.decision is RecallDecision.DENY_BY_ROLE
    assert check.reason is RecallDenyReason.TARGET_PROTECTED


def test_case7_admin_bot_cannot_recall_owner():
    check = decide_recall(
        POLICY,
        bot_role=GroupRole.ADMIN,
        target_role=GroupRole.OWNER,
        is_bot_message=False,
        age_seconds=10,
    )
    assert check.decision is RecallDecision.DENY_BY_ROLE
    assert check.reason is RecallDenyReason.TARGET_PROTECTED


# ----------------------------------------------------------------------------
# 额外边界
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("age", [119, 120, 121])
def test_own_message_boundary(age: int):
    check = decide_recall(
        POLICY,
        bot_role=GroupRole.MEMBER,
        target_role=GroupRole.MEMBER,
        is_bot_message=True,
        age_seconds=age,
    )
    assert check.allowed is (age <= 120)


def test_unknown_bot_role_is_not_treated_as_admin():
    """§53/§166：角色未知不得当作管理员放行。"""
    check = decide_recall(
        POLICY,
        bot_role=GroupRole.UNKNOWN,
        target_role=GroupRole.MEMBER,
        is_bot_message=False,
        age_seconds=1,
    )
    assert check.decision is RecallDecision.DENY_BY_ROLE


def test_policy_declares_no_time_limit_for_admin():
    assert POLICY.admin_bot_member_max_age_seconds is None
    assert POLICY.admin_bot_own_max_age_seconds is None
    assert POLICY.normal_bot_own_max_age_seconds == 120
