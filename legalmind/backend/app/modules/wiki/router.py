from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import Principal
from app.models import WikiPage, WikiRevision
from app.modules.authorization.service import (
    REVIEW_DECIDE,
    WIKI_GRANT,
    WIKI_READ,
    WIKI_WRITE,
    AuthorizationService,
    require_permission,
)
from app.modules.wiki import service
from app.modules.wiki.schemas import (
    CitationOut,
    CreatePage,
    CreateRevision,
    GrantOut,
    PageOut,
    PendingRevisionOut,
    ReviewDecision,
    RevisionOut,
    SetAccessScope,
    SetCitations,
)

router = APIRouter(prefix="/wiki", tags=["wiki"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ReaderDep = Annotated[Principal, Depends(require_permission(WIKI_READ))]
WriterDep = Annotated[Principal, Depends(require_permission(WIKI_WRITE))]
GrantorDep = Annotated[Principal, Depends(require_permission(WIKI_GRANT))]
ReviewerDep = Annotated[Principal, Depends(require_permission(REVIEW_DECIDE))]


@router.get("/pages", response_model=list[PageOut])
async def list_pages(
    session: SessionDep,
    principal: ReaderDep,
    before: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    # 范围条件在数据库中执行，未授权的受限页面不会进入结果
    statement = select(WikiPage).where(AuthorizationService.wiki_page_scope(principal))

    if before is not None:
        statement = statement.where(WikiPage.id < before)

    result = await session.scalars(statement.order_by(WikiPage.id.desc()).limit(limit))
    return list(result)


@router.get("/pages/stale", response_model=list[PageOut])
async def list_stale_pages(
    session: SessionDep,
    principal: ReviewerDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    """待复核的已发布页面（设计 §10.2「来源更新先标记待复核」）。

    标记由 ``python -m app.cli flag-stale-pages`` 计算并写入；这里只读。
    """
    return list(
        await session.scalars(
            select(WikiPage)
            .where(
                AuthorizationService.wiki_page_scope(principal),
                WikiPage.review_due_at.is_not(None),
            )
            .order_by(WikiPage.review_due_at.desc(), WikiPage.id)
            .limit(limit)
        )
    )


@router.post("/pages", response_model=RevisionOut, status_code=201)
async def create_page(
    data: CreatePage,
    session: SessionDep,
    principal: WriterDep,
):
    return await service.create_page(session, principal, data)


@router.post(
    "/pages/{page_id}/revisions",
    response_model=RevisionOut,
    status_code=201,
)
async def create_revision(
    page_id: UUID,
    data: CreateRevision,
    session: SessionDep,
    principal: WriterDep,
):
    return await service.create_revision(
        session,
        principal,
        page_id,
        data,
    )


@router.get(
    "/pages/{page_id}/revisions",
    response_model=list[RevisionOut],
)
async def list_revisions(
    page_id: UUID,
    session: SessionDep,
    principal: ReaderDep,
    before: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    page = await service.get_visible_page(session, principal, page_id)

    statement = select(WikiRevision).where(WikiRevision.page_id == page.id)

    if before is not None:
        statement = statement.where(WikiRevision.number < before)

    result = await session.scalars(statement.order_by(WikiRevision.number.desc()).limit(limit))
    return list(result)


@router.put("/pages/{page_id}/access", response_model=PageOut)
async def set_access_scope(
    page_id: UUID,
    data: SetAccessScope,
    session: SessionDep,
    principal: GrantorDep,
):
    return await service.set_access_scope(session, principal, page_id, data.access_scope)


@router.get("/pages/{page_id}/grants", response_model=list[GrantOut])
async def list_grants(page_id: UUID, session: SessionDep, principal: GrantorDep):
    return await service.list_grants(session, principal, page_id)


@router.put("/pages/{page_id}/grants/{user_id}", response_model=GrantOut)
async def grant(
    page_id: UUID,
    user_id: UUID,
    session: SessionDep,
    principal: GrantorDep,
):
    return await service.grant(session, principal, page_id, user_id)


@router.delete("/pages/{page_id}/grants/{user_id}", status_code=204)
async def revoke(
    page_id: UUID,
    user_id: UUID,
    session: SessionDep,
    principal: GrantorDep,
):
    await service.revoke(session, principal, page_id, user_id)


@router.put(
    "/pages/{page_id}/revisions/{revision_number}/citations",
    response_model=list[CitationOut],
)
async def set_citations(
    page_id: UUID,
    revision_number: int,
    data: SetCitations,
    session: SessionDep,
    principal: WriterDep,
):
    """覆盖式登记修订引用的条款版本（设计 §5.3、§10.2）。"""
    await service.set_citations(
        session, principal, page_id, revision_number, data.provision_version_ids
    )
    return await service.list_citations(session, principal, page_id, revision_number)


@router.get(
    "/pages/{page_id}/revisions/{revision_number}/citations",
    response_model=list[CitationOut],
)
async def list_citations(
    page_id: UUID,
    revision_number: int,
    session: SessionDep,
    principal: ReaderDep,
):
    return await service.list_citations(session, principal, page_id, revision_number)


@router.post("/pages/{page_id}/revisions/{revision_number}/submit", response_model=RevisionOut)
async def submit_revision(
    page_id: UUID,
    revision_number: int,
    session: SessionDep,
    principal: WriterDep,
):
    """提交审核；提交后该修订锁定，改正文要新开修订（设计 §10.2）。"""
    return await service.submit_revision(session, principal, page_id, revision_number)


@router.post("/pages/{page_id}/revisions/{revision_number}/publish", response_model=RevisionOut)
async def publish_revision(
    page_id: UUID,
    revision_number: int,
    data: ReviewDecision,
    session: SessionDep,
    principal: ReviewerDep,
):
    """审核通过并发布。需 ``review.decide``；**作者不能审自己的修订**；引用检查不通过不得发布。"""
    return await service.publish_revision(session, principal, page_id, revision_number, data.note)


@router.post("/pages/{page_id}/revisions/{revision_number}/reject", response_model=RevisionOut)
async def reject_revision(
    page_id: UUID,
    revision_number: int,
    data: ReviewDecision,
    session: SessionDep,
    principal: ReviewerDep,
):
    """驳回。作者可以改引用后重新提交，不必新开修订。"""
    return await service.reject_revision(session, principal, page_id, revision_number, data.note)


@router.get("/revisions/pending", response_model=list[PendingRevisionOut])
async def list_pending(session: SessionDep, principal: ReviewerDep):
    """待审队列——此前只能靠 SQL 查。

    ⚠️ 返回里带 **`page_title`**：这是跨页面的清单，只给 `page_id` 的话审核人得拿 UUID 去别处对。
    """
    rows = await service.list_pending(session, principal)
    return [
        PendingRevisionOut(**RevisionOut.model_validate(revision).model_dump(), page_title=title)
        for revision, title in rows
    ]


@router.get("/pages/{page_id}/published", response_model=RevisionOut)
async def get_published_revision(page_id: UUID, session: SessionDep, principal: ReaderDep):
    """读者看到的是发布指针指向的修订，不是最新修订（设计 §10.2）。"""
    return await service.get_published_revision(session, principal, page_id)
