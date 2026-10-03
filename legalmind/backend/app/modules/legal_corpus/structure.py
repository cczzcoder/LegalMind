"""法律结构识别（设计 §5.2、§6、§7）。

从解析产物里识别 **编 / 章 / 节 / 条** 四级结构，供分块对齐与后续的法律版本落库使用。
设计 §7 的流水线把"结构化识别"放在"分块与原文定位"之前，所以本模块在分块前运行。

16 份真实法律文本的实测结论（决定了这里的模型）：

- **条号全文连续**：《生态环境法典》含 5 编、1242 条，编号 1…1242 无跳号，因此
  ``(法, 条, N)`` 可以作为唯一身份。
- **章号、节号会随上级重置**：法典的章号重置 4 次（每编从第一章重新开始）、节号重置 14 次，
  所以章/节**不能**用 ``(法, 类型, 编号)`` 唯一标识——它们只作结构路径保存，不建身份。
- **「目录」会重复一遍章/节标题**：12 份带目录的文档，章/节计数正好翻倍。识别时必须排除。

**PDF 折行带来的假标题**：PDF 是逐行解析的，正文里被断到行首的交叉引用看起来就像标题，
例如「第十六条第一款、第十九条…」「第二十八条至第三十条规定…」。这里用两条规则排除：
编号**回退**的必是引用；编号**超前**时必须上一行确实结束了一句话。

**未建模的层级**：款（无编号；PDF 逐行解析下与普通换行无法区分）、项（``（一）``，嵌套位置
依赖款）、目。这些层级的文本仍完整保留在分块里，只是不单独建身份。
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.modules.parsing.interface import Block

# 结构层级；与 models.PROVISION_TYPES 的取值一致（这里只识别前四级）
PART = "part"
CHAPTER = "chapter"
SECTION = "section"
ARTICLE = "article"

_ORDER = (PART, CHAPTER, SECTION, ARTICLE)
_KINDS = {"编": PART, "章": CHAPTER, "节": SECTION}

# 编/章/节是标题行，必须短；正文里以「第X章」开头的长句不算标题。
# 条不设长度上限——一条就是整段正文。
MAX_HEADING_CHARS = 50

# 标题里不会出现句读；PDF 折行可能把交叉引用断到行首，用句读把它排除掉
_SENTENCE_PUNCTUATION = "，。；！？"

# 一句话的结束标点。新的一条必须起于上一段结束之后；注意**不含**右括号——折行常停在
# 「《伦理办法》」这类词之后，把它当句子结束会漏判
_SENTENCE_END = "。！？；："

# 紧跟在这些标点后的「第X条」是并列引用（如「第二十条、第二十一条规定的…」），不是标题
_REFERENCE_TAIL = "、，。；："

_CN = "零一二三四五六七八九十百千"
_CN_DIGITS = {char: index for index, char in enumerate("零一二三四五六七八九")}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}

_ARTICLE = re.compile(rf"^第([{_CN}]+)条(?:之([{_CN}]+))?")
_HEADING = re.compile(rf"^第([{_CN}]+)(编|章|节)")


def chinese_number_to_int(text: str) -> int | None:
    """中文数字转整数（支持 零/十/百/千，覆盖 1–9999）；无法解析时返回 None。"""
    if not text:
        return None
    section = 0
    number = 0
    for char in text:
        if char == "零":
            number = 0
        elif char in _CN_DIGITS:
            number = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            section += (number or 1) * _CN_UNITS[char]
            number = 0
        else:
            return None
    return section + number


@dataclass(frozen=True)
class Heading:
    """一个结构标题；``number`` 是规范化编号（条为 ``"101"`` 或 ``"101之1"``）。"""

    kind: str
    number: str
    label: str
    title: str
    char_start: int
    char_end: int
    block_ordinal: int

    @property
    def display(self) -> str:
        """用于结构路径展示，如 ``"第一章 基本规定"``；无标题时只有标记。"""
        return f"{self.label} {self.title}".strip()


@dataclass(frozen=True)
class StructureOutline:
    """结构大纲。``article_segments`` 与 ``articles`` 一一对应，是各条覆盖的块区间
    ``[start, end)``，段首含该条的编/章/节标题——这样每个分块自带章节上下文。
    ``body_start`` 之前的内容（标题、公布信息、目录）不属于任何条。
    """

    headings: tuple[Heading, ...]
    articles: tuple[Heading, ...]
    body_start: int
    article_segments: tuple[tuple[int, int], ...]
    numbering_issues: tuple[str, ...]

    @property
    def detected(self) -> bool:
        return bool(self.articles)

    def path_at(self, char_start: int) -> dict[str, str]:
        """返回该字符位置所处的结构路径；不在任何条款内时返回空字典。

        更浅的层级会重置更深的层级（编重置章/节/条，章重置节/条），否则上一章的「节」会
        泄漏到下一章的条上。
        """
        current: dict[str, str] = {}
        for heading in self.headings:
            if heading.char_start > char_start:
                break
            if heading.kind == ARTICLE:
                current["article"] = heading.display
                current["article_number"] = heading.number
                continue
            for deeper in _ORDER[_ORDER.index(heading.kind) + 1 :]:
                current.pop(deeper, None)
            current[heading.kind] = heading.display
        return current


def _article_candidate(block: Block) -> tuple[str, str] | None:
    """与上下文无关的条标题判定，返回 ``(规范化编号, 原文标记)``。"""
    match = _ARTICLE.match(block.text)
    if match is None:
        return None
    if block.text[match.end() : match.end() + 1] in _REFERENCE_TAIL:
        return None
    value = chinese_number_to_int(match.group(1))
    if value is None:
        return None
    suffix = match.group(2)
    if suffix:
        return f"{value}之{chinese_number_to_int(suffix)}", match.group(0)
    return str(value), match.group(0)


def _structure_heading(block: Block) -> Heading | None:
    text = block.text
    if len(text) > MAX_HEADING_CHARS or any(char in text for char in _SENTENCE_PUNCTUATION):
        return None
    match = _HEADING.match(text)
    if match is None:
        return None
    value = chinese_number_to_int(match.group(1))
    if value is None:
        return None
    label = match.group(0)
    return Heading(
        kind=_KINDS[match.group(2)],
        number=str(value),
        label=label,
        title=re.sub(r"\s+", " ", text[match.end() :]).strip(),
        char_start=block.char_start,
        char_end=block.char_end,
        block_ordinal=block.ordinal,
    )


def _previous_ends_clause(previous: Block | None) -> bool:
    """上一块是否结束了一句话（或是标题）；否则本块是它的折行。"""
    if previous is None:
        return True
    return previous.text[-1:] in _SENTENCE_END or _structure_heading(previous) is not None


def _plausible_article(number: str, expected: int, previous: Block | None) -> bool:
    """排除 PDF 折行造成的假条标题。

    编号回退（如「第十六条第一款、第十九条…」出现在第六十七条之后）必是引用；编号超前时
    必须上一块确实结束了一句话，否则同样是被断到行首的交叉引用。
    """
    if "之" in number:  # 「第X条之一」跟随其主条，不参与主编号推进
        return _previous_ends_clause(previous)
    value = int(number)
    if value < expected:
        return False
    if value == expected:
        return True
    return _previous_ends_clause(previous)


def _contiguous_run_start(blocks: Sequence[Block], index: int, floor: int) -> int:
    """从 ``index`` 往前收集连续的编/章/节标题，返回这段标题的起点块序号。

    停在 ``floor``（正文起点）——再往前就是目录，不能把目录标题并进第一条。
    条本身不是编/章/节标题，所以不会把上一条吞进本段。
    """
    start = index
    while start > floor:
        if _structure_heading(blocks[start - 1]) is None:
            break
        start -= 1
    return start


def _numbering_issues(articles: Sequence[Heading]) -> tuple[str, ...]:
    """检查条号是否连续；不连续只作提示，不改变识别结果。"""
    issues: list[str] = []
    expected = 1
    for heading in articles:
        if "之" in heading.number:
            continue
        actual = int(heading.number)
        if actual != expected:
            issues.append(f"条号不连续：期望 {expected}，实际 {actual}")
            expected = actual
        expected += 1
    return tuple(issues)


def detect_structure(blocks: Sequence[Block]) -> StructureOutline:
    """识别法律结构；识别不到条时返回空大纲（分块会回退到按字符上限切分）。"""
    if not blocks:
        return StructureOutline((), (), 0, (), ())

    articles: list[Heading] = []
    headings: list[Heading] = []
    expected = 1
    for index, block in enumerate(blocks):
        previous = blocks[index - 1] if index else None
        candidate = _article_candidate(block)
        if candidate is not None and _plausible_article(candidate[0], expected, previous):
            number, label = candidate
            if "之" not in number:
                expected = int(number) + 1
            heading = Heading(
                kind=ARTICLE,
                number=number,
                label=label,
                title="",
                char_start=block.char_start,
                char_end=block.char_end,
                block_ordinal=block.ordinal,
            )
            articles.append(heading)
            headings.append(heading)
            continue
        structure = _structure_heading(block)
        if structure is not None:
            headings.append(structure)

    if not articles:
        return StructureOutline((), (), 0, (), ())

    # 正文起点：从第一条往前回溯编/章/节标题串，直到遇到**最外层**层级的「第一X」。
    # 其之前的标题属于目录（目录会把章/节标题重复一遍），不建身份。
    outermost = min((h.kind for h in headings), key=_ORDER.index)
    body_start = articles[0].block_ordinal
    index = body_start
    while index > 0:
        previous = _structure_heading(blocks[index - 1])
        if previous is None:
            break
        index -= 1
        body_start = index
        if previous.kind == outermost and previous.number == "1":
            break

    headings = [h for h in headings if h.block_ordinal >= body_start]
    articles = [h for h in articles if h.block_ordinal >= body_start]

    # 每条一段：段首含该条的编/章/节标题；末段延伸到全文结束
    starts = [_contiguous_run_start(blocks, h.block_ordinal, body_start) for h in articles]
    segments: list[tuple[int, int]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(blocks)
        if start < end:
            segments.append((start, end))

    return StructureOutline(
        headings=tuple(headings),
        articles=tuple(articles),
        body_start=body_start,
        article_segments=tuple(segments),
        numbering_issues=_numbering_issues(articles),
    )
