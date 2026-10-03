"""原始资料导入、读取与授权（FR-02、FR-10、设计 7.3、12.1）。

导入顺序：检查文件 → 写入存储（事务外）→ 一个事务内登记文件、解析任务、审计与 Outbox。
事务失败时删除刚写入的文件，数据库中不会出现指向缺失文件的记录。
"""

import asyncio
import hashlib
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage import LocalFileStorage
from app.core.security import Principal
from app.models import AccessGrant, AuditEvent, Job, SourceArtifact
from app.modules.authorization import grants
from app.modules.authorization.grants import record_event
from app.modules.authorization.service import AuthorizationService
from app.modules.documents.validation import RejectedFile, clean_filename, detect_media_type
from app.modules.sources.service import get_org_source

PARSE_JOB = "document.parse"
# 解析处理配置版本；解析器选定后随配置变化递增（设计 7.1）
PARSE_CONFIG_VERSION = "v1"


async def import_document(
    session: AsyncSession,
    storage: LocalFileStorage,
    principal: Principal,
    *,
    source_id: UUID,
    filename: str,
    content: bytes,
    sensitivity: str,
    access_scope: str,
    acquired_at: datetime | None = None,
) -> tuple[SourceArtifact, Job]:
    if sensitivity == "confidential" and access_scope != "restricted":
        raise HTTPException(status_code=422, detail="Confidential documents must be restricted")

    try:
        filename = clean_filename(filename)
        media_type = detect_media_type(filename, content)
    except RejectedFile as error:
        raise HTTPException(status_code=422, detail=str(error)) from None

    sha256 = hashlib.sha256(content).hexdigest()

    async with session.begin():
        await get_org_source(session, principal, source_id)
        # 提前检查只为避免无谓写盘；并发重复由唯一约束兜底
        duplicate = await session.scalar(
            select(SourceArtifact.id).where(
                SourceArtifact.organization_id == principal.organization_id,
                SourceArtifact.sha256 == sha256,
            )
        )
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="Document already imported")

    key = storage.new_key()
    await asyncio.to_thread(storage.put, key, content, sha256)

    try:
        async with session.begin():
            artifact = SourceArtifact(
                organization_id=principal.organization_id,
                source_id=source_id,
                object_key=key,
                sha256=sha256,
                size_bytes=len(content),
                media_type=media_type,
                original_filename=filename,
                sensitivity=sensitivity,
                access_scope=access_scope,
                acquired_at=acquired_at,
                created_by=principal.user_id,
            )
            session.add(artifact)
            await session.flush()

            if access_scope == "restricted":
                grants.add_creator_grant(session, principal, "document", artifact.id)

            # 只登记任务；解析 worker 尚未实现（P3），任务保持 pending
            job = Job(
                organization_id=principal.organization_id,
                job_type=PARSE_JOB,
                payload={"document_id": str(artifact.id)},
                idempotency_key=(
                    f"{principal.organization_id}:{sha256}:{PARSE_JOB}:{PARSE_CONFIG_VERSION}"
                ),
                status="pending",
                attempt_count=0,
                max_attempts=3,
            )
            session.add(job)
            await session.flush()

            record_event(
                session,
                principal,
                artifact.id,
                "document.imported",
                {
                    "document_id": str(artifact.id),
                    "source_id": str(source_id),
                    "sha256": sha256,
                    "size_bytes": len(content),
                    "media_type": media_type,
                    "job_id": str(job.id),
                },
            )
            await session.flush()
            await session.refresh(artifact)
            await session.refresh(job)
    except IntegrityError:
        await asyncio.to_thread(storage.delete, key)
        raise HTTPException(status_code=409, detail="Document already imported") from None
    except BaseException:
        await asyncio.to_thread(storage.delete, key)
        raise

    return artifact, job


async def get_visible_document(
    session: AsyncSession,
    principal: Principal,
    document_id: UUID,
) -> SourceArtifact:
    # 不存在与无权访问返回同样的 404
    artifact = await session.scalar(
        select(SourceArtifact).where(
            SourceArtifact.id == document_id,
            AuthorizationService.document_scope(principal),
        )
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return artifact


async def get_org_document(
    session: AsyncSession,
    principal: Principal,
    document_id: UUID,
) -> SourceArtifact:
    """授权管理用：只按组织查找，管理授权不等于可阅读内容。"""
    artifact = await session.scalar(
        select(SourceArtifact)
        .where(
            SourceArtifact.id == document_id,
            SourceArtifact.organization_id == principal.organization_id,
        )
        .with_for_update()
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return artifact


async def read_content(
    session: AsyncSession,
    storage: LocalFileStorage,
    principal: Principal,
    document_id: UUID,
) -> tuple[SourceArtifact, bytes]:
    # 授权复核用独立短事务；文件处理不放进事务（设计 12.1）
    async with session.begin():
        artifact = await get_visible_document(session, principal, document_id)

    try:
        content = await asyncio.to_thread(storage.open, artifact.object_key)
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="Original file unavailable") from None

    # 原件与登记哈希不符时不交付，避免把被替换的文件当作原件（设计 16.2）
    if hashlib.sha256(content).hexdigest() != artifact.sha256:
        raise HTTPException(status_code=503, detail="Original file failed integrity check")

    # 下载写审计（FR-11）；审计写入失败则不交付文件
    async with session.begin():
        session.add(
            AuditEvent(
                organization_id=principal.organization_id,
                actor_id=principal.user_id,
                action="document.downloaded",
                resource_id=artifact.id,
                payload={"document_id": str(artifact.id)},
            )
        )
    return artifact, content


async def set_access_scope(
    session: AsyncSession,
    principal: Principal,
    document_id: UUID,
    access_scope: str,
) -> SourceArtifact:
    async with session.begin():
        artifact = await get_org_document(session, principal, document_id)
        if artifact.sensitivity == "confidential" and access_scope != "restricted":
            raise HTTPException(
                status_code=422,
                detail="Confidential documents must be restricted",
            )
        before = artifact.access_scope
        artifact.access_scope = access_scope
        record_event(
            session,
            principal,
            artifact.id,
            "document.access_scope_changed",
            {"document_id": str(artifact.id), "before": before, "after": access_scope},
        )
        await session.flush()
        await session.refresh(artifact)
    return artifact


async def list_grants(
    session: AsyncSession,
    principal: Principal,
    document_id: UUID,
) -> list[AccessGrant]:
    async with session.begin():
        artifact = await get_org_document(session, principal, document_id)
        return await grants.list_grants(session, "document", artifact.id)


async def grant(
    session: AsyncSession,
    principal: Principal,
    document_id: UUID,
    user_id: UUID,
) -> AccessGrant:
    async with session.begin():
        artifact = await get_org_document(session, principal, document_id)
        return await grants.grant(
            session,
            principal,
            "document",
            artifact.id,
            user_id,
            "document.access_granted",
            {"document_id": str(artifact.id), "user_id": str(user_id)},
        )


async def revoke(
    session: AsyncSession,
    principal: Principal,
    document_id: UUID,
    user_id: UUID,
) -> None:
    async with session.begin():
        artifact = await get_org_document(session, principal, document_id)
        await grants.revoke(
            session,
            principal,
            "document",
            artifact.id,
            user_id,
            "document.access_revoked",
            {"document_id": str(artifact.id), "user_id": str(user_id)},
        )
