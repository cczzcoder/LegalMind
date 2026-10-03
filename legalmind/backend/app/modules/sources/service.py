"""来源登记（FR-01）。"""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.models import Source
from app.modules.authorization.grants import record_event
from app.modules.sources.schemas import CreateSource


async def create_source(
    session: AsyncSession,
    principal: Principal,
    data: CreateSource,
) -> Source:
    try:
        async with session.begin():
            source = Source(
                organization_id=principal.organization_id,
                created_by=principal.user_id,
                **data.model_dump(),
            )
            session.add(source)
            await session.flush()
            record_event(
                session,
                principal,
                source.id,
                "source.created",
                {"source_id": str(source.id), "name": source.name},
            )
            await session.flush()
            await session.refresh(source)
    except IntegrityError:
        raise HTTPException(status_code=409, detail="Source name already exists") from None
    return source


async def get_org_source(session: AsyncSession, principal: Principal, source_id: UUID) -> Source:
    source = await session.scalar(
        select(Source).where(
            Source.id == source_id,
            Source.organization_id == principal.organization_id,
        )
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return source
