"""数据库任务表的状态机（设计 §12.2）。

单进程 worker 按 ``job_type`` 领取任务。领取用 ``FOR UPDATE SKIP LOCKED`` **原子**完成，
防止重复领取；耗时处理不占用长事务——领取、心跳、完成/失败各自是独立短事务。

状态流转::

    PENDING → RUNNING → SUCCEEDED
                  ├─ RETRY_WAIT → PENDING
                  ├─ FAILED
                  └─ CANCELLED

取消只作用于**尚未开始**的任务（PENDING / RETRY_WAIT）：不强制终止正在执行的任务，
不把杀进程等同于成功取消（设计 §12.2）。正在执行的任务若失去租约（崩溃、被回收），
完成/失败写入会因租约不匹配而作废，任务由 ``reclaim_expired`` 退回重试。
"""

from collections.abc import Sequence
from datetime import timedelta
from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job

# 租约过期被回收时写入的 error_code，便于运维区分"崩过"与"业务失败"
ERROR_LEASE_EXPIRED = "lease_expired"

# 任务执行中允许领取的状态
_CLAIMABLE = ("pending", "retry_wait")


async def claim_next(
    session: AsyncSession,
    *,
    worker_id: str,
    job_types: Sequence[str],
    lease_seconds: int,
) -> Job | None:
    """原子领取一个到期任务并置为 RUNNING；没有可领任务时返回 None。

    ``FOR UPDATE SKIP LOCKED`` 保证并发 worker 不会领到同一个任务；``attempt_count``
    在领取时自增，因此重试次数按"领取次数"计。已用尽重试次数的任务不再被领取。
    """
    if not job_types:
        return None

    candidate = (
        select(Job.id)
        .where(
            Job.job_type.in_(job_types),
            Job.status.in_(_CLAIMABLE),
            Job.available_at <= func.now(),
            Job.attempt_count < Job.max_attempts,
        )
        .order_by(Job.available_at, Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    async with session.begin():
        job = await session.scalar(
            update(Job)
            .where(Job.id == candidate)
            .values(
                status="running",
                lease_owner=worker_id,
                lease_expires_at=func.now() + timedelta(seconds=lease_seconds),
                heartbeat_at=func.now(),
                attempt_count=Job.attempt_count + 1,
                started_at=func.coalesce(Job.started_at, func.now()),
                error_code=None,
                error_message=None,
            )
            .returning(Job)
        )
    return job


async def heartbeat(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    lease_seconds: int,
) -> bool:
    """续租并刷新心跳；返回 False 表示已不持有该任务（租约被回收或状态已变）。"""
    async with session.begin():
        result = await session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.lease_owner == worker_id,
                Job.status == "running",
            )
            .values(
                heartbeat_at=func.now(),
                lease_expires_at=func.now() + timedelta(seconds=lease_seconds),
            )
        )
    return result.rowcount == 1


async def complete(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    progress: dict | None = None,
) -> bool:
    """置为 SUCCEEDED；返回 False 表示租约已不在本 worker 手里，结果作废。

    业务结果与审计由处理器在自己的事务里写入（设计 §12.1）；这里只推进任务状态。
    若两者之间进程崩溃，任务会因租约过期被回收重试，处理器幂等即可自愈。
    """
    values = {
        "status": "succeeded",
        "finished_at": func.now(),
        "lease_owner": None,
        "lease_expires_at": None,
    }
    # progress 为空时不写入：JSONB 会把 Python None 存成 JSON null 而非 SQL NULL
    if progress is not None:
        values["progress"] = progress

    async with session.begin():
        result = await session.execute(
            update(Job)
            .where(Job.id == job_id, Job.lease_owner == worker_id, Job.status == "running")
            .values(**values)
        )
    return result.rowcount == 1


async def fail(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    error_code: str,
    error_message: str | None,
    retryable: bool,
    backoff_seconds: int,
) -> str | None:
    """记录失败：可重试则置 RETRY_WAIT 并退避，否则置 FAILED（进入人工处理队列）。

    返回写入后的状态；返回 None 表示租约已不在本 worker 手里，不覆盖他人状态。
    """
    async with session.begin():
        job = await session.scalar(
            select(Job)
            .where(Job.id == job_id, Job.lease_owner == worker_id, Job.status == "running")
            .with_for_update()
        )
        if job is None:
            return None

        will_retry = retryable and job.attempt_count < job.max_attempts
        job.status = "retry_wait" if will_retry else "failed"
        job.error_code = error_code
        job.error_message = (error_message or "")[:2000] or None
        job.lease_owner = None
        job.lease_expires_at = None
        if will_retry:
            job.finished_at = None
            job.available_at = func.now() + timedelta(seconds=backoff_seconds)
        else:
            job.finished_at = func.now()
        return job.status


async def reclaim_expired(session: AsyncSession, *, job_types: Sequence[str]) -> int:
    """把租约过期的 RUNNING 任务退回可重试状态（worker 崩溃/被杀的恢复路径）。

    "租约过期的任务经过幂等检查后才重新执行"（设计 §12.2）由处理器保证：解析按
    ``(原件, 解析器, 版本, 配置)`` 幂等，重复执行返回既有版本（设计 §7.1）。
    已用尽重试次数的任务直接置 FAILED，不无限重试。
    """
    if not job_types:
        return 0

    exhausted = Job.attempt_count >= Job.max_attempts
    async with session.begin():
        result = await session.execute(
            update(Job)
            .where(
                Job.job_type.in_(job_types),
                Job.status == "running",
                Job.lease_expires_at.is_not(None),
                Job.lease_expires_at < func.now(),
            )
            .values(
                status=case((exhausted, "failed"), else_="retry_wait"),
                available_at=func.now(),
                finished_at=case((exhausted, func.now()), else_=None),
                lease_owner=None,
                lease_expires_at=None,
                error_code=ERROR_LEASE_EXPIRED,
            )
        )
    return result.rowcount or 0


async def cancel(session: AsyncSession, *, job_id: UUID) -> bool:
    """取消尚未开始的任务；正在执行的任务不会被强制终止（设计 §12.2）。"""
    async with session.begin():
        result = await session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status.in_(_CLAIMABLE))
            .values(status="cancelled", finished_at=func.now())
        )
    return result.rowcount == 1


def backoff_seconds(attempt: int, *, base: int, maximum: int) -> int:
    """指数退避：``base * 2^(attempt-1)``，封顶 ``maximum``。"""
    return min(maximum, base * 2 ** max(0, attempt - 1))
