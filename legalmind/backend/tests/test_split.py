"""问题拆分（`split_questions`）：纯单元测试，不需要数据库。

⚠️ 这一层的纪律是**宁可漏拆，也不要拆错**——所以「不该拆」的用例和「该拆」的一样重要：
拆错了，用户要面对两个都不是他问的问题。
"""

from app.modules.answering.split import split_questions


def test_single_question_is_returned_as_is():
    """只有一个问题时原样返回（含标点），调用方不必判断「有没有拆过」。"""
    assert split_questions("社保怎么缴？") == ["社保怎么缴？"]
    assert split_questions("用人单位无故不缴纳社会保险费，会被怎么处理？") == [
        "用人单位无故不缴纳社会保险费，会被怎么处理？"
    ]


def test_two_questions_separated_by_question_marks_are_split():
    assert split_questions("社保怎么缴？工资怎么发？") == ["社保怎么缴", "工资怎么发"]


def test_full_width_and_half_width_marks_both_split():
    assert split_questions("社保怎么缴?工资怎么发?") == ["社保怎么缴", "工资怎么发"]
    assert split_questions("社保怎么缴；工资怎么发") == ["社保怎么缴", "工资怎么发"]


def test_comma_is_not_a_separator():
    """⚠️ 逗号是句内停顿，不是问题边界——在逗号处拆会把**一个**问题拆碎。"""
    text = "用人单位无故不缴纳社会保险费，会被怎么处理？"
    assert split_questions(text) == [text]


def test_segments_without_substance_are_dropped():
    """剥完没有实质内容的片段不算独立问题（否则会拿用户根本没问的东西去检索）。

    「怎么办」剥完是空串 ⇒ 不算一个问题；剩下的只有一段 ⇒ 整体原样返回。
    """
    assert split_questions("怎么办？社保怎么缴？") == ["怎么办？社保怎么缴？"]


def test_no_separator_means_no_split():
    """⚠️ 没有分隔符时**一律不拆**，哪怕读起来像两个问题——宁可漏拆，也不要拆错。"""
    text = "我想知道社保怎么缴还有工资怎么发"
    assert split_questions(text) == [text]


def test_blank_input_returns_empty_list():
    assert split_questions("") == []
    assert split_questions("   ") == []


def test_original_text_is_kept_when_not_split():
    """没拆时返回的是**原文**（不是规范化后的），标点与空白都保留。"""
    text = "  社保怎么缴？  "
    assert split_questions(text) == ["社保怎么缴？"]
