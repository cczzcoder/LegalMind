"""查询改写（设计 §8.2 第 2 步）：纯单元测试，不需要数据库。

三层各自可单独开关，所以要分别钉住：规范化、剥离疑问框架、同义扩展。
"""

from app.modules.retrieval import rewrite


def test_normalize_strips_whitespace_punctuation_and_fullwidth():
    assert rewrite.normalize("单位欠缴　社保费？") == "单位欠缴社保费"
    assert rewrite.normalize("ＡＢＣ １２３") == "ABC123"


def test_strip_frames_keeps_the_content_words():
    assert rewrite.strip_frames("单位欠缴社保费会被怎么处理") == "单位欠缴社保费"
    assert rewrite.strip_frames("监狱属于什么性质的国家机关") == "监狱 性质的国家机关"
    assert rewrite.strip_frames("请问审计工作底稿必须存放在哪里") == "审计工作底稿必须存放在哪里"


def test_expand_replaces_one_phrase_at_a_time():
    """一次换一处：候选数随命中数线性增长，而不是笛卡尔积。"""
    variants = rewrite.expand("单位欠缴社保费")
    assert "单位不缴纳社保费" in variants
    assert "单位欠缴社会保险费" in variants
    # 两个词都替换的组合不在候选里——那是笛卡尔积，收益不值那个数量
    assert "单位不缴纳社会保险费" not in variants


def test_expand_returns_nothing_when_no_known_phrase():
    assert rewrite.expand("量子计算专利强制许可") == ()


def test_variants_always_keep_the_original_query():
    """改写不替代原查询：并集只可能提高召回，不会因为改错而降低召回。"""
    result = rewrite.rewrite("单位欠缴社保费会被怎么处理？")
    variants = result.variants()
    assert variants[0] == "单位欠缴社保费会被怎么处理？"
    assert "单位欠缴社保费" in variants
    assert any("社会保险" in item for item in variants)


def test_variants_are_deduplicated():
    variants = rewrite.rewrite("社保").variants()
    assert len(variants) == len(set(variants))


def test_terms_splits_on_the_spaces_the_rewrite_introduces():
    assert rewrite.terms("单位 欠缴 社会保险") == ["单位", "欠缴", "社会保险"]
    assert rewrite.terms("没有空格的连续中文") == ["没有空格的连续中文"]


def test_variants_never_include_a_blank_query():
    """**空白变体不是查询，必须滤掉**——它会让关键词段抛 `IndexError`（整条检索 500）。

    实测（2026-10-07）：「怎么办」「怎么处理」这类**纯疑问句**经第 2 层剥离后 `stripped` 是空串，
    空串进 `_all_terms_flat` 取 `terms[0]` 直接崩——而这是用户最常见的问法之一。
    """
    for question in ("怎么办", "怎么处理", "  ", "？"):
        variants = rewrite.rewrite(question).variants()
        assert all(item.strip() for item in variants), question
        assert "" not in variants


def test_vague_question_still_keeps_its_original_variant():
    """滤掉空白变体不等于把原查询也丢了——原查询**永远**在候选里（§8.2 第 2 步「默认保留」）。"""
    variants = rewrite.rewrite("怎么办").variants()
    assert "怎么办" in variants
