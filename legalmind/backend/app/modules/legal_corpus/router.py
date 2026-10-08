"""法律版本的人工复核接口（需求第 3 节「法律审核员：审核资料版本」；设计 §7、§13）。

**为什么要有它**：复核原先只有 CLI（V1.42）。CLI 够用，但**审核是一个角色动作**——法律审核员
不该为了审一条版本去连服务器敲命令。这里把 `legal_corpus/review.py` 的服务函数暴露成两个端点，
形状与 `answering/router.py` 的待审队列 / 复核保持一致。

⚠️ **只改审核状态，绝不动效力状态**——理由见 `review.py` 的模块注释。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import Principal
from app.models import LegalInstrument, LegalVersion, SourceArtifact
from app.modules.authorization.service import (
    DOCUMENT_READ,
    REVIEW_DECIDE,
    AuthorizationService,
    require_permission,
)
from app.modules.legal_corpus import review as version_review
from app.modules.legal_corpus.schemas import LegalVersionOut, ReviewVersionRequest

router = APIRouter(tags=["legal-corpus"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ReaderDep = Annotated[Principal, Depends(require_permission(DOCUMENT_READ))]
ReviewerDep = Annotated[Principal, Depends(require_permission(REVIEW_DECIDE))]


def _query():
    """版本 + 法律名称 + 主原件文件名。

    原件用**外连接**：允许先登记版本、后导入文件（`artifact_id` 可空）；用内连接会把这类版本
    整条漏掉——而「还没挂原件的版本」恰恰是最需要人工看一眼的。
    """
    return (
        select(LegalVersion, LegalInstrument.title, SourceArtifact.original_filename)
        .join(LegalInstrument, LegalInstrument.id == LegalVersion.instrument_id)
        .outerjoin(SourceArtifact, SourceArtifact.id == LegalVersion.artifact_id)
    )


def _to_out(version: LegalVersion, instrument_title: str, artifact_filename: str | None):
    return LegalVersionOut(
        id=version.id,
        instrument_title=instrument_title,
        version_label=version.version_label,
        legal_status=version.legal_status,
        review_status=version.review_status,
        promulgated_on=version.promulgated_on,
        effective_from=version.effective_from,
        artifact_filename=artifact_filename,
        created_at=version.created_at,
    )


@router.get("/legal-versions", response_model=list[LegalVersionOut])
async def list_legal_versions(
    session: SessionDep,
    principal: ReaderDep,
    pending: bool = Query(default=False, description="只看待人工复核的（待审队列）"),
    limit: int = Query(default=50, ge=1, le=200),
):
    """列出法律版本；`pending=true` 即**待审队列**（需求第 3 节）。

    ⚠️ **两种模式的权限不同**（与 `GET /answers` 同一条口径）：全量列表只要 `document.read`——
    法律版本是**公共法律数据**（§21.2），不是谁的私有记录；**待审队列额外要 `review.decide`**，
    因为「哪些数据还没被人确认」本身就是审核面的信息。

    ⚠️ **授权照旧在数据库里复核**（§11.2、§8.3），与检索侧同一口径：`restricted` 原件没有显式
    授权就不该因为「列个版本清单」而暴露出来。**没挂原件的版本照样可见**——没有来源就无从限制，
    而且「还没挂原件」恰恰是最需要人工看一眼的情形（见 `_query`）。
    """
    if pending and not AuthorizationService.can(principal, REVIEW_DECIDE):
        raise HTTPException(status_code=403, detail="Permission denied")

    query = (
        _query()
        .where(
            or_(
                LegalVersion.artifact_id.is_(None),
                AuthorizationService.document_scope(principal),
            )
        )
        .order_by(LegalVersion.created_at.desc())
        .limit(limit)
    )
    if pending:
        query = query.where(LegalVersion.review_status == "pending")

    rows = await session.execute(query)
    return [_to_out(*row) for row in rows.all()]


@router.post("/legal-versions/{version_id}/review", response_model=LegalVersionOut)
async def review_legal_version(
    version_id: UUID,
    data: ReviewVersionRequest,
    session: SessionDep,
    principal: ReviewerDep,
):
    """人工复核一个法律版本（需求第 3 节）；需 `review.decide`，写审计。

    ⚠️ **只改 `review_status`，绝不动 `legal_status`**——后者是法律事实，不是审核意见。
    ⚠️ **允许改已复核的版本**（与 `POST /answers/{id}/review` 刻意不同）：问答运行是**历史记录**，
    复核一次就定了；而 `review_status` 是**当前状态**，审错了必须能纠正——否则只是把
    「只能改 SQL」换个地方再来一遍。每次改动都写审计 `legal_version.reviewed`。
    """
    async with session.begin():
        try:
            await version_review.review_version(
                session, principal, version_id, decision=data.decision, note=data.note
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            # 已经是该状态 → 409（与 `POST /answers/{id}/review` 的重复复核同码）
            raise HTTPException(status_code=409, detail=str(error)) from error
        # 回传展示字段（法律名称、原件名）——复核完页面上还要接着看
        row = (await session.execute(_query().where(LegalVersion.id == version_id))).one()

    return _to_out(*row)
