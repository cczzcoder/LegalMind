from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import Principal
from app.models import WikiPage, WikiRevision
from app.modules.authorization.service import (
    WIKI_GRANT,
    WIKI_READ,
    WIKI_WRITE,
    AuthorizationService,
    require_permission,
)
from app.modules.wiki import service
from app.modules.wiki.schemas import (
    CreatePage,
    CreateRevision,
    GrantOut,
    PageOut,
    RevisionOut,
    SetAccessScope,
)

router = APIRouter(prefix="/wiki", tags=["wiki"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ReaderDep = Annotated[Principal, Depends(require_permission(WIKI_READ))]
WriterDep = Annotated[Principal, Depends(require_permission(WIKI_WRITE))]
GrantorDep = Annotated[Principal, Depends(require_permission(WIKI_GRANT))]


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
