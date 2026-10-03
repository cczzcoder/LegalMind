"""解析落库（设计 §7、§12.1、§21.3）。

流水线顺序：解析 → 结构化识别 → 分块与原文定位 → **数据准入与脱敏** → 入库 → **法律版本落库**。

- 解析本身 CPU 密集、可能耗时，放在**事务外**的线程里执行。
- 写入解析版本、分块、定位、脱敏映射、审计与 Outbox 在**同一个事务**内完成，任一失败整体
  回滚，不产生半成品（设计 §12.1）。
- **先定位、后脱敏**（设计 §21.3）：分块边界与 ``chunk_spans`` 偏移都在**原文本**坐标上；
  入库的 ``chunks.text`` 是**脱敏文本**，原文本不落库、由映射表可逆重建。
  ``parse_revisions.text_sha256`` 记的是**原文本**的哈希。

重复解析同一 ``(原件, 解析器, 解析器版本, 配置版本)`` 是幂等的，返回既有解析版本（设计 §7.1）。

**法律版本落库**（设计 §5.1、§5.2、§7）与分块在同一事务内完成：识别到法律结构时，把本次解析
挂到 ``legal_instruments`` / ``legal_versions`` / ``provision_identities`` / ``provision_versions``
上；识别不到结构（非法律文本）则跳过。见 ``legal_corpus.service.link_legal_version``。
"""

import asyncio
import hashlib
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage import LocalFileStorage
from app.core.security import Principal
from app.models import Chunk, ChunkSpan, ParseRevision, SourceArtifact
from app.modules.authorization.grants import record_event
from app.modules.documents.service import PARSE_CONFIG_VERSION
from app.modules.legal_corpus.service import ArticleChunk, link_legal_version
from app.modules.legal_corpus.structure import detect_structure
from app.modules.parsing.chunking import chunk_blocks, text_sha256
from app.modules.parsing.interface import (
    DocumentTooLarge,
    MemoryBudgetExceeded,
    ParseError,
)
from app.modules.parsing.registry import parse_document
from app.modules.redaction.detector import EntityDetector
from app.modules.redaction.service import persist_redaction, redact_spans, redacted_slice


async def parse_artifact(
    session: AsyncSession,
    storage: LocalFileStorage,
    principal: Principal,
    document_id: UUID,
    *,
    pdf_backend: str | None = None,
    detector: EntityDetector | None = None,
) -> ParseRevision:
    """解析原件并落库，返回解析版本。

    错误契约（HTTP 状态码在这里同时充当调用方的错误分类信号，因为解析的消费者是后台
    worker 而不是路由；见 ``app/workers/handlers.py`` 的映射表）：

    - 404 原件不存在
    - 413 超出解析资源上限（``DocumentTooLarge``，确定性失败，重试无用）
    - 422 解析被拒（``ParseError``，如文件损坏，确定性失败）
    - 503 原件缺失或与登记哈希不符（可能恢复，可重试）
    - 507 内存增长超预算（``MemoryBudgetExceeded``，资源释放后重试可能成功，FR-13）
    """
    artifact = await _load_artifact(session, document_id)
    # 先取出主键：事务回滚会让会话里的实例 expire，之后再读属性会触发隐式 IO 而报错
    artifact_id = artifact.id
    content = await _read_verified_content(storage, artifact)

    try:
        result = await asyncio.to_thread(
            parse_document, content, artifact.media_type, pdf_backend=pdf_backend
        )
    except DocumentTooLarge as error:
        raise HTTPException(status_code=413, detail=str(error)) from None
    except MemoryBudgetExceeded as error:
        raise HTTPException(status_code=507, detail=str(error)) from None
    except ParseError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None

    # 设计 §7 的顺序：结构化识别在分块之前，分块据此对齐到「条」
    outline = detect_structure(result.blocks)
    chunks = chunk_blocks(result.blocks, result.text, outline=outline)
    # 先定位、后脱敏：分块完成后整篇脱敏一次，占位符在全文范围内保持一致（设计 §21.3）
    _, entities, redaction_spans = redact_spans(result.text, detector)
    try:
        async with session.begin():
            revision = ParseRevision(
                artifact_id=artifact_id,
                parser=result.parser,
                parser_version=result.parser_version,
                config_version=PARSE_CONFIG_VERSION,
                # 原文本的哈希：可用 restore(入库文本, 映射) 重建后校验映射未被篡改
                text_sha256=result.text_sha256,
                quality_status=result.quality_status,
                created_by=principal.user_id,
            )
            session.add(revision)
            await session.flush()

            article_chunks: list[ArticleChunk] = []
            for chunk in chunks:
                # 入库的是脱敏文本；chunk_spans 偏移仍指向原文本
                stored_text = redacted_slice(
                    result.text, redaction_spans, chunk.char_start, chunk.char_end
                )
                chunk_row = Chunk(
                    parse_revision_id=revision.id,
                    ordinal=chunk.ordinal,
                    structure_path=chunk.structure_path,
                    text=stored_text,
                    text_sha256=text_sha256(stored_text),
                )
                session.add(chunk_row)
                await session.flush()
                article_chunks.append(
                    ArticleChunk(
                        chunk_id=chunk_row.id,
                        structure_path=chunk.structure_path,
                        text=stored_text,
                    )
                )
                for span in chunk.spans:
                    session.add(
                        ChunkSpan(
                            chunk_id=chunk_row.id,
                            ordinal=span.ordinal,
                            page_index=span.block.page_index,
                            printed_page_label=span.block.printed_page_label,
                            block_id=span.block.block_id,
                            char_start=span.block.char_start,
                            char_end=span.block.char_end,
                            coordinate_system=span.block.coordinate_system,
                            bbox=list(span.block.bbox) if span.block.bbox else None,
                            text_sha256=span.text_sha256,
                        )
                    )

            # 映射与业务变更同事务（设计 §12.1）；审计只记条数与类型
            persist_redaction(session, principal, revision.id, entities)

            # 法律版本落库（设计 §5.2、§7）：识别到结构才挂版本树，非法律文本跳过
            linking = None
            if outline.detected:
                linking = await link_legal_version(
                    session,
                    principal,
                    artifact=artifact,
                    preamble_blocks=[block.text for block in result.blocks[: outline.body_start]],
                    article_chunks=article_chunks,
                    today=datetime.now(UTC).date(),
                )

            record_event(
                session,
                principal,
                artifact_id,
                "document.parsed",
                {
                    "document_id": str(artifact_id),
                    "parse_revision_id": str(revision.id),
                    "parser": result.parser,
                    "parser_version": result.parser_version,
                    "config_version": PARSE_CONFIG_VERSION,
                    "media_type": artifact.media_type,
                    "quality_status": result.quality_status,
                    "page_count": result.page_count,
                    "chunk_count": len(chunks),
                    "char_count": result.char_count,
                    "redacted_entity_count": len(entities),
                    "article_count": len(outline.articles),
                    "structure_detected": outline.detected,
                    "numbering_issues": list(outline.numbering_issues),
                    "warnings": list(result.warnings),
                    "legal_version_id": str(linking.legal_version_id) if linking else None,
                    "instrument_id": str(linking.instrument_id) if linking else None,
                    "provision_count": linking.provision_count if linking else 0,
                    "legal_version_review_status": linking.review_status if linking else None,
                    "legal_version_conflict": linking.conflict if linking else False,
                },
            )
            await session.flush()
            await session.refresh(revision)
    except IntegrityError:
        # 同一 (原件, 解析器, 版本, 配置) 已存在：幂等返回既有解析版本（设计 §7.1）
        async with session.begin():
            existing = await session.scalar(
                select(ParseRevision).where(
                    ParseRevision.artifact_id == artifact_id,
                    ParseRevision.parser == result.parser,
                    ParseRevision.parser_version == result.parser_version,
                    ParseRevision.config_version == PARSE_CONFIG_VERSION,
                )
            )
        if existing is None:
            raise
        return existing

    return revision


async def _load_artifact(session: AsyncSession, document_id: UUID) -> SourceArtifact:
    """原件属公共法律数据、全局共享（设计 §21.2），不按组织过滤。"""
    async with session.begin():
        artifact = await session.scalar(
            select(SourceArtifact).where(SourceArtifact.id == document_id)
        )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return artifact


async def _read_verified_content(storage: LocalFileStorage, artifact: SourceArtifact) -> bytes:
    try:
        content = await asyncio.to_thread(storage.open, artifact.object_key)
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="Original file unavailable") from None

    # 原件与登记哈希不符时不解析，避免把被替换的文件当作原件（设计 §16.2）
    if hashlib.sha256(content).hexdigest() != artifact.sha256:
        raise HTTPException(status_code=503, detail="Original file failed integrity check")
    return content
