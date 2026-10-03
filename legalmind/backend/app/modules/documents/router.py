from datetime import datetime
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage import LocalFileStorage, get_storage
from app.core.config import get_settings
from app.core.database import get_session
from app.core.security import Principal
from app.models import Job, SourceArtifact
from app.modules.authorization.service import (
    DOCUMENT_DOWNLOAD,
    DOCUMENT_GRANT,
    DOCUMENT_READ,
    DOCUMENT_WRITE,
    AuthorizationService,
    require_permission,
)
from app.modules.documents import service
from app.modules.documents.schemas import (
    AccessScope,
    DocumentOut,
    ImportResult,
    JobOut,
    Sensitivity,
    SetAccessScope,
)
from app.modules.wiki.schemas import GrantOut

router = APIRouter(tags=["documents"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
StorageDep = Annotated[LocalFileStorage, Depends(get_storage)]
ReaderDep = Annotated[Principal, Depends(require_permission(DOCUMENT_READ))]
DownloaderDep = Annotated[Principal, Depends(require_permission(DOCUMENT_DOWNLOAD))]
WriterDep = Annotated[Principal, Depends(require_permission(DOCUMENT_WRITE))]
GrantorDep = Annotated[Principal, Depends(require_permission(DOCUMENT_GRANT))]


async def read_body(request: Request) -> bytes:
    # 边读边计数，超过上限立即停止，不把超大请求整个读进内存
    limit = get_settings().max_upload_bytes
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="File too large")

    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail="File too large")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/documents", response_model=ImportResult, status_code=202)
async def import_document(
    request: Request,
    session: SessionDep,
    storage: StorageDep,
    principal: WriterDep,
    source_id: UUID,
    filename: Annotated[str, Query(min_length=1, max_length=255)],
    sensitivity: Sensitivity,
    access_scope: AccessScope = "organization",
    acquired_at: datetime | None = None,
):
    """请求体为文件原始字节（application/octet-stream）。返回 202：解析任务已登记，尚未执行。"""
    if acquired_at is not None and acquired_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="acquired_at must include a timezone")

    artifact, job = await service.import_document(
        session,
        storage,
        principal,
        source_id=source_id,
        filename=filename,
        content=await read_body(request),
        sensitivity=sensitivity,
        access_scope=access_scope,
        acquired_at=acquired_at,
    )
    return ImportResult(document=DocumentOut.model_validate(artifact), job_id=job.id)


@router.get("/documents", response_model=list[DocumentOut])
async def list_documents(
    session: SessionDep,
    principal: ReaderDep,
    source_id: UUID | None = None,
    before: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    statement = select(SourceArtifact).where(AuthorizationService.document_scope(principal))
    if source_id is not None:
        statement = statement.where(SourceArtifact.source_id == source_id)
    if before is not None:
        statement = statement.where(SourceArtifact.id < before)

    result = await session.scalars(statement.order_by(SourceArtifact.id.desc()).limit(limit))
    return list(result)


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(document_id: UUID, session: SessionDep, principal: ReaderDep):
    return await service.get_visible_document(session, principal, document_id)


@router.get("/documents/{document_id}/content")
async def download(
    document_id: UUID,
    session: SessionDep,
    storage: StorageDep,
    principal: DownloaderDep,
):
    artifact, content = await service.read_content(session, storage, principal, document_id)
    return Response(
        content,
        media_type=artifact.media_type,
        headers={
            # 始终作为附件下载，不在本站内渲染上传的 HTML
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(artifact.original_filename)}"
            ),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.put("/documents/{document_id}/access", response_model=DocumentOut)
async def set_access_scope(
    document_id: UUID,
    data: SetAccessScope,
    session: SessionDep,
    principal: GrantorDep,
):
    return await service.set_access_scope(session, principal, document_id, data.access_scope)


@router.get("/documents/{document_id}/grants", response_model=list[GrantOut])
async def list_grants(document_id: UUID, session: SessionDep, principal: GrantorDep):
    return await service.list_grants(session, principal, document_id)


@router.put("/documents/{document_id}/grants/{user_id}", response_model=GrantOut)
async def grant(document_id: UUID, user_id: UUID, session: SessionDep, principal: GrantorDep):
    return await service.grant(session, principal, document_id, user_id)


@router.delete("/documents/{document_id}/grants/{user_id}", status_code=204)
async def revoke(document_id: UUID, user_id: UUID, session: SessionDep, principal: GrantorDep):
    await service.revoke(session, principal, document_id, user_id)


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: UUID, session: SessionDep, principal: ReaderDep):
    job = await session.scalar(
        select(Job).where(Job.id == job_id, Job.organization_id == principal.organization_id)
    )
    # 任务可见性跟随其资料：看不到资料就看不到任务，避免借任务探测受限资料
    if job is None or job.job_type != service.PARSE_JOB:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        await service.get_visible_document(session, principal, UUID(job.payload["document_id"]))
    except HTTPException:
        raise HTTPException(status_code=404, detail="Job not found") from None
    return job
