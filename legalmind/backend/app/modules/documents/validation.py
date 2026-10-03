"""导入文件检查（FR-02 第 1、2 步）。

按文件内容判断类型，扩展名必须与内容一致。这里只做结构与已知风险特征检查，
不等于杀毒扫描；未通过的文件直接拒收，不登记、不保存。
"""

import io
import re
import zipfile
from pathlib import PurePath

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF = "application/pdf"
HTML = "text/html"
TEXT = "text/plain"

EXTENSIONS = {".docx": DOCX, ".pdf": PDF, ".html": HTML, ".htm": HTML, ".txt": TEXT}

# docx 解压上限：防止压缩炸弹
MAX_ZIP_ENTRIES = 1000
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100

# PDF 中可执行或携带附件的对象；内容可能藏在压缩流中，此检查不能保证发现
_PDF_ACTIVE = re.compile(rb"/(JavaScript|JS|Launch|EmbeddedFile|OpenAction|AA|RichMedia)\b")
_HTML_START = re.compile(
    r"\s*(<!--.*?-->\s*)*<(!doctype\s+html|html)[\s>]",
    re.IGNORECASE | re.DOTALL,
)


class RejectedFile(ValueError):
    """文件不符合导入要求；消息可安全返回给用户。"""


def clean_filename(name: str) -> str:
    # 只保留文件名部分，去掉控制字符；仅用于展示，不参与存储路径
    base = PurePath(name.replace("\\", "/")).name
    base = "".join(ch for ch in base if ch.isprintable()).strip()
    if not base.strip("."):
        raise RejectedFile("Filename is empty")
    return base[:255]


def _check_docx(content: bytes) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise RejectedFile("File is not a valid DOCX package") from None

    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_ZIP_ENTRIES:
            raise RejectedFile("DOCX package has too many entries")

        total = 0
        for entry in entries:
            if entry.flag_bits & 0x1:
                raise RejectedFile("Encrypted DOCX entries are not accepted")
            total += entry.file_size
            if (
                entry.compress_size
                and entry.file_size / entry.compress_size > MAX_COMPRESSION_RATIO
            ):
                raise RejectedFile("DOCX entry compression ratio is too high")
        if total > MAX_UNCOMPRESSED_BYTES:
            raise RejectedFile("DOCX package is too large when uncompressed")

        names = {entry.filename for entry in entries}
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise RejectedFile("File is not a valid DOCX package")

        # 宏与嵌入对象：.docx 不应包含，出现即拒收
        lowered = [name.lower() for name in names]
        if any(name.endswith("vbaproject.bin") or "/embeddings/" in name for name in lowered):
            raise RejectedFile("DOCX with macros or embedded objects is not accepted")
        content_types = archive.read("[Content_Types].xml").lower()
        if b"macroenabled" in content_types or b"vbaproject" in content_types:
            raise RejectedFile("DOCX with macros or embedded objects is not accepted")


def _decode_text(content: bytes) -> str:
    if b"\x00" in content:
        raise RejectedFile("Text file contains NUL bytes")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise RejectedFile("Text file must be UTF-8 encoded") from None


def detect_media_type(filename: str, content: bytes) -> str:
    """返回内容对应的媒体类型；不合格时抛出 RejectedFile。"""
    if not content:
        raise RejectedFile("File is empty")

    expected = EXTENSIONS.get(PurePath(filename).suffix.lower())
    if expected is None:
        raise RejectedFile("Only DOCX, PDF, HTML and TXT files are accepted")

    if content.startswith(b"PK\x03\x04"):
        actual = DOCX
    elif content.startswith(b"%PDF-"):
        actual = PDF
    else:
        text = _decode_text(content)
        actual = HTML if _HTML_START.match(text) else TEXT

    if actual != expected:
        raise RejectedFile("File content does not match its extension")

    if actual == DOCX:
        _check_docx(content)
    elif actual == PDF and _PDF_ACTIVE.search(content):
        raise RejectedFile("PDF with scripts, actions or attachments is not accepted")

    return actual
