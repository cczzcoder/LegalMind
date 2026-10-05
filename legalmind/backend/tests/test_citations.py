"""条款引用抽取（设计 §8.4）：纯单元测试，不需要数据库。

这些形态都取自真实语料的实测结果，不是编出来的例子。
"""

from app.modules.legal_corpus.citations import extract


def test_local_citation():
    refs = extract("用人单位依据本法第二十四条、第二十六条的规定解除劳动合同。")
    assert [ref.target for ref in refs] == ["24", "26"]
    assert all(ref.alias is None for ref in refs)


def test_range_is_expanded():
    """实测商标法「本法第二十条至第二十二条」——区间要展开成每一条，不是只留两端。"""
    refs = extract("违反本法第二十条至第二十二条规定的情形。")
    assert [ref.target for ref in refs] == ["20", "21", "22"]


def test_regulation_qualifier_counts_as_local():
    refs = extract("违反本条例第三十六条规定销售取得的药品。")
    assert [ref.target for ref in refs] == ["36"]


def test_previous_article_uses_the_current_number():
    refs = extract("有前条所列情形之一的，应当报告。", article_number="50")
    assert [ref.target for ref in refs] == ["49"]


def test_window_stops_at_another_law():
    """跨到别的法律去就会造出错边——实测「本法第五十条和《药品管理法》第四十二条」。"""
    refs = extract(
        "依照本法第五十条和《药品管理法》第四十二条的规定处理。",
        titles=["中华人民共和国药品管理法"],
    )
    assert [ref.target for ref in refs if ref.alias is None] == ["50"]
    assert any(ref.alias == "药品管理法" and ref.target == "42" for ref in refs)


def test_cross_law_citation_uses_the_short_name():
    refs = extract("符合药品管理法第四十二条规定条件的资料。", titles=["中华人民共和国药品管理法"])
    assert [(ref.alias, ref.target) for ref in refs] == [("药品管理法", "42")]


def test_unknown_law_name_is_not_extracted():
    """匹配不上的法律名不抽——宁缺勿错。"""
    assert extract("违反某不存在法第十二条的规定。", titles=["中华人民共和国药品管理法"]) == []


def test_shorter_law_name_does_not_match_inside_a_longer_one():
    """「药品管理法」不能命中「药品管理法实施条例」的前几个字，否则会指向错法律。"""
    refs = extract(
        "依照《中华人民共和国药品管理法实施条例》第三十条的规定处理。",
        titles=["中华人民共和国药品管理法", "中华人民共和国药品管理法实施条例"],
    )
    assert [(ref.alias, ref.target) for ref in refs] == [("药品管理法实施条例", "30")]


def test_bare_article_number_is_not_a_citation():
    """没有「本法/本条例」也没有可解析法律名的裸引用不抽。"""
    assert extract("第十二条 本法自公布之日起施行。") == []


def test_whitespace_from_pdf_line_breaks_is_ignored():
    refs = extract("依照本法第八十 七条的规定处理。")
    assert [ref.target for ref in refs] == ["87"]


def test_article_with_subarticle():
    refs = extract("依照本法第二十四条之一的规定。")
    assert [ref.target for ref in refs] == ["24之1"]


def test_evidence_points_back_at_the_text():
    """边是从文本抽的，就要能指回文本。"""
    refs = extract("依照本法第八十七条的规定处理。")
    assert refs[0].evidence == "本法第八十七条"
