"""分块与原文定位物化（设计 §6、§7）。

设计 §7 的顺序是"结构化识别 → … → 分块与原文定位"，所以分块**优先对齐到「条」**：给了结构
大纲（``legal_corpus.structure``）时，先按条段切分，再在段内按字符上限细分；没有识别到结构
（非法律文本、或识别不出条）时，回退为按字符上限切分。

无论走哪条路径，边界都只落在**块边界**上，因此每个 chunk 的 text 都是规范化文本的一段连续
切片，``"\\n".join(chunk.text for chunk in chunks) == result.text`` 恒成立——整篇文本可由
chunk 还原，与 ``ParseRevision.text_sha256``（**脱敏前**原文的哈希）一致。

这里产出的是**脱敏前**的原文切片；入库前由流水线按设计 §21.3 换成脱敏文本，届时
``chunks.text`` 存脱敏形态、``chunk_spans`` 偏移仍指向原文本，两者的对应关系由
``redaction.service.redacted_slice`` 维护。
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from app.modules.legal_corpus.structure import StructureOutline
from app.modules.parsing.interface import Block

# 单个 chunk 的目标字符上限；块不可再分，故超长单块会独占一个 chunk
DEFAULT_MAX_CHARS = 800


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SpanDraft:
    """一个 chunk 在某块上的定位草稿，字段直接映射到 ``chunk_spans``。"""

    ordinal: int
    block: Block
    text_sha256: str


@dataclass(frozen=True)
class ChunkDraft:
    """分块草稿：``text`` 是**脱敏前**的原文切片，区间为 ``[char_start, char_end)``。

    入库时由流水线按 ``RedactionSpan`` 换成脱敏文本（设计 §21.3），因此这里保留原文形态与
    原文本坐标——分块边界和 ``chunk_spans`` 偏移都在原文本坐标上。
    """

    ordinal: int
    char_start: int
    char_end: int
    text: str
    spans: tuple[SpanDraft, ...]
    structure_path: dict | None = None


def _segments(
    blocks: Sequence[Block], outline: StructureOutline | None
) -> list[tuple[int, int, dict | None]]:
    """返回 ``(起始块, 结束块, 结构路径)`` 列表；无结构时整篇作为一段。

    路径取**该条的起字符**位置，因此段首的编/章/节标题也被算进这条的路径里。
    """
    if outline is None or not outline.detected:
        return [(0, len(blocks), None)]

    segments: list[tuple[int, int, dict | None]] = []
    # 正文之前的内容（标题、公布信息、目录）不属于任何条，路径为空
    first_start = outline.article_segments[0][0]
    if first_start > 0:
        segments.append((0, first_start, None))
    for (start, end), article in zip(outline.article_segments, outline.articles):
        segments.append((start, end, outline.path_at(article.char_start) or None))
    return segments


def chunk_blocks(
    blocks: Sequence[Block],
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    outline: StructureOutline | None = None,
) -> list[ChunkDraft]:
    """按块边界切分 ``blocks``，``text`` 为该解析版本的规范化文本。

    给了 ``outline`` 时按条段切分（每个 chunk 自带编/章/节/条路径），否则按 ``max_chars`` 切分。
    """
    chunks: list[ChunkDraft] = []
    for start, end, path in _segments(blocks, outline):
        _emit_segment(chunks, blocks[start:end], text, max_chars, path)
    return chunks


def _emit_segment(
    chunks: list[ChunkDraft],
    blocks: Sequence[Block],
    text: str,
    max_chars: int,
    structure_path: dict | None,
) -> None:
    """把一段（一条，或整篇）按字符上限细分成若干 chunk。"""
    current: list[Block] = []
    size = 0

    def flush() -> None:
        nonlocal current, size
        if not current:
            return
        start = current[0].char_start
        end = current[-1].char_end
        chunks.append(
            ChunkDraft(
                ordinal=len(chunks),
                char_start=start,
                char_end=end,
                text=text[start:end],
                spans=tuple(
                    SpanDraft(ordinal=index, block=block, text_sha256=text_sha256(block.text))
                    for index, block in enumerate(current)
                ),
                structure_path=structure_path,
            )
        )
        current = []
        size = 0

    for block in blocks:
        if current and size + len(block.text) > max_chars:
            flush()
        current.append(block)
        size += len(block.text)
    flush()
