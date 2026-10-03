"""任务处理器（设计 §7、§12.2）。

每个 ``job_type`` 对应一个处理器，签名 ``(session, storage, job) -> progress | None``。
处理器失败时抛异常，由 ``classify`` 映射为任务错误码与是否可重试。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage import LocalFileStorage
from app.core.security import Principal
from app.models import Job, SourceArtifact, User, UserRole
from app.modules.parsing.service import parse_artifact

PARSE_JOB = "document.parse"


@dataclass(frozen=True)
class JobFailure:
    error_code: str
    retryable: bool
    message: str


# 解析服务的错误契约（HTTP 状态码）→ 任务错误码与是否可重试（设计 §12.2、FR-13）
_FAILURE_BY_STATUS: dict[int, tuple[str, bool]] = {
    404: ("document_missing", False),
    413: ("document_too_large", False),
    422: ("parse_rejected", False),
    503: ("document_unavailable", True),
    # 内存超预算：资源释放后重试可能成功，但绝不伪装成业务结果（FR-13）
    507: ("resource_exhausted", True),
}


def classify(error: Exception) -> JobFailure:
    """把异常映射为任务错误码。未知异常按可重试处理，由重试次数兜底。"""
    if isinstance(error, HTTPException):
        error_code, retryable = _FAILURE_BY_STATUS.get(error.status_code, ("internal_error", True))
        return JobFailure(error_code, retryable, str(error.detail))
    return JobFailure("internal_error", True, f"{type(error).__name__}: {error}")


async def _principal_for_document(session: AsyncSession, document_id: UUID) -> Principal:
    """解析在后台执行，操作者记为**导入该原件的人**——当前唯一的解析触发者。

    ``ParseRevision.created_by`` 与审计都需要真实用户；``jobs`` 不记录创建者，故取自原件。
    将来若出现管理员手动重解析等其他触发方式，应改为在任务上记录请求者。
    """
    async with session.begin():
        artifact = await session.scalar(
            select(SourceArtifact).where(SourceArtifact.id == document_id)
        )
        if artifact is None:
            raise HTTPException(status_code=404, detail="Document not found")
        user = await session.scalar(select(User).where(User.id == artifact.created_by))
        if user is None or not user.is_active:
            raise HTTPException(status_code=404, detail="Document owner not found")
        roles = frozenset(
            await session.scalars(select(UserRole.role).where(UserRole.user_id == user.id))
        )
        return Principal(organization_id=user.organization_id, user_id=user.id, roles=roles)


async def handle_document_parse(
    session: AsyncSession, storage: LocalFileStorage, job: Job
) -> dict | None:
    """解析原件并落库。重复执行是幂等的：已解析则返回既有解析版本（设计 §7.1）。"""
    document_id = UUID(job.payload["document_id"])
    principal = await _principal_for_document(session, document_id)
    revision = await parse_artifact(session, storage, principal, document_id)
    return {
        "parse_revision_id": str(revision.id),
        "quality_status": revision.quality_status,
    }


Handler = Callable[[AsyncSession, LocalFileStorage, Job], Awaitable[dict | None]]

HANDLERS: dict[str, Handler] = {
    PARSE_JOB: handle_document_parse,
}
