"""纯文本解析（第 1 层）。

文件在导入时已校验为 UTF-8（``documents.validation``），这里只做规范化与逐行定位。
纯文本没有版面概念，不产生页记录，也不写 bbox（设计 §6：无页概念的格式不强制页码）。
"""

from app.modules.documents.validation import TEXT
from app.modules.parsing.interface import (
    DocumentAssembler,
    DocumentParser,
    DocumentTooLarge,
    ParseLimits,
    ParseResult,
    enforce_char_limit,
)


class TextParser(DocumentParser):
    name = "native.text"
    version = "1"
    media_types = frozenset({TEXT})

    def parse(self, content: bytes, *, media_type: str, limits: ParseLimits) -> ParseResult:
        if len(content) > limits.max_bytes:
            raise DocumentTooLarge("Text document exceeds the parsing size limit")

        assembler = DocumentAssembler(
            parser=self.name, parser_version=self.version, media_type=media_type
        )
        text = content.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        for ordinal, line in enumerate(text.split("\n")):
            assembler.add_block(line, kind="line", block_id=f"l{ordinal}")
            enforce_char_limit(assembler, limits)
        return assembler.result()
