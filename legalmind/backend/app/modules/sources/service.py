"""来源登记（FR-01）。"""

from datetime import datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.models import Source
from app.modules.authorization.grants import record_event
from app.modules.sources.schemas import CreateSource, UpdateSource


async def create_source(
    session: AsyncSession,
    principal: Principal,
    data: CreateSource,
) -> Source:
    try:
        async with session.begin():
            source = Source(
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


async def update_source(
    session: AsyncSession,
    principal: Principal,
    source_id: UUID,
    data: UpdateSource,
) -> Source:
    """更正来源登记信息（FR-01、设计 §20.3）。

    授权说明是「未登记授权说明的来源不得入库」的判据，因此**不允许清空**：显式传 ``null`` 直接
    422，避免把来源变成没有入库依据的状态。§20.3 要求「来源更新、撤回或内容变化须留痕」，故每次
    变更写 ``source.updated`` 审计，**记录变更前后的字段值**（只记字段名看不出原来写的是什么）。
    """
    changes = data.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No fields to update")
    if "license_note" in changes and changes["license_note"] is None:
        raise HTTPException(status_code=422, detail="license_note cannot be cleared")

    try:
        async with session.begin():
            source = await session.scalar(
                select(Source).where(Source.id == source_id).with_for_update()
            )
            if source is None:
                raise HTTPException(status_code=404, detail="Source not found")
            before = {field: _jsonable(getattr(source, field)) for field in changes}
            for field, value in changes.items():
                setattr(source, field, value)
            record_event(
                session,
                principal,
                source.id,
                "source.updated",
                {
                    "source_id": str(source.id),
                    "changed_fields": sorted(changes),
                    "before": before,
                    "after": {field: _jsonable(value) for field, value in changes.items()},
                },
            )
            await session.flush()
            await session.refresh(source)
    except IntegrityError:
        raise HTTPException(status_code=409, detail="Source name already exists") from None
    return source


def _jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


async def get_source(session: AsyncSession, source_id: UUID) -> Source:
    """来源属公共法律数据，全局共享（设计 21.2），不再按组织过滤。"""
    source = await session.scalar(select(Source).where(Source.id == source_id))
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return source
