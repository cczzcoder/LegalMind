"""解析器注册表（设计 §4、§17.2）。

按媒体类型选择第 1 层解析器；PDF 后端由配置 ``parsing_pdf_backend`` 决定，架构上两个后端
可互换。**第 3 层 Docling 明确暂缓引入**——避免拉取大模型权重造成镜像膨胀与维护成本，
待 P4 检索确需表格结构化时再按需评估；因此这里不注册任何第 3 层解析器（``LAYER3_PARSERS``
只作占位说明，不含实现）。
"""

from app.core.config import get_settings
from app.modules.documents.validation import DOCX, HTML, PDF, TEXT
from app.modules.parsing.interface import (
    DocumentParser,
    ParseLimits,
    ParseResult,
    UnsupportedFormat,
)
from app.modules.parsing.native.docx import DocxParser
from app.modules.parsing.native.html import HtmlParser
from app.modules.parsing.native.pdf import PdfplumberPdfParser, Pypdfium2PdfParser
from app.modules.parsing.native.text import TextParser

# 第 1 层 PDF 双后端：同名接口，可互换
PDF_BACKENDS: dict[str, type[DocumentParser]] = {
    "pypdfium2": Pypdfium2PdfParser,
    "pdfplumber": PdfplumberPdfParser,
}

# 第 3 层（结构化版面理解，如 Docling）暂缓：键为占位名，值为状态说明，无实现
LAYER3_PARSERS: dict[str, str] = {
    "docling": "deferred: 待 P4 检索需要表格结构化时再评估接入（避免拉取大模型权重）",
}


def get_parser(media_type: str, *, pdf_backend: str | None = None) -> DocumentParser:
    """按媒体类型返回解析器实例；未知类型或未知后端抛 ``UnsupportedFormat``。"""
    backend = pdf_backend or get_settings().parsing_pdf_backend
    if media_type == PDF:
        parser_class = PDF_BACKENDS.get(backend)
        if parser_class is None:
            raise UnsupportedFormat(f"Unknown PDF backend: {backend!r}")
        return parser_class()
    if media_type == DOCX:
        return DocxParser()
    if media_type == HTML:
        return HtmlParser()
    if media_type == TEXT:
        return TextParser()
    raise UnsupportedFormat(f"No parser for media type: {media_type!r}")


def build_limits() -> ParseLimits:
    settings = get_settings()
    return ParseLimits(
        max_bytes=settings.parse_max_bytes,
        max_pages=settings.parse_max_pages,
        max_chars=settings.parse_max_chars,
        memory_budget_bytes=settings.parse_memory_budget_mb * 1024 * 1024,
    )


def parse_document(
    content: bytes,
    media_type: str,
    *,
    pdf_backend: str | None = None,
) -> ParseResult:
    """便利入口：选解析器并应用配置的资源上限。CPU 密集，调用方应放进线程。"""
    parser = get_parser(media_type, pdf_backend=pdf_backend)
    return parser.parse(content, media_type=media_type, limits=build_limits())
