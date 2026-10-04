"""检索的规范化（设计 §8.3）：纯单元测试，不需要数据库。

写入与检索必须用同一套规范化口径，否则精确匹配会漏——这两个函数就是那套口径本身。
"""

import pytest

from app.modules.documents.validation import TEXT
from app.modules.legal_corpus.metadata import normalize_document_number
from app.modules.legal_corpus.structure import detect_structure, normalize_article_number
from app.modules.parsing.interface import ParseLimits
from app.modules.parsing.native.text import TextParser


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("中华人民共和国主席令第七十七号", "中华人民共和国主席令第77号"),
        # 空白（含全角空格）一律去掉
        ("主席令第 七十七 号", "主席令第77号"),
        ("中华人民共和国主席令第七十七号\u3000", "中华人民共和国主席令第77号"),
        # 全角数字转半角（公报文本会用全角）
        ("国务院令第８２８号", "国务院令第828号"),
        # 已经是阿拉伯数字的保持原样
        ("国务院令第828号", "国务院令第828号"),
        # 非「第X号」的中文数字不动，避免误改
        ("某某某十号文件", "某某某十号文件"),
        # 解析不出来的中文数字保持原样，不猜
        ("主席令第〇号", "主席令第〇号"),
    ],
)
def test_normalize_document_number(raw, expected):
    assert normalize_document_number(raw) == expected


def test_normalize_document_number_is_idempotent():
    once = normalize_document_number("中华人民共和国主席令第七十七号")
    assert normalize_document_number(once) == once


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("第八十七条", "87"),
        ("87", "87"),
        ("第一条", "1"),
        ("第一千二百四十二条", "1242"),
        ("第八十七条之一", "87之1"),
        ("87之1", "87之1"),
        # 只取条号，款/项被忽略
        ("第一条第二款", "1"),
        ("第一百零一条　第三项", "101"),
    ],
)
def test_normalize_article_number(raw, expected):
    assert normalize_article_number(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "abc", "第八十", "第", "之一"])
def test_normalize_article_number_rejects_unparsable(raw):
    """解析不出来返回 None——调用方据此返回空结果，而不是退化成不过滤。"""
    assert normalize_article_number(raw) is None


def test_normalize_article_number_matches_detection():
    """规范化口径与识别口径一致：识别出的条号再规范化应当不变。"""
    limits = ParseLimits(
        max_bytes=1024 * 1024,
        max_pages=10,
        max_chars=100_000,
        memory_budget_bytes=64 * 1024 * 1024,
    )
    blocks = (
        TextParser()
        .parse(
            "示例法\n第一条　正文。\n第一百零一条　正文。\n第一百零一条之一　正文。\n".encode(),
            media_type=TEXT,
            limits=limits,
        )
        .blocks
    )
    outline = detect_structure(blocks)
    for article in outline.articles:
        assert normalize_article_number(article.label) == article.number
