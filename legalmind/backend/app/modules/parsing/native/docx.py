"""DOCX 解析（第 1 层，python-docx）。

按正文顺序遍历段落与表格（``body`` 的子元素顺序），避免段落和表格被分开处理而丢顺序。
DOCX 没有页概念，不产生页记录，也不写 bbox（设计 §6）。

注意：本层只依据 Word 的段落样式（``Heading 1`` / ``标题 1``）识别标题。实际入库的法律文本
常把「第 X 章」「第 X 条」写成普通段落，法律结构映射待法律版本切片定型（见 ``models.Chunk``
的 ``structure_path`` 注释），不在此处臆测。
"""

import io
import re
import zipfile

import docx
from docx.document import Document as DocxDocument
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.modules.documents.validation import DOCX
from app.modules.parsing.interface import (
    DocumentAssembler,
    DocumentParser,
    DocumentTooLarge,
    ParseError,
    ParseLimits,
    ParseResult,
    enforce_char_limit,
    package_version,
)

_HEADING = re.compile(r"^(?:Heading|标题)\s*([1-9])$")


def _iter_body(document: DocxDocument):
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def _paragraph_kind(paragraph: Paragraph) -> tuple[str, int | None]:
    match = _HEADING.match(paragraph.style.name or "")
    if match:
        return "heading", int(match.group(1))
    return "paragraph", None


class DocxParser(DocumentParser):
    name = "native.docx"
    version = f"1+python-docx{package_version('python-docx')}"
    media_types = frozenset({DOCX})

    def parse(self, content: bytes, *, media_type: str, limits: ParseLimits) -> ParseResult:
        if len(content) > limits.max_bytes:
            raise DocumentTooLarge("DOCX document exceeds the parsing size limit")

        try:
            document = docx.Document(io.BytesIO(content))
        except (PackageNotFoundError, zipfile.BadZipFile, KeyError) as error:
            raise ParseError("DOCX document could not be parsed") from error

        assembler = DocumentAssembler(
            parser=self.name, parser_version=self.version, media_type=media_type
        )
        paragraph_index = 0
        table_index = 0
        for item in _iter_body(document):
            if isinstance(item, Paragraph):
                if item.text.strip():
                    kind, level = _paragraph_kind(item)
                    assembler.add_block(
                        item.text, kind=kind, block_id=f"p{paragraph_index}", level=level
                    )
                paragraph_index += 1
            elif isinstance(item, Table):
                for row_index, row in enumerate(item.rows):
                    for cell_index, cell in enumerate(row.cells):
                        assembler.add_block(
                            cell.text,
                            kind="table_cell",
                            block_id=f"t{table_index}r{row_index}c{cell_index}",
                        )
                table_index += 1
            enforce_char_limit(assembler, limits)
        return assembler.result()
