from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import Principal, get_principal
from app.models import WikiPage, WikiRevision
from app.modules.wiki import service
from app.modules.wiki.schemas import (
    CreatePage,
    CreateRevision,
    PageOut,
    RevisionOut,
)

router = APIRouter(prefix="/wiki", tags=["wiki"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]


@router.get("/pages", response_model=list[PageOut])
async def list_pages(
    session: SessionDep,
    principal: PrincipalDep,
    before: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    statement = select(WikiPage).where(
        WikiPage.organization_id == principal.organization_id
    )

    if before is not None:
        statement = statement.where(WikiPage.id < before)

    result = await session.scalars(
        statement.order_by(WikiPage.id.desc()).limit(limit)
    )
    return list(result)


@router.post("/pages", response_model=RevisionOut, status_code=201)
async def create_page(
    data: CreatePage,
    session: SessionDep,
    principal: PrincipalDep,
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
    principal: PrincipalDep,
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
    principal: PrincipalDep,
    before: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    page = await session.scalar(
        select(WikiPage).where(
            WikiPage.id == page_id,
            WikiPage.organization_id == principal.organization_id,
        )
    )

    if page is None:
        raise HTTPException(status_code=404, detail="Page not found")

    statement = select(WikiRevision).where(
        WikiRevision.page_id == page_id
    )

    if before is not None:
        statement = statement.where(WikiRevision.number < before)

    result = await session.scalars(
        statement.order_by(WikiRevision.number.desc()).limit(limit)
    )
    return list(result)
