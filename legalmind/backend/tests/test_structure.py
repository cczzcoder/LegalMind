"""法律结构识别（设计 §5.2、§6、§7）：纯单元测试，不需要数据库。

用 ``TextParser`` 把文本切成块，因此断言里的偏移就是真实的规范化文本偏移。
"""

import pytest

from app.modules.documents.validation import TEXT
from app.modules.legal_corpus.structure import (
    ARTICLE,
    CHAPTER,
    PART,
    SECTION,
    StructureOutline,
    chinese_number_to_int,
    detect_structure,
)
from app.modules.parsing.chunking import chunk_blocks
from app.modules.parsing.interface import ParseLimits
from app.modules.parsing.native.text import TextParser

LIMITS = ParseLimits(
    max_bytes=1024 * 1024,
    max_pages=10,
    max_chars=100_000,
    memory_budget_bytes=64 * 1024 * 1024,
)


def blocks_of(text: str):
    return TextParser().parse(text.encode(), media_type=TEXT, limits=LIMITS).blocks


def structure_of(text: str) -> StructureOutline:
    return detect_structure(blocks_of(text))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("一", 1),
        ("十", 10),
        ("十一", 11),
        ("二十", 20),
        ("一百", 100),
        ("一百零一", 101),
        ("一百一十", 110),
        ("一百二十三", 123),
        ("一千", 1000),
        ("一千二百四十二", 1242),
    ],
)
def test_chinese_number_conversion(text, expected):
    assert chinese_number_to_int(text) == expected


def test_chinese_number_rejects_non_numbers():
    assert chinese_number_to_int("") is None
    assert chinese_number_to_int("甲") is None


def test_detects_articles_with_offsets():
    outline = structure_of("第一条 甲。\n第二条 乙。\n第三条 丙。")
    assert [h.number for h in outline.articles] == ["1", "2", "3"]
    assert [h.label for h in outline.articles] == ["第一条", "第二条", "第三条"]
    assert outline.detected is True
    assert outline.numbering_issues == ()
    # 偏移指向规范化文本，标签与正文都在该条段内
    first = outline.articles[0]
    assert first.char_start == 0 and first.char_end > first.char_start


def test_returns_empty_outline_without_articles():
    outline = structure_of("这是一份没有条款的说明材料。\n只有普通段落。")
    assert outline.detected is False
    assert outline.articles == ()
    assert outline.article_segments == ()


def test_article_numbering_issues_are_reported_not_fatal():
    outline = structure_of("第一条 甲。\n第二条 乙。\n第四条 丁。")
    assert [h.number for h in outline.articles] == ["1", "2", "4"]
    assert outline.numbering_issues == ("条号不连续：期望 3，实际 4",)


def test_article_with_suffix_is_detected():
    outline = structure_of("第一条 甲。\n第一条之一 甲附。\n第二条 乙。")
    assert [h.number for h in outline.articles] == ["1", "1之1", "2"]
    # 「之一」不参与主编号推进，因此不报不连续
    assert outline.numbering_issues == ()


def test_part_chapter_section_nesting():
    text = "第一编 甲编\n第一章 甲章\n第一条 甲一。\n第一节 甲节\n第二条 甲二。"
    outline = structure_of(text)
    assert [h.kind for h in outline.headings] == [PART, CHAPTER, ARTICLE, SECTION, ARTICLE]
    assert outline.path_at(outline.articles[0].char_start) == {
        "part": "第一编 甲编",
        "chapter": "第一章 甲章",
        "article": "第一条",
        "article_number": "1",
    }
    assert outline.path_at(outline.articles[1].char_start)["section"] == "第一节 甲节"


def test_shallower_heading_resets_deeper_levels():
    """新的一章不得继承上一章的「节」，否则节的路径会泄漏到下一章的条上。"""
    text = "第一章 甲\n第一条 甲一。\n第一节 甲节\n第二条 甲二。\n第二章 乙\n第三条 乙一。"
    outline = structure_of(text)
    second = outline.path_at(outline.articles[1].char_start)
    assert second["section"] == "第一节 甲节"
    third = outline.path_at(outline.articles[2].char_start)
    assert third["chapter"] == "第二章 乙"
    assert "section" not in third


def test_table_of_contents_is_excluded():
    """目录会把章标题重复一遍；只有正文里的那一份才算结构。"""
    text = "某法\n目　录\n第一章 甲\n第二章 乙\n第一章 甲\n第一条 甲一。\n第二章 乙\n第二条 乙一。"
    outline = structure_of(text)
    chapters = [h for h in outline.headings if h.kind == CHAPTER]
    assert [h.number for h in chapters] == ["1", "2"]
    assert outline.body_start == 4
    # 正文之前的内容不属于任何条
    assert outline.article_segments[0][0] == 4


def test_table_of_contents_with_parts_is_excluded():
    text = "法典\n目　录\n第一编 总则\n第一章 基本规定\n第一编 总则\n第一章 基本规定\n第一条 甲。"
    outline = structure_of(text)
    assert [h.kind for h in outline.headings] == [PART, CHAPTER, ARTICLE]
    assert outline.body_start == 4


def test_reference_at_line_start_is_not_an_article():
    """PDF 折行会把交叉引用断到行首；编号回退的必是引用。"""
    text = "第一条 甲。\n第二条 乙。\n第三条 丙。\n第二条规定的其他情形。"
    outline = structure_of(text)
    assert [h.number for h in outline.articles] == ["1", "2", "3"]
    assert outline.numbering_issues == ()


def test_wrapped_forward_reference_is_not_an_article():
    """编号超前时，上一块必须确实结束一句话，否则本块是上一段的折行。"""
    text = "第一条 甲。\n第二条 乙。\n第三条 本法所称的，包括\n第二十八条至第三十条规定的情形。"
    outline = structure_of(text)
    assert [h.number for h in outline.articles] == ["1", "2", "3"]
    assert outline.numbering_issues == ()


def test_forward_reference_after_a_finished_clause_is_an_article():
    """上一块结束了一句话时，编号超前仍按新的一条接受（容忍源文件跳号）。"""
    text = "第一条 甲。\n第二条 乙。\n第三条 丙。\n第五十条 戊。"
    outline = structure_of(text)
    assert [h.number for h in outline.articles] == ["1", "2", "3", "50"]


def test_parallel_reference_list_is_not_an_article():
    text = "第一条 甲。\n第二条 乙。\n第三条 丙。\n第二十条、第二十一条规定的，从其规定。"
    outline = structure_of(text)
    assert [h.number for h in outline.articles] == ["1", "2", "3"]


def test_long_sentence_starting_with_a_chapter_is_not_a_heading():
    """正文里以「第X章」开头的长句不是标题。"""
    text = "第一章 甲\n第一条 依照第三章 监督管理的规定，作出如下处理决定的，应当报请上级机关批准。"
    outline = structure_of(text)
    chapters = [h for h in outline.headings if h.kind == CHAPTER]
    assert [h.number for h in chapters] == ["1"]


def test_wrapped_cross_reference_to_a_section_is_not_a_heading():
    """「第五节 规定的地方国家机关的职权，同时依照…」是折行，不是节标题。"""
    text = (
        "第一章 甲\n第一节 甲节\n第一条 行使第三节 规定的地方国家机关的职权，同时依照宪法。\n"
        "第二节 乙节\n第二条 乙二。"
    )
    outline = structure_of(text)
    sections = [h.number for h in outline.headings if h.kind == SECTION]
    assert sections == ["1", "2"]


def test_article_segments_cover_the_whole_document():
    text = "某法\n目录\n第一章 甲\n第一条 甲一。\n第二条 甲二。\n第二章 乙\n第三条 乙一。"
    outline = structure_of(text)
    blocks = blocks_of(text)
    # 正文之前 + 每条一段，且段首含该条的章标题
    assert outline.article_segments[0] == (outline.body_start, 4)
    assert outline.article_segments[-1][1] == len(blocks)
    assert outline.article_segments[-1][0] == 5  # 第二章 乙 并入第三条


def test_chunks_align_to_articles_and_carry_structure_path():
    text = "某法\n目录\n第一章 甲\n第一条 甲一。\n第二条 甲二。\n第二章 乙\n第三条 乙一。"
    blocks = blocks_of(text)
    outline = detect_structure(blocks)
    chunks = chunk_blocks(blocks, "\n".join(b.text for b in blocks), outline=outline)

    assert "\n".join(chunk.text for chunk in chunks) == "\n".join(b.text for b in blocks)
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    # 第一段是正文之前的内容（标题 + 目录），没有结构路径
    assert chunks[0].structure_path is None
    assert "第一条" in chunks[1].text
    assert chunks[1].structure_path["article_number"] == "1"
    assert chunks[1].structure_path["chapter"] == "第一章 甲"
    assert chunks[2].structure_path["article_number"] == "2"
    assert chunks[3].structure_path["chapter"] == "第二章 乙"


def test_chunking_falls_back_to_size_when_no_structure():
    text = "\n".join(f"第{index}段 普通内容。" for index in range(1, 41))
    blocks = blocks_of(text)
    plain = chunk_blocks(blocks, text, max_chars=60)
    with_outline = chunk_blocks(blocks, text, max_chars=60, outline=detect_structure(blocks))

    assert len(plain) > 1
    # 识别不到条时，两种调用结果一致
    assert [chunk.text for chunk in with_outline] == [chunk.text for chunk in plain]
    assert all(chunk.structure_path is None for chunk in with_outline)


def test_long_article_is_split_but_keeps_its_path():
    body = "、".join(f"第{index}项内容" for index in range(1, 60))
    text = f"第一章 甲\n第一条 {body}。\n第二条 乙。"
    blocks = blocks_of(text)
    outline = detect_structure(blocks)
    chunks = chunk_blocks(blocks, text, max_chars=80, outline=outline)

    first_article_chunks = [
        chunk for chunk in chunks if (chunk.structure_path or {}).get("article_number") == "1"
    ]
    assert len(first_article_chunks) > 1, "超长的一条应被拆成多个 chunk"
    assert "\n".join(chunk.text for chunk in chunks) == text
