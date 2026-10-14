"""HTML 解析（第 1 层，lxml）。

按设计 §6：HTML 使用内容快照及 DOM/文本定位，不强制生成页码——这里不产生页记录，也不写 bbox。
只取「叶级块元素」（不含嵌套块元素的块级标签）的文本，避免父子容器重复计入同一段文字。
"""

import lxml.html
from lxml import etree

from app.modules.documents.validation import HTML
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

_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "td",
        "th",
        "dt",
        "dd",
        "pre",
        "blockquote",
        "figcaption",
        "caption",
        "article",
        "section",
        "aside",
        "header",
        "footer",
        "main",
        "nav",
        "tr",
        "ul",
        "ol",
        "table",
        "body",
    }
)
# 不参与正文的元素；连同子树一起丢弃
_SKIP_TAGS = ("script", "style", "noscript", "template")


def _kind_and_level(tag: str) -> tuple[str, int | None]:
    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return "heading", int(tag[1])
    if tag == "li":
        return "list_item", None
    if tag in {"td", "th"}:
        return "table_cell", None
    if tag == "pre":
        return "preformatted", None
    return "paragraph", None


def _leaf_blocks(root: etree._Element) -> list[etree._Element]:
    """返回不含嵌套块元素的块级元素，保持文档顺序。"""
    containers: set[etree._Element] = set()
    for element in root.iter():
        if element.tag in _BLOCK_TAGS:
            parent = element.getparent()
            while parent is not None:
                if parent.tag in _BLOCK_TAGS:
                    containers.add(parent)
                parent = parent.getparent()
    return [
        element
        for element in root.iter()
        if element.tag in _BLOCK_TAGS and element not in containers
    ]


class HtmlParser(DocumentParser):
    name = "native.html"
    version = f"1+lxml{package_version('lxml')}"
    media_types = frozenset({HTML})

    def parse(self, content: bytes, *, media_type: str, limits: ParseLimits) -> ParseResult:
        if len(content) > limits.max_bytes:
            raise DocumentTooLarge("HTML document exceeds the parsing size limit")

        # ⚠️⚠️ **NUL 字节必须自己判，不能指望 libxml2**。`b"\x00"` 是**合法 UTF-8**，能过下面的
        # 解码；而 libxml2 对它的处置**随版本而变**——实测（2026-10-14）同一个 lxml 6.1.3：
        #   libxml2 2.11（Windows 本机）→ 把整份文档判成空，抛 `ParserError: Document is empty`
        #   libxml2 2.14（Linux / CI）  → 替换成 U+FFFD 后照常解析，返回一份垃圾文档
        # 于是「不可解析的输入」在旧版上抛错、在新版上**静默返回空内容**——同一份代码两个平台
        # 两种行为，而 CI 恰好是后者。这里自己判，让行为与 libxml2 版本无关；
        # HTML 里 NUL 本就是非法字符，拒绝它不会有任何正当输入被误伤。
        if b"\x00" in content:
            raise ParseError("HTML document contains NUL bytes")

        # no_network 禁联网；huge_tree=False 限制深度与文本长度，防实体膨胀/深层嵌套打爆内存
        parser = lxml.html.HTMLParser(no_network=True, huge_tree=False, recover=True)
        try:
            # 导入校验已保证 UTF-8；显式解码，避免 libxml2 在缺 meta charset 时按 Latin-1 误读
            document = content.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ParseError("HTML document is not valid UTF-8") from error

        try:
            root = lxml.html.document_fromstring(document, parser=parser)
        except (etree.ParserError, etree.XMLSyntaxError, ValueError) as error:
            raise ParseError("HTML document could not be parsed") from error

        for tag in _SKIP_TAGS:
            for element in root.xpath(f".//{tag}"):
                element.drop_tree()

        assembler = DocumentAssembler(
            parser=self.name, parser_version=self.version, media_type=media_type
        )
        for ordinal, element in enumerate(_leaf_blocks(root)):
            kind, level = _kind_and_level(element.tag)
            assembler.add_block(
                element.text_content(),
                kind=kind,
                block_id=f"h{ordinal}",
                level=level,
            )
            enforce_char_limit(assembler, limits)
        return assembler.result()
