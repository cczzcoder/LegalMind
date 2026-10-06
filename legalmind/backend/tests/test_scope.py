"""提问范围与输出边界（`app/modules/answering/scope.py`，设计 §20.2、§9.3 第三层）。

规则匹配必然有漏有误报，所以这里既钉「该命中的」也钉**不该命中的**——
误报的代价是用户白跑一趟人工，漏报的代价是系统替用户下了个人结论。
"""

import pytest

from app.modules.answering import scope


@pytest.mark.parametrize(
    "question",
    [
        "我是名特困人员，我能否领到社会救助？",
        "我的劳动合同到期公司不续签，该不该赔偿？",
        "我能不能申请最低生活保障？",
        "我是否符合特困人员供养的条件？",
        "我有没有资格领取失业保险金？",
    ],
)
def test_personal_questions_are_detected(question):
    assert scope.detect_personal_scope(question) is not None


@pytest.mark.parametrize(
    "question",
    [
        # 没有人称：问的是法条本身
        "社会救助分为哪几类？",
        "用人单位无故不缴纳社会保险费，会被怎么处理？",
        "国家发展规划实施到中期阶段，国务院应当做什么？",
        # 有人称但问的是信息，不是对本人资格下判断
        "我想知道国家通用语言文字法的适用范围。",
        "我想了解社会救助有哪些类型。",
        # 「资格」是法条里的名词，不是对提问者的判断
        "注册会计师资格的取得条件是什么？",
    ],
)
def test_impersonal_questions_are_not_flagged(question):
    """误报的代价是用户白跑一趟人工——所以「我」必须**同时**配上资格类说法才算个性化。"""
    assert scope.detect_personal_scope(question) is None


def test_reason_names_what_matched():
    reason = scope.detect_personal_scope("我是名特困人员，我能否领到社会救助？")
    assert "我" in reason and "能否" in reason


def test_scope_notice_tells_the_user_what_to_do_next():
    """边界说明不能只说「不行」——要说明不做什么、并给出下一步。"""
    notice = scope.scope_notice("我能不能申请最低生活保障？")
    assert notice is not None
    assert "不生成个人结论" in notice
    assert "转人工" in notice
    # 命中的理由要写进去，否则用户不知道系统为什么这么答
    assert "我" in notice


def test_disclaimer_says_what_the_design_requires():
    """§20.2：每个正式输出都要带「不构成法律意见」声明与知识范围说明。"""
    assert "不构成法律意见" in scope.DISCLAIMER
    assert "执业律师" in scope.DISCLAIMER


def test_empty_question_is_not_personal():
    assert scope.detect_personal_scope("") is None
    assert scope.detect_personal_scope(None) is None
