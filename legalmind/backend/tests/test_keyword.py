"""中文关键词检索的候选实现（设计 §8.3）：纯单元测试，不需要数据库。

``_terms`` 的「先切后平」是有意的：整条查询先 ``_flat`` 会把词间空格一起抹掉，多词查询就退化
成一个长词——那正是选型时踩过的坑（``all_terms_flat`` 因此一度在全部多词查询上 0 命中）。
"""

from app.modules.retrieval.keyword import SELECTED, STRATEGIES, _flat, _terms, selected


def test_flat_removes_ascii_and_full_width_spaces():
    assert _flat("为了测 试　制定") == "为了测试制定"
    assert _flat("\u3000减刑\u3000") == "减刑"


def test_terms_split_on_whitespace_and_chinese_punctuation():
    assert _terms("集体商标 证明商标") == ["集体商标", "证明商标"]
    assert _terms("铸牢中华民族共同体意识，应当引导各族人民") == [
        "铸牢中华民族共同体意识",
        "应当引导各族人民",
    ]
    assert _terms("减刑、假释；公示期") == ["减刑", "假释", "公示期"]


def test_terms_keep_the_separators_from_being_swallowed():
    """先切后平：词间分隔符必须真的切开，不能被整条去空白抹平。"""
    assert _terms("减刑 假释 公示期") == ["减刑", "假释", "公示期"]
    assert _terms("  减刑　、假释  ") == ["减刑", "假释"]


def test_selected_strategy_is_one_of_the_candidates():
    assert selected().name == SELECTED
    assert SELECTED in {item.name for item in STRATEGIES}
