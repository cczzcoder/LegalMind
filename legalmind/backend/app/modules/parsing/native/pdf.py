"""PDF 解析（第 1 层，双后端：pypdfium2 / pdfplumber）。

两个后端实现同一个内部契约 ``_iter_pages``，共用基类完成规范化文本、块定位与页索引，
因此可以按配置互换而不影响调用方（见 ``registry``）。

- **pypdfium2**：PDFium C 库，逐字符 box，速度快、内存低，定位粒度到字符。
- **pdfplumber**：pdfminer.six，布局归并更强（word/line），速度慢、内存高。

具体选型待样本基准（``evaluations``）按设计 §6 精度要求拍板，本文件不预设赢家。
定位统一为块级（一行一个块），``bbox`` 归一化到页面 ``[0, 1]``、原点左上。
印刷页码在本层不检测，保持 ``None``（设计 §6：未知不虚构）。
"""

import io
from abc import abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass

from app.modules.documents.validation import PDF
from app.modules.parsing.interface import (
    QUALITY_NEEDS_REVIEW,
    QUALITY_OK,
    DocumentAssembler,
    DocumentParser,
    DocumentTooLarge,
    ParseError,
    ParseLimits,
    ParseResult,
    enforce_char_limit,
    normalize_bbox,
    package_version,
)
from app.modules.parsing.memory import MemoryGuard

# 本文件解析逻辑的版本；库版本一并记入 parser_version，便于复现与去重（设计 §7.1）
_LOGIC_VERSION = "1"


@dataclass(frozen=True)
class _Line:
    """一行文本及其在页面上的框（左上原点，单位为点）。"""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float


class _PdfParser(DocumentParser):
    """PDF 解析基类：把后端的逐页输出统一成 ParseResult。"""

    media_types = frozenset({PDF})

    @abstractmethod
    def _iter_pages(
        self, content: bytes, limits: ParseLimits
    ) -> Iterator[tuple[int, float, float, list[_Line]]]:
        """产出 ``(页序号, 宽, 高, 行列表)``；实现须逐页处理并及时释放页对象。"""

    def parse(self, content: bytes, *, media_type: str, limits: ParseLimits) -> ParseResult:
        if len(content) > limits.max_bytes:
            raise DocumentTooLarge("PDF document exceeds the parsing size limit")

        assembler = DocumentAssembler(
            parser=self.name, parser_version=self.version, media_type=media_type
        )
        empty_pages = 0
        with MemoryGuard(limits.memory_budget_bytes) as guard:
            for page_index, width, height, lines in self._iter_pages(content, limits):
                if limits.max_pages and page_index + 1 > limits.max_pages:
                    raise DocumentTooLarge(f"PDF exceeds {limits.max_pages} pages")
                assembler.set_page_geometry(page_index, width, height)
                if not lines:
                    empty_pages += 1
                for line_index, line in enumerate(lines):
                    assembler.add_block(
                        line.text,
                        kind="line",
                        page_index=page_index,
                        block_id=f"p{page_index}b{line_index}",
                        bbox=normalize_bbox(line.x0, line.y0, line.x1, line.y1, width, height),
                    )
                enforce_char_limit(assembler, limits)
                guard.check()

        warnings: list[str] = []
        if assembler.char_count == 0:
            # 取不到任何文本层：多为扫描件，需第 2 层 OCR；不伪造文本（设计 §17.2）
            warnings.append("no_text_layer")
        elif empty_pages:
            warnings.append(f"pages_without_text:{empty_pages}")
        quality = QUALITY_NEEDS_REVIEW if warnings else QUALITY_OK
        return assembler.result(quality_status=quality, warnings=tuple(warnings))


class Pypdfium2PdfParser(_PdfParser):
    name = "native.pdf.pypdfium2"
    version = f"{_LOGIC_VERSION}+pdfium{package_version('pypdfium2')}"

    def _iter_pages(
        self, content: bytes, limits: ParseLimits
    ) -> Iterator[tuple[int, float, float, list[_Line]]]:
        import pypdfium2 as pdfium

        try:
            document = pdfium.PdfDocument(content)
        except pdfium.PdfiumError as error:
            raise ParseError("PDF document could not be parsed") from error

        try:
            for page_index, page in enumerate(document):
                width, height = page.get_size()
                textpage = page.get_textpage()
                try:
                    lines = _pypdfium2_lines(textpage, height)
                finally:
                    textpage.close()
                    page.close()
                yield page_index, width, height, lines
        finally:
            document.close()


def _pypdfium2_lines(textpage, page_height: float) -> list[_Line]:
    """逐字符取 box，按 PDFium 给出的换行符切行，合并成行级框。"""
    count = textpage.count_chars()
    if not count:
        return []

    raw = textpage.get_text_range(0, count)
    lines: list[_Line] = []
    chars: list[str] = []
    bounds: list[float] | None = None

    def flush() -> None:
        nonlocal bounds
        if chars:
            lines.append(
                _Line(text="".join(chars), x0=bounds[0], y0=bounds[1], x1=bounds[2], y1=bounds[3])
            )
        chars.clear()
        bounds = None

    for index in range(count):
        char = raw[index]
        if char in "\r\n":
            flush()
            continue
        # pypdfium2 返回 (left, bottom, right, top)，原点在左下，需转成左上原点
        left, bottom, right, top = textpage.get_charbox(index)
        top_from_top = page_height - top
        bottom_from_top = page_height - bottom
        if bounds is None:
            bounds = [left, top_from_top, right, bottom_from_top]
        else:
            bounds[0] = min(bounds[0], left)
            bounds[1] = min(bounds[1], top_from_top)
            bounds[2] = max(bounds[2], right)
            bounds[3] = max(bounds[3], bottom_from_top)
        chars.append(char)
    flush()
    return lines


class PdfplumberPdfParser(_PdfParser):
    name = "native.pdf.pdfplumber"
    version = f"{_LOGIC_VERSION}+pdfplumber{package_version('pdfplumber')}"

    def _iter_pages(
        self, content: bytes, limits: ParseLimits
    ) -> Iterator[tuple[int, float, float, list[_Line]]]:
        import pdfplumber
        from pdfminer.pdfparser import PDFSyntaxError
        from pdfplumber.utils.exceptions import PdfminerException

        try:
            document = pdfplumber.open(io.BytesIO(content))
        except (PDFSyntaxError, PdfminerException, ValueError) as error:
            raise ParseError("PDF document could not be parsed") from error

        try:
            for page_index, page in enumerate(document.pages):
                try:
                    lines = [
                        _Line(
                            text=line["text"],
                            x0=line["x0"],
                            y0=line["top"],
                            x1=line["x1"],
                            y1=line["bottom"],
                        )
                        for line in page.extract_text_lines()
                    ]
                    yield page_index, page.width, page.height, lines
                finally:
                    # 逐页释放布局对象，避免整篇缓存在内存里（设计 §14.2）
                    page.close()
        finally:
            document.close()
