from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.models import (
    AuditEvent,
    OutboxEvent,
    WikiPage,
    WikiRevision,
)
from app.modules.wiki.schemas import CreatePage, CreateRevision


def record_change(
    session: AsyncSession,
    principal: Principal,
    page_id: UUID,
    revision: WikiRevision,
    action: str,
) -> None:
    payload = {
        "page_id": str(page_id),
        "revision_id": str(revision.id),
        "revision_number": revision.number,
    }

    session.add(
        AuditEvent(
            organization_id=principal.organization_id,
            actor_id=principal.user_id,
            action=action,
            resource_id=page_id,
            payload=payload,
        )
    )

    session.add(
        OutboxEvent(
            organization_id=principal.organization_id,
            event_type=action,
            payload=payload,
        )
    )


async def create_page(
    session: AsyncSession,
    principal: Principal,
    data: CreatePage,
) -> WikiRevision:
    async with session.begin():
        page = WikiPage(
            organization_id=principal.organization_id,
            title=data.title,
            head_revision=1,
        )
        session.add(page)
        await session.flush()

        revision = WikiRevision(
            page_id=page.id,
            number=1,
            body=data.body,
            author_id=principal.user_id,
        )
        session.add(revision)
        await session.flush()

        record_change(
            session,
            principal,
            page.id,
            revision,
            "wiki.page.created",
        )

        await session.flush()
        await session.refresh(revision)

    return revision


async def create_revision(
    session: AsyncSession,
    principal: Principal,
    page_id: UUID,
    data: CreateRevision,
) -> WikiRevision:
    async with session.begin():
        page = await session.scalar(
            select(WikiPage)
            .where(
                WikiPage.id == page_id,
                WikiPage.organization_id == principal.organization_id,
            )
            .with_for_update()
        )

        if page is None:
            raise HTTPException(
                status_code=404,
                detail="Page not found",
            )

        if page.head_revision != data.expected_revision:
            raise HTTPException(
                status_code=409,
                detail="Revision conflict: reload before editing",
            )

        page.head_revision += 1

        revision = WikiRevision(
            page_id=page.id,
            number=page.head_revision,
            body=data.body,
            author_id=principal.user_id,
        )
        session.add(revision)
        await session.flush()

        record_change(
            session,
            principal,
            page.id,
            revision,
            "wiki.revision.created",
        )

        await session.flush()
        await session.refresh(revision)

    return revision
