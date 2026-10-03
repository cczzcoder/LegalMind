from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import Principal
from app.models import Source
from app.modules.authorization.service import DOCUMENT_READ, SOURCE_MANAGE, require_permission
from app.modules.sources import service
from app.modules.sources.schemas import CreateSource, SourceOut

router = APIRouter(prefix="/sources", tags=["sources"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("", response_model=list[SourceOut])
async def list_sources(
    session: SessionDep,
    principal: Annotated[Principal, Depends(require_permission(DOCUMENT_READ))],
):
    # 来源属公共法律数据，全局共享（设计 21.2）
    result = await session.scalars(select(Source).order_by(Source.name))
    return list(result)


@router.post("", response_model=SourceOut, status_code=201)
async def create_source(
    data: CreateSource,
    session: SessionDep,
    principal: Annotated[Principal, Depends(require_permission(SOURCE_MANAGE))],
):
    return await service.create_source(session, principal, data)
