"""导入文件检查（FR-02）单元测试，不需要数据库。"""

import io
import zipfile

import pytest

from app.modules.documents.validation import (
    DOCX,
    HTML,
    PDF,
    TEXT,
    RejectedFile,
    clean_filename,
    detect_media_type,
)

CONTENT_TYPES = (
    b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
    b'content-types"><Override PartName="/word/document.xml" ContentType="application/'
    b'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
)


def make_docx(extra: dict[str, bytes] | None = None, content_types: bytes = CONTENT_TYPES) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", "<w:document>第一条　正文。</w:document>")
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_accepts_supported_types():
    assert detect_media_type("法.docx", make_docx()) == DOCX
    assert detect_media_type("a.pdf", b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\n") == PDF
    assert detect_media_type("a.html", b"<!DOCTYPE html><html><body>x</body></html>") == HTML
    assert detect_media_type("a.htm", b"<!-- c --><html lang='zh'>x</html>") == HTML
    assert detect_media_type("a.txt", "第一条　正文。".encode()) == TEXT
    assert detect_media_type("a.TXT", "﻿带 BOM".encode()) == TEXT


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("a.txt", b"", "empty"),
        ("a.exe", b"MZ\x90\x00", "Only DOCX"),
        ("a.doc", b"\xd0\xcf\x11\xe0", "Only DOCX"),
        ("a.docx", b"%PDF-1.7", "does not match"),
        ("a.pdf", make_docx(), "does not match"),
        ("a.txt", b"<html><body>x</body></html>", "does not match"),
        ("a.html", b"plain text", "does not match"),
        ("a.txt", b"\xff\xfe\x00a", "NUL"),
        ("a.txt", b"\xb5\xda\xd2\xbb", "UTF-8"),
        ("a.docx", b"PK\x03\x04garbage", "valid DOCX"),
        ("a.pdf", b"%PDF-1.7 /OpenAction << /S /JavaScript >>", "scripts"),
        ("a.pdf", b"%PDF-1.7 /EmbeddedFile", "scripts"),
    ],
)
def test_rejects_invalid_files(filename, content, message):
    with pytest.raises(RejectedFile, match=message):
        detect_media_type(filename, content)


def test_rejects_docx_missing_main_part():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
    with pytest.raises(RejectedFile, match="valid DOCX"):
        detect_media_type("a.docx", buffer.getvalue())


def test_rejects_docx_with_macros():
    with pytest.raises(RejectedFile, match="macros"):
        detect_media_type("a.docx", make_docx({"word/vbaProject.bin": b"\x00" * 10}))
    with pytest.raises(RejectedFile, match="macros"):
        detect_media_type(
            "a.docx",
            make_docx(content_types=CONTENT_TYPES.replace(b"document.main", b"document.macroEnabled.main")),
        )


def test_rejects_docx_with_embedded_objects():
    with pytest.raises(RejectedFile, match="embedded"):
        detect_media_type("a.docx", make_docx({"word/embeddings/oleObject1.bin": b"x"}))


def test_rejects_compression_bomb():
    # 10 MB 的零压缩后只有约 10 KB，压缩比远超上限
    with pytest.raises(RejectedFile, match="compression ratio"):
        detect_media_type("a.docx", make_docx({"word/media/big.bin": b"\x00" * 10_000_000}))


def test_clean_filename_strips_paths_and_control_characters():
    assert clean_filename("../../etc/劳动法.docx") == "劳动法.docx"
    assert clean_filename("C:\\Users\\x\\劳动法.docx") == "劳动法.docx"
    assert clean_filename("a\x00b\n.txt") == "ab.txt"
    for name in ("../", "..", " ", "\x00"):
        with pytest.raises(RejectedFile):
            clean_filename(name)
