"""中文关键词检索的候选实现（设计 §8.3、§8.2）。

§8.3 要求「中文关键词方案通过 KeywordSearch 适配器接入，**用测试集选型**」。本模块放的是**候选**
实现：同一个匹配接口、同一套授权与效力过滤，只有匹配与排序方式不同；由
``backend/scripts/evaluate_retrieval.py`` 在金标准上比出赢家，再决定谁接入检索通路。

**为什么不用 PostgreSQL 自带的全文检索**：默认解析器把一整段连续中日韩字符当成**一个词元**
（实测 ``to_tsvector('simple', '中华人民共和国监狱法第一条')`` 只产出一个词元），中文切不出词；
``zhparser`` / ``pg_bigm`` 这类中文扩展本机不可用。因此候选都建在子串匹配与 ``pg_trgm`` 上，
两者都在现有 PostgreSQL 内，不新增常驻组件（设计 §18）。

**折行空格是这里的头号陷阱**：PDF 解析出来的文本会带折行空格（实测「民用航 空器」），
按原文直接子串匹配会漏。所以候选各自处理：去空白后再匹配，或用三元组相似度。
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import ColumnElement, func, literal

from app.models import ProvisionVersion

_WS = re.compile(r"[\s\u3000]+")
# 查询切词：空白与常见中文顿断都算分隔符（用户会把带标点的短语整段粘进来）
_SPLIT = re.compile(r"[\s\u3000，、；;]+")

Match = Callable[[str], ColumnElement[bool]]
Order = Callable[[str], list]


def _flat(value: str) -> str:
    """去掉全部空白（含全角空格）。查询侧与列侧必须同口径，否则折行空格会让匹配漏掉。"""
    return _WS.sub("", value)


def _flat_column() -> ColumnElement:
    """去空白后的条款文本（列侧）。先去掉 ASCII 空白，再去掉全角空格。"""
    return func.replace(func.regexp_replace(ProvisionVersion.text, r"\s", "", "g"), "\u3000", "")


def _terms(query: str) -> list[str]:
    """切词：按空白与中文顿断切开，再各自去空白。

    注意**先切后平**：整条查询先 ``_flat`` 会把词间空格也抹掉，多词查询就退化成一个长词。
    """
    return [flat for flat in (_flat(part) for part in _SPLIT.split(query)) if flat]


def _like_all(terms: list[str]) -> ColumnElement[bool]:
    condition = _flat_column().like(f"%{terms[0]}%")
    for term in terms[1:]:
        condition = condition & _flat_column().like(f"%{term}%")
    return condition


def _substring_raw(query: str) -> ColumnElement[bool]:
    return ProvisionVersion.text.like(f"%{query}%")


def _substring_flat(query: str) -> ColumnElement[bool]:
    return _like_all([_flat(query)])


def _all_terms_flat(query: str) -> ColumnElement[bool]:
    return _like_all(_terms(query))


def _trigram(query: str) -> ColumnElement[bool]:
    # pg_trgm 的 % 走三元组相似度（GIN/GiST 索引可加速）；阈值由 pg_trgm.similarity_threshold 控制
    return ProvisionVersion.text.op("%")(query)


def _word_trigram(query: str) -> ColumnElement[bool]:
    """pg_trgm 的「词相似度」算子：把**短查询**放进**长正文**里找最佳匹配段。

    `%` 用的是两边三元组集合的整体相似度——查询短、条款长时并集很大，相似度天然很低，
    短查询几乎过不了阈值。`<%` 专为此设计，更适合「短语 → 长条文」。

    **算子两边的顺序有讲究**：`<%` 把**第一个操作数**当作要找的那段（needle），所以必须是
    `查询 <% 条款文本`。写成 `条款文本 <% 查询` 会反过来，短查询同样过不了阈值（实测 28 条
    全部 0 命中）。
    """
    return literal(query).op("<%")(ProvisionVersion.text)


def _no_order(query: str) -> list:
    return []


def _shortest_first(query: str) -> list:
    """子串匹配没有相似度可用，用「条文越短越聚焦」当排序代理。"""
    return [func.length(_flat_column()).asc()]


def _similarity_order(query: str) -> list:
    return [func.similarity(ProvisionVersion.text, query).desc()]


def _word_similarity_order(query: str) -> list:
    return [func.word_similarity(query, ProvisionVersion.text).desc()]


@dataclass(frozen=True)
class Strategy:
    name: str
    summary: str
    match: Match
    order: Order


STRATEGIES: tuple[Strategy, ...] = (
    Strategy(
        name="substring_raw",
        summary="原始子串匹配（基线）：不改文本，直接 ILIKE 整条查询，无排序",
        match=_substring_raw,
        order=_no_order,
    ),
    Strategy(
        name="substring_flat",
        summary="去空白后子串匹配：两边都去空白以规避 PDF 折行空格；按条文长度排序",
        match=_substring_flat,
        order=_shortest_first,
    ),
    Strategy(
        name="all_terms_flat",
        summary="去空白 + 多词 AND：按空白/顿断切词，要求每个词都命中（顺序无关）",
        match=_all_terms_flat,
        order=_shortest_first,
    ),
    Strategy(
        name="trigram",
        summary="pg_trgm 整体相似度（%）：容忍插入/错字，短查询对长条文天然吃亏",
        match=_trigram,
        order=_similarity_order,
    ),
    Strategy(
        name="word_trigram",
        summary="pg_trgm 词相似度（<%）：短查询在长条文里找最佳匹配段，专为这个场景设计",
        match=_word_trigram,
        order=_word_similarity_order,
    ),
)

# 金标准选型结果（2026-10-04，28 条查询 / 38 个期望条款）：
# all_terms_flat Recall 1.0 / 准确率 1.0 / MRR 1.0；substring_raw 0.5（折行空格漏一半）；
# word_trigram 0.68 / 准确率 0.81；trigram 0.08。报告见 evaluations/reports/retrieval_eval_*.md。
SELECTED = "all_terms_flat"


def selected() -> Strategy:
    """接入检索通路的方案。改选型时改 ``SELECTED`` 并重跑 ``scripts/evaluate_retrieval.py``。"""
    return next(item for item in STRATEGIES if item.name == SELECTED)
