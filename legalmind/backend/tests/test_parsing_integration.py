"""解析落库集成测试（设计 §6、§7、§12.1）：需要 TEST_DATABASE_URL。

覆盖：解析版本/分块/定位落库、文本可由 chunk 还原、§6 定位字段、审计与 Outbox 同事务、
重复解析幂等、无文本层标 needs_review、原件缺失或被替换拒绝、解析失败返回 422。
"""

import hashlib
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.adapters.storage import LocalFileStorage, get_storage
from app.core.security import Principal
from app.models import (
    AuditEvent,
    Chunk,
    ChunkSpan,
    OutboxEvent,
    ParseRevision,
    RedactionEntity,
    SourceArtifact,
)
from app.modules.documents.service import PARSE_CONFIG_VERSION
from app.modules.parsing.service import parse_artifact
from app.modules.redaction.service import RedactedEntity, restore
from tests.helpers import build_minimal_pdf, import_document_for_parsing

pytestmark = pytest.mark.anyio


@pytest.fixture
def storage(tmp_path, make_client):
    # make_client 结束时会清空 dependency_overrides
    from app.main import app

    storage = LocalFileStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: storage
    return storage


def _principal(user) -> Principal:
    return Principal(
        organization_id=user.organization_id,
        user_id=user.id,
        roles=frozenset(),
    )


def _unique_pdf(pages: list[list[str]]) -> bytes:
    """原件按 sha256 全库唯一（设计 21.2），测试每次用唯一内容避免冲突。"""
    return build_minimal_pdf(pages, marker=uuid4().hex)


async def test_parse_pdf_persists_revision_chunks_and_spans(
    make_client, make_user, storage, session_factory
):
    content = _unique_pdf([["Hello Legal Mind", "Second line"], ["Page two text"]])
    editor, document_id = await import_document_for_parsing(
        make_client, make_user, content, "sample.pdf"
    )

    async with session_factory() as session:
        revision = await parse_artifact(session, storage, _principal(editor), document_id)

    assert revision.parser == "native.pdf.pypdfium2"
    assert revision.config_version == PARSE_CONFIG_VERSION
    assert revision.quality_status == "ok"
    assert revision.artifact_id == document_id

    async with session_factory() as session:
        chunks = list(
            await session.scalars(
                select(Chunk).where(Chunk.parse_revision_id == revision.id).order_by(Chunk.ordinal)
            )
        )
        spans = list(
            await session.scalars(
                select(ChunkSpan)
                .join(Chunk, ChunkSpan.chunk_id == Chunk.id)
                .where(Chunk.parse_revision_id == revision.id)
                .order_by(Chunk.ordinal, ChunkSpan.ordinal)
            )
        )
        mapping_rows = list(
            await session.scalars(
                select(RedactionEntity).where(RedactionEntity.parse_revision_id == revision.id)
            )
        )
        actions = list(
            await session.scalars(
                select(AuditEvent.action).where(AuditEvent.resource_id == document_id)
            )
        )
        outbox = await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.payload["parse_revision_id"].astext == str(revision.id))
        )

    mappings = [
        RedactedEntity(row.entity_type, row.plaintext, row.placeholder) for row in mapping_rows
    ]
    # 入库的是脱敏文本；用映射还原后应等于原文本，其哈希与解析版本记录一致（设计 §21.3）
    stored = "\n".join(chunk.text for chunk in chunks)
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    restored = restore(stored, mappings)
    assert hashlib.sha256(restored.encode("utf-8")).hexdigest() == revision.text_sha256

    # 本文档没有个人信息，脱敏是空操作：入库文本与原文切片逐字相同
    assert mappings == []
    assert all(
        chunk.text == stored[chunk_spans[0].char_start : chunk_spans[-1].char_end]
        for chunk, chunk_spans in _spans_by_chunk(chunks, spans)
    )

    # 定位字段对齐设计 §6
    assert [span.page_index for span in spans] == [0, 0, 1]
    assert all(span.coordinate_system == "normalized-page" for span in spans)
    assert all(span.bbox is not None and len(span.bbox) == 4 for span in spans)
    assert all(span.char_end > span.char_start for span in spans)
    assert all(span.block_id for span in spans)

    assert actions == ["document.imported", "document.parsed"]
    assert outbox == 1


def _spans_by_chunk(chunks, spans):
    grouped = {chunk.id: [] for chunk in chunks}
    for span in spans:
        grouped[span.chunk_id].append(span)
    return [(chunk, grouped[chunk.id]) for chunk in chunks]


async def test_reparse_is_idempotent(make_client, make_user, storage, session_factory):
    content = _unique_pdf([["Hello Legal Mind"]])
    editor, document_id = await import_document_for_parsing(
        make_client, make_user, content, "same.pdf"
    )
    principal = _principal(editor)

    async with session_factory() as session:
        first = await parse_artifact(session, storage, principal, document_id)
    async with session_factory() as session:
        second = await parse_artifact(session, storage, principal, document_id)

    assert first.id == second.id
    async with session_factory() as session:
        revisions = await session.scalar(
            select(func.count())
            .select_from(ParseRevision)
            .where(ParseRevision.artifact_id == document_id)
        )
        chunks = await session.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.parse_revision_id == first.id)
        )
    assert revisions == 1
    assert chunks == 1


async def test_pdf_without_text_layer_records_needs_review(
    make_client, make_user, storage, session_factory
):
    content = _unique_pdf([[]])
    editor, document_id = await import_document_for_parsing(
        make_client, make_user, content, "scan.pdf"
    )

    async with session_factory() as session:
        revision = await parse_artifact(session, storage, _principal(editor), document_id)

    assert revision.quality_status == "needs_review"
    async with session_factory() as session:
        payload = await session.scalar(
            select(AuditEvent.payload).where(
                AuditEvent.resource_id == document_id,
                AuditEvent.action == "document.parsed",
            )
        )
    assert payload["warnings"] == ["no_text_layer"]
    assert payload["chunk_count"] == 0


async def test_text_document_parses_without_page_or_bbox(
    make_client, make_user, storage, session_factory
):
    content = f"第一条　测试条文\n第二条　又一条\n{uuid4().hex}\n".encode()
    editor, document_id = await import_document_for_parsing(
        make_client, make_user, content, "law.txt"
    )

    async with session_factory() as session:
        revision = await parse_artifact(session, storage, _principal(editor), document_id)
    assert revision.parser == "native.text"

    async with session_factory() as session:
        spans = list(
            await session.scalars(
                select(ChunkSpan)
                .join(Chunk, ChunkSpan.chunk_id == Chunk.id)
                .where(Chunk.parse_revision_id == revision.id)
            )
        )
    # 无页概念的格式不写页码与 bbox（设计 §6）
    assert all(span.page_index is None for span in spans)
    assert all(span.coordinate_system is None and span.bbox is None for span in spans)


async def test_parse_failure_returns_422(make_client, make_user, storage, session_factory):
    # 通过导入校验（无主动内容特征）但无法真正解析的 PDF
    editor, document_id = await import_document_for_parsing(
        make_client, make_user, f"%PDF-1.7\nbroken body {uuid4().hex}".encode(), "broken.pdf"
    )

    async with session_factory() as session:
        with pytest.raises(HTTPException) as error:
            await parse_artifact(session, storage, _principal(editor), document_id)
    assert error.value.status_code == 422

    async with session_factory() as session:
        revisions = await session.scalar(
            select(func.count())
            .select_from(ParseRevision)
            .where(ParseRevision.artifact_id == document_id)
        )
    assert revisions == 0


async def test_unknown_document_returns_404(make_client, make_user, storage, session_factory):
    editor = await make_user("editor")
    async with session_factory() as session:
        with pytest.raises(HTTPException) as error:
            await parse_artifact(session, storage, _principal(editor), uuid4())
    assert error.value.status_code == 404


async def test_missing_or_tampered_file_is_refused(
    make_client, make_user, storage, session_factory
):
    content = _unique_pdf([["Hello Legal Mind"]])
    editor, document_id = await import_document_for_parsing(
        make_client, make_user, content, "tamper.pdf"
    )
    principal = _principal(editor)

    async with session_factory() as session:
        key = await session.scalar(
            select(SourceArtifact.object_key).where(SourceArtifact.id == document_id)
        )
    path = storage._path(key)

    path.write_bytes(b"tampered")
    async with session_factory() as session:
        with pytest.raises(HTTPException) as tampered:
            await parse_artifact(session, storage, principal, document_id)

    path.unlink()
    async with session_factory() as session:
        with pytest.raises(HTTPException) as missing:
            await parse_artifact(session, storage, principal, document_id)

    assert tampered.value.status_code == missing.value.status_code == 503


async def test_public_document_parsable_across_organizations(
    make_client, make_user, storage, session_factory
):
    """原件属公共法律数据、全局共享（设计 21.2）：其他组织也能解析。"""
    content = _unique_pdf([["Hello Legal Mind"]])
    _, document_id = await import_document_for_parsing(
        make_client, make_user, content, "shared.pdf"
    )
    outsider = await make_user("editor")

    async with session_factory() as session:
        revision = await parse_artifact(session, storage, _principal(outsider), document_id)
    assert revision.artifact_id == document_id


async def test_personal_information_is_redacted_before_storage(
    make_client, make_user, storage, session_factory
):
    """入库的是脱敏文本，原文本不落库、由映射表可逆重建（设计 §21.3）。"""
    # 内容足够长以产生多个 chunk，让"按原文本区间取脱敏切片"真正生效；
    # 原件按 sha256 全库唯一（设计 21.2），末尾补随机行保证每次内容不同
    content = (
        "第一条　原告身份证110101199001011234，案号（2023）京01民终1234号。\n"
        "第二条　被告联系电话13800138000。\n"
        f"第三条　{'为保护当事人合法权益，' * 100}\n"
        f"第四条　{uuid4().hex}\n"
    ).encode()
    editor, document_id = await import_document_for_parsing(
        make_client, make_user, content, "judgment.txt"
    )

    async with session_factory() as session:
        revision = await parse_artifact(session, storage, _principal(editor), document_id)

    async with session_factory() as session:
        chunks = list(
            await session.scalars(
                select(Chunk).where(Chunk.parse_revision_id == revision.id).order_by(Chunk.ordinal)
            )
        )
        spans = list(
            await session.scalars(
                select(ChunkSpan)
                .join(Chunk, ChunkSpan.chunk_id == Chunk.id)
                .where(Chunk.parse_revision_id == revision.id)
                .order_by(Chunk.ordinal, ChunkSpan.ordinal)
            )
        )
        mapping_rows = list(
            await session.scalars(
                select(RedactionEntity).where(RedactionEntity.parse_revision_id == revision.id)
            )
        )
        parsed_payload = await session.scalar(
            select(AuditEvent.payload).where(
                AuditEvent.resource_id == document_id,
                AuditEvent.action == "document.parsed",
            )
        )

    assert len(chunks) > 1, "本用例需要多 chunk 才能验证按区间切片"
    stored = "\n".join(chunk.text for chunk in chunks)
    mappings = [
        RedactedEntity(row.entity_type, row.plaintext, row.placeholder) for row in mapping_rows
    ]

    # 1) 明文不入库：chunks.text 里只有占位符
    for plaintext in ("110101199001011234", "（2023）京01民终1234号", "13800138000"):
        assert plaintext not in stored
    assert "[身份证_1]" in stored and "[案号_1]" in stored and "[电话_1]" in stored

    # 2) 明文只存在于映射表（系统内最敏感的数据）
    assert {row.plaintext for row in mapping_rows} == {
        "110101199001011234",
        "（2023）京01民终1234号",
        "13800138000",
    }
    assert {row.placeholder for row in mapping_rows} == {"[身份证_1]", "[案号_1]", "[电话_1]"}

    # 3) 可逆重建：脱敏文本 + 映射 = 原文本，其哈希等于解析版本记录（可校验映射未被篡改）
    restored = restore(stored, mappings)
    assert hashlib.sha256(restored.encode("utf-8")).hexdigest() == revision.text_sha256

    # 4) chunk_spans 偏移指向**原文本**，而入库文本已脱敏——两者坐标不同，这是设计 §21.3 的规定
    grouped = _spans_by_chunk(chunks, spans)
    first_chunk, first_spans = grouped[0]
    original_slice = restored[first_spans[0].char_start : first_spans[-1].char_end]
    assert "110101199001011234" in original_slice
    assert "110101199001011234" not in first_chunk.text

    # 5) 审计只记条数与类型，不记明文
    payload = json.dumps(parsed_payload, ensure_ascii=False)
    assert parsed_payload["redacted_entity_count"] == 3
    assert "110101199001011234" not in payload
    assert "京01民终1234号" not in payload

    # 6) 脱敏后分块仍完整可用
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.text for chunk in chunks)


async def test_legal_structure_is_detected_and_persisted(
    make_client, make_user, storage, session_factory
):
    """分块对齐到条并落库结构路径（设计 §5.2、§7）。"""
    content = (
        "中华人民共和国示例法\n"
        "第一章　总则\n"
        "第一条　为了示例，制定本法。\n"
        "第二条　本法适用于示例活动。\n"
        "第二章　附则\n"
        "第三条　本法自公布之日起施行。\n"
        f"公布日期占位：{uuid4().hex}\n"
    ).encode()
    editor, document_id = await import_document_for_parsing(
        make_client, make_user, content, "demo.txt"
    )

    async with session_factory() as session:
        revision = await parse_artifact(session, storage, _principal(editor), document_id)

    async with session_factory() as session:
        chunks = list(
            await session.scalars(
                select(Chunk).where(Chunk.parse_revision_id == revision.id).order_by(Chunk.ordinal)
            )
        )
        payload = await session.scalar(
            select(AuditEvent.payload).where(
                AuditEvent.resource_id == document_id,
                AuditEvent.action == "document.parsed",
            )
        )

    # 第 0 段是正文之前的内容（标题），不属于任何条
    assert chunks[0].structure_path is None
    assert chunks[0].text == "中华人民共和国示例法"

    # 每条一个 chunk，段首带上章标题，结构路径完整
    assert len(chunks) == 4
    assert chunks[1].text.startswith("第一章　总则\n第一条")
    assert chunks[1].structure_path == {
        "chapter": "第一章 总则",
        "article": "第一条",
        "article_number": "1",
    }
    assert chunks[2].structure_path["article_number"] == "2"
    # 换章后不继承上一章的信息（示例法没有节，路径里就不该有 section）
    assert chunks[3].text.startswith("第二章　附则\n第三条")
    assert chunks[3].structure_path["chapter"] == "第二章 附则"
    assert "section" not in chunks[3].structure_path

    # 审计记录结构识别结果；无实体时入库文本即原文，可直接校验哈希
    assert payload["structure_detected"] is True
    assert payload["article_count"] == 3
    assert payload["numbering_issues"] == []
    joined = "\n".join(chunk.text for chunk in chunks)
    assert hashlib.sha256(joined.encode("utf-8")).hexdigest() == revision.text_sha256
