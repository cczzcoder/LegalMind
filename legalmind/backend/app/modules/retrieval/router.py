"""检索（设计 §8.1、§8.2、§8.3、§13）。

当前只实现 §8.3 的**精确字段与过滤**通路；关键词、向量与图谱通路尚未实现（§8.3 把中文关键词
方案定为经测试集选型后再接入，§8.4 的图谱概念验证属 P4）。
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import Principal
from app.modules.authorization.service import DOCUMENT_READ, require_permission
from app.modules.retrieval import service
from app.modules.retrieval.schemas import SearchQuery, SearchResponse

router = APIRouter(tags=["retrieval"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ReaderDep = Annotated[Principal, Depends(require_permission(DOCUMENT_READ))]


@router.post("/search", response_model=SearchResponse)
async def search(data: SearchQuery, session: SessionDep, principal: ReaderDep):
    """按精确字段与过滤条件检索条款版本；默认屏蔽「已公布未生效」（设计 §8.3）。"""
    return await service.search_provisions(session, principal, data)
