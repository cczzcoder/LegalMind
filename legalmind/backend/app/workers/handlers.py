"""任务处理器（设计 §7、§12.2）。

每个 ``job_type`` 对应一个处理器，签名 ``(session, storage, job) -> progress | None``。
处理器失败时抛异常，由 ``classify`` 映射为任务错误码与是否可重试。
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import cache as answer_cache
from app.adapters.storage import LocalFileStorage
from app.core.config import get_settings
from app.core.database import SessionFactory
from app.core.security import Principal
from app.models import (
    ANSWER_RUN_TERMINAL_STATES,
    AnswerRun,
    Job,
    SourceArtifact,
    User,
    UserRole,
)
from app.modules.answering import runs as answer_runs
from app.modules.answering import service as answering_service
from app.modules.parsing.service import parse_artifact
from app.modules.redaction.service import redact

logger = logging.getLogger(__name__)

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


def build_answer_cache():
    """构造短期内容缓存。**抽成函数是为了让测试能替换掉它**（真 Redis 不该进单元测试）。"""
    return answer_cache.from_settings(get_settings())


async def _principal_for_run(session: AsyncSession, run: AnswerRun) -> Principal:
    """按运行记录里的请求者重建 Principal（`answer_runs.requested_by`）。

    解析任务当初只能取原件的 `created_by`（`jobs` 不记创建者，见 `_principal_for_document` 的
    注释），**运行记录把请求者记下来了**——所以这里不需要绕道。

    ⚠️ **不开自己的事务**：调用方已经在事务里，再 `begin()` 会抛
    `InvalidRequestError: A transaction is already begun`（实测踩过）。
    """
    user = await session.scalar(select(User).where(User.id == run.requested_by))
    if user is None or not user.is_active:
        raise HTTPException(status_code=404, detail="Requester not found")
    roles = frozenset(
        await session.scalars(select(UserRole.role).where(UserRole.user_id == user.id))
    )
    return Principal(organization_id=user.organization_id, user_id=user.id, roles=roles)


async def _update_run_state(run_id: UUID, state: str) -> None:
    """把进度写进运行记录——**用独立会话**，免得和链路的检索事务缠在一起。

    单独开会话意味着**每次转移都提交一次**，所以调用方轮询 run 就能看到进度（§9.4 的
    「核验前发送进度状态」）。**这是有意为之的写放大**：一次问答只有十来个状态。
    """
    async with SessionFactory() as session, session.begin():
        run = await session.get(AnswerRun, run_id)
        if run is not None:
            run.previous_state = run.state
            run.state = state


async def handle_answer_question(
    session: AsyncSession, storage: LocalFileStorage, job: Job
) -> dict | None:
    """跑一次异步问答（设计 §9.4）。

    结果**不进数据库**（§21「客户数据不落库」）：运行记录只更新状态、证据引用与配置快照，
    答案本身**跑过脱敏后写进短期缓存**（TTL 到期即焚），复核人从那里取。
    """
    run_id = UUID(job.payload["run_id"])
    async with session.begin():
        run = await session.get(AnswerRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Answer run not found")
        # ⚠️ **重试要先把状态复位到 CREATED**：转移表里没有「回到起点」的边（`FAILED` 是终态、
        # `GENERATING → RETRIEVING` 也不合法），而任务重试（租约过期、可重试失败）本质上是
        # **同一次运行的下一次尝试**。不复位就会在重试时抛 `IllegalTransition`。
        #
        # ⚠️ **但澄清后的续跑不是重试**：它是第一次尝试（`attempt_count == 1`）、状态停在
        # `CLARIFYING`，链路要走的正是转移表里声明的 `CLARIFYING → RETRIEVING`——**复位反而会
        # 把这条边用掉的机会抹掉**。所以只在「重试」或「停在终态」时复位。
        if job.attempt_count > 1 or run.state in ANSWER_RUN_TERMINAL_STATES:
            run.previous_state = run.state
            run.state = "CREATED"
        principal = await _principal_for_run(session, run)

    cache = build_answer_cache()
    question = await answer_runs.read_question(cache, run_id)
    if question is None:
        # 问题随缓存过期了（TTL 短于任务排队时间）——重试也没用，如实失败
        async with session.begin():
            target = await session.get(AnswerRun, run_id)
            if target is not None:
                target.previous_state, target.state = target.state, "FAILED"
        raise HTTPException(status_code=404, detail="Question expired from the short-term cache")

    async def on_state(state: str) -> None:
        await _update_run_state(run_id, state)

    # ⚠️ **这里刻意不包 try/except**：失败时**不动运行记录**——任务会重试，重试前会把状态复位；
    # 而「永久失败」由 job 表记录（运行记录停在最后一次尝试的状态，`answer-run` 会把任务状态
    # 一并显示出来）。在这里补一个 FAILED 反而会让重试撞上终态（转移表里 FAILED 没有出边）。
    answer = await answering_service.answer_question(
        session,
        principal,
        question,
        limit=job.payload.get("limit", 5),
        max_new_tokens=job.payload.get("max_new_tokens", 512),
        model=job.payload.get("model"),
        record=True,
        on_state=on_state,
        # **更新 submit 时建好的那一行**，不要再建一行（实测踩过：一次运行留两条记录）
        run_row=run,
    )

    # ⚠️ **先脱敏再缓存**：缓存里不得出现未脱敏的客户内容（设计 §21.3）
    redacted_answer, entities = redact(answer.answer)
    stored = await answer_runs.store_answer(cache, run_id, redacted_answer)
    if entities:
        logger.info("answer run %s: redacted %d entities before caching", run_id, len(entities))
    await cache.close()
    return {"run_id": str(run_id), "state": "recorded", "cached": stored}


Handler = Callable[[AsyncSession, LocalFileStorage, Job], Awaitable[dict | None]]

HANDLERS: dict[str, Handler] = {
    PARSE_JOB: handle_document_parse,
    answer_runs.QUESTION_JOB: handle_answer_question,
}
