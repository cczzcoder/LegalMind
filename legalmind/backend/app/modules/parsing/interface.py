"""DocumentParser 接口与规范化输出模型（设计 4、6、7）。

解析分三层，本模块定义第 1 层：

- **第 1 层 原生解析**（``native/``）：纯 CPU、零外发、许可宽松。PDF 用 pypdfium2 或
  pdfplumber（双后端，见 ``registry``），DOCX 用 python-docx，HTML 用 lxml，纯文本直读。
- **第 2 层 OCR**：扫描件/图片型 PDF 的文字识别。**未实现**；PDF 页取不到文本层时在
  ``ParseResult`` 标记 ``needs_review`` 并给出告警，不伪造文本。
- **第 3 层 结构化版面理解**（Docling 等）：表格与复杂版面还原。**明确暂缓引入**，避免
  拉取大模型权重造成镜像膨胀与维护成本；待 P4 检索确需表格结构化时再按需评估（设计 §2、§17.2）。

接口契约（对齐设计 §6 原文定位）：

- 输出规范化文本与块级定位，字段可直接映射到 ``chunk_spans``。
- ``bbox`` 一律归一化到页面 ``[0, 1]``，``coordinate_system="normalized-page"``，
  原点在左上（x 向右、y 向下），这样不同后端产出的定位可比较、可互换。
- 字符偏移 ``char_start``/``char_end`` 相对该解析版本的规范化文本，以 Unicode 码点计数。
- 未知信息（如印刷页码）存为 ``None``，不虚构。
"""

import hashlib
import importlib.metadata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.modules.documents.validation import DOCX, HTML, PDF, TEXT

# 解析质量状态，与 models.PARSE_QUALITY_STATUSES 一致（pending 由登记时使用）
QUALITY_OK = "ok"
QUALITY_NEEDS_REVIEW = "needs_review"

# 设计 §6 约定的归一化坐标系：bbox 相对页面尺寸，原点左上
COORDINATE_SYSTEM = "normalized-page"

# 第 1 层支持的媒体类型
NATIVE_MEDIA_TYPES = frozenset({PDF, DOCX, HTML, TEXT})


class ParseError(Exception):
    """解析失败；消息可安全写入任务错误字段（不含原件内容）。"""


def package_version(distribution: str) -> str:
    """查询已安装库的版本，用于把库版本记入 ``parser_version``（复现与去重需要）。"""
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


class UnsupportedFormat(ParseError):
    """没有可用的解析器处理该媒体类型。"""


class DocumentTooLarge(ParseError):
    """文件超出配置的解析上限，为避免 OOM 主动拒绝（设计 14.2）。"""


class MemoryBudgetExceeded(ParseError):
    """解析过程中内存增长超过预算，主动中止（16 GB 单机防护，设计 14.2）。"""


@dataclass(frozen=True)
class ParseLimits:
    """单次解析的资源上限；由配置注入，避免在解析器里硬编码。"""

    max_bytes: int
    max_pages: int
    max_chars: int
    memory_budget_bytes: int


@dataclass(frozen=True)
class Block:
    """规范化文本中的一个块，带其在原件中的定位（设计 §6）。

    ``bbox`` 为归一化页面坐标 ``(x0, y0, x1, y1)``；无版面概念的格式（HTML、TXT）为 ``None``，
    此时 ``coordinate_system`` 也为 ``None``（数据库约束要求两者同时有或同时无）。
    """

    ordinal: int
    kind: str
    text: str
    char_start: int
    char_end: int
    page_index: int | None = None
    printed_page_label: str | None = None
    block_id: str | None = None
    coordinate_system: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    level: int | None = None


@dataclass(frozen=True)
class Page:
    """原件的页；无页概念的格式不产生页记录（设计 §6）。"""

    index: int
    char_start: int
    char_end: int
    width: float | None = None
    height: float | None = None
    printed_page_label: str | None = None
    has_text: bool = True


@dataclass(frozen=True)
class ParseResult:
    """解析产物：规范化文本 + 块级定位 + 页索引。"""

    parser: str
    parser_version: str
    media_type: str
    text: str
    blocks: tuple[Block, ...] = ()
    pages: tuple[Page, ...] = ()
    quality_status: str = QUALITY_OK
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def text_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def char_count(self) -> int:
        return len(self.text)


class DocumentParser(ABC):
    """解析器统一接口（设计 §4）。实现不得访问网络、不得调用外部服务。"""

    name: str
    version: str
    media_types: frozenset[str]

    def supports(self, media_type: str) -> bool:
        return media_type in self.media_types

    @abstractmethod
    def parse(self, content: bytes, *, media_type: str, limits: ParseLimits) -> ParseResult:
        """把原件字节解析为规范化文本与块级定位。

        内容不合法或超出资源上限时抛出 ``ParseError`` 子类；不得返回半成品。
        """


@dataclass(frozen=True)
class TextSpan:
    start: int
    end: int
    text: str


def clean_text(text: str) -> str:
    """规范化单个块的文本：统一行尾、去掉行尾空白、去掉首尾空白。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


class NormalizedTextBuilder:
    """按块累积规范化文本，返回每块的 Unicode 码点区间。

    规范化规则必须稳定可复现，因为 ``ParseRevision.text_sha256`` 与所有 ``char_start``/
    ``char_end`` 都相对它：块内行尾统一为 ``\\n``、去行尾空白、去首尾空白；块与块之间用单个
    ``\\n`` 分隔；空块被跳过。
    """

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._length = 0

    @property
    def length(self) -> int:
        return self._length

    def add(self, text: str) -> TextSpan | None:
        cleaned = clean_text(text)
        if not cleaned:
            return None
        if self._parts:
            self._length += 1  # 块间分隔符
        start = self._length
        self._parts.append(cleaned)
        self._length += len(cleaned)
        return TextSpan(start=start, end=self._length, text=cleaned)

    def build(self) -> str:
        return "\n".join(self._parts)


class DocumentAssembler:
    """累积块与页几何，生成 ``ParseResult``；各解析器共用，保证输出一致。"""

    def __init__(self, *, parser: str, parser_version: str, media_type: str) -> None:
        self._parser = parser
        self._parser_version = parser_version
        self._media_type = media_type
        self._builder = NormalizedTextBuilder()
        self._blocks: list[Block] = []
        self._geometry: dict[int, tuple[float | None, float | None]] = {}

    def set_page_geometry(self, index: int, width: float | None, height: float | None) -> None:
        """登记页尺寸；无文本的页也要登记，才能被记为 needs_review 而非漏掉。"""
        self._geometry[index] = (width, height)

    @property
    def char_count(self) -> int:
        """当前已累积的规范化文本长度，供解析器在循环里执行上限检查。"""
        return self._builder.length

    def add_block(
        self,
        text: str,
        *,
        kind: str,
        page_index: int | None = None,
        printed_page_label: str | None = None,
        block_id: str | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        level: int | None = None,
    ) -> Block | None:
        span = self._builder.add(text)
        if span is None:
            return None
        block = Block(
            ordinal=len(self._blocks),
            kind=kind,
            text=span.text,
            char_start=span.start,
            char_end=span.end,
            page_index=page_index,
            printed_page_label=printed_page_label,
            block_id=block_id,
            coordinate_system=COORDINATE_SYSTEM if bbox is not None else None,
            bbox=bbox,
            level=level,
        )
        self._blocks.append(block)
        return block

    def result(
        self,
        *,
        quality_status: str = QUALITY_OK,
        warnings: tuple[str, ...] = (),
    ) -> ParseResult:
        return ParseResult(
            parser=self._parser,
            parser_version=self._parser_version,
            media_type=self._media_type,
            text=self._builder.build(),
            blocks=tuple(self._blocks),
            pages=self._build_pages(),
            quality_status=quality_status,
            warnings=tuple(warnings),
        )

    def _build_pages(self) -> tuple[Page, ...]:
        ranges: dict[int, list[int]] = {}
        labels: dict[int, str | None] = {}
        for block in self._blocks:
            if block.page_index is None:
                continue
            current = ranges.get(block.page_index)
            if current is None:
                ranges[block.page_index] = [block.char_start, block.char_end]
            else:
                current[0] = min(current[0], block.char_start)
                current[1] = max(current[1], block.char_end)
            labels.setdefault(block.page_index, block.printed_page_label)

        pages = []
        for index in sorted(set(ranges) | set(self._geometry)):
            start, end = ranges.get(index, (0, 0))
            width, height = self._geometry.get(index, (None, None))
            pages.append(
                Page(
                    index=index,
                    char_start=start,
                    char_end=end,
                    width=width,
                    height=height,
                    printed_page_label=labels.get(index),
                    has_text=end > start,
                )
            )
        return tuple(pages)


def enforce_char_limit(assembler: DocumentAssembler, limits: ParseLimits) -> None:
    """累积文本超过上限时立即中止，避免继续吃内存（设计 §14.2）。"""
    if limits.max_chars and assembler.char_count > limits.max_chars:
        raise DocumentTooLarge(f"Parsed text exceeds {limits.max_chars} characters")


def normalize_bbox(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    width: float,
    height: float,
) -> tuple[float, float, float, float] | None:
    """把页面坐标（左上原点，单位为点）归一化到 ``[0, 1]``，并裁剪越界值。

    宽高非法时返回 ``None``（宁可没有 bbox，也不写错坐标）。
    """
    if not width or not height or width <= 0 or height <= 0:
        return None

    def clamp(value: float) -> float:
        return min(1.0, max(0.0, value))

    left, right = sorted((x0 / width, x1 / width))
    top, bottom = sorted((y0 / height, y1 / height))
    return (clamp(left), clamp(top), clamp(right), clamp(bottom))
