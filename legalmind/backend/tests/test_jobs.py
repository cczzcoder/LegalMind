"""任务表状态机（设计 §12.2）。

退避计算与错误映射是纯单元测试；领取/心跳/完成/失败/回收/取消需要 TEST_DATABASE_URL。
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models import Job
from app.modules.jobs import service as jobs
from app.workers.handlers import classify


def test_backoff_is_exponential_and_capped():
    assert [jobs.backoff_seconds(i, base=30, maximum=900) for i in range(1, 7)] == [
        30,
        60,
        120,
        240,
        480,
        900,
    ]
    # 非法/首次尝试都退化为基准值，不会算出 0 或负数
    assert jobs.backoff_seconds(0, base=30, maximum=900) == 30


def test_classify_maps_the_parse_error_contract():
    cases = {
        404: ("document_missing", False),
        413: ("document_too_large", False),
        422: ("parse_rejected", False),
        503: ("document_unavailable", True),
        507: ("resource_exhausted", True),
    }
    for status_code, (error_code, retryable) in cases.items():
        failure = classify(HTTPException(status_code=status_code, detail="boom"))
        assert (failure.error_code, failure.retryable) == (error_code, retryable)
        assert failure.message == "boom"


def test_classify_treats_unknown_errors_as_retryable():
    failure = classify(RuntimeError("unexpected"))
    assert failure.error_code == "internal_error"
    assert failure.retryable is True
    assert "RuntimeError" in failure.message


def _job_type(prefix: str) -> str:
    """每次用唯一 job_type，避免共享测试库里历次运行残留的同类型任务干扰断言。"""
    return f"{prefix}.{uuid4().hex[:8]}"


async def _make_job(session_factory, **overrides) -> Job:
    async with session_factory() as session, session.begin():
        job = Job(
            job_type=overrides.pop("job_type", "test.task"),
            payload=overrides.pop("payload", {}),
            idempotency_key=f"key-{uuid4()}",
            status=overrides.pop("status", "pending"),
            attempt_count=overrides.pop("attempt_count", 0),
            max_attempts=overrides.pop("max_attempts", 3),
            **overrides,
        )
        session.add(job)
        await session.flush()
        return job


async def _reload(session_factory, job_id) -> Job:
    async with session_factory() as session:
        return await session.scalar(select(Job).where(Job.id == job_id))


@pytest.mark.anyio
async def test_claim_marks_running_and_counts_the_attempt(migrated_test_database, session_factory):
    job_type = _job_type("claim")
    job = await _make_job(session_factory, job_type=job_type)

    async with session_factory() as session:
        claimed = await jobs.claim_next(
            session, worker_id="w1", job_types=[job_type], lease_seconds=60
        )

    assert claimed is not None and claimed.id == job.id
    stored = await _reload(session_factory, job.id)
    assert stored.status == "running"
    assert stored.lease_owner == "w1"
    assert stored.attempt_count == 1
    assert stored.started_at is not None
    assert stored.lease_expires_at > datetime.now(UTC)


@pytest.mark.anyio
async def test_claim_skips_jobs_that_are_not_due_or_exhausted(
    migrated_test_database, session_factory
):
    job_type = _job_type("skip")
    future = await _make_job(
        session_factory,
        job_type=job_type,
        available_at=datetime.now(UTC) + timedelta(hours=1),
    )
    exhausted = await _make_job(session_factory, job_type=job_type, attempt_count=3, max_attempts=3)

    async with session_factory() as session:
        claimed = await jobs.claim_next(
            session, worker_id="w1", job_types=[job_type], lease_seconds=60
        )

    assert claimed is None
    assert (await _reload(session_factory, future.id)).status == "pending"
    assert (await _reload(session_factory, exhausted.id)).status == "pending"


@pytest.mark.anyio
async def test_two_workers_never_claim_the_same_job(migrated_test_database, session_factory):
    job_type = _job_type("race")
    job = await _make_job(session_factory, job_type=job_type)

    async def claim(worker_id: str):
        async with session_factory() as session:
            return await jobs.claim_next(
                session, worker_id=worker_id, job_types=[job_type], lease_seconds=60
            )

    first, second = await asyncio.gather(claim("w1"), claim("w2"))

    winners = [result for result in (first, second) if result is not None]
    assert len(winners) == 1 and winners[0].id == job.id


@pytest.mark.anyio
async def test_heartbeat_renews_only_for_the_lease_holder(migrated_test_database, session_factory):
    job = await _make_job(
        session_factory, job_type=_job_type("hb"), status="running", lease_owner="w1"
    )

    async with session_factory() as session:
        assert (
            await jobs.heartbeat(session, job_id=job.id, worker_id="w1", lease_seconds=60) is True
        )
        assert (
            await jobs.heartbeat(session, job_id=job.id, worker_id="w2", lease_seconds=60) is False
        )

    stored = await _reload(session_factory, job.id)
    assert stored.heartbeat_at is not None
    assert stored.lease_owner == "w1"


@pytest.mark.anyio
async def test_complete_and_fail_require_the_lease(migrated_test_database, session_factory):
    job = await _make_job(
        session_factory, job_type=_job_type("done"), status="running", lease_owner="w1"
    )

    async with session_factory() as session:
        assert (
            await jobs.complete(session, job_id=job.id, worker_id="w2", progress={"a": 1}) is False
        )
    assert (await _reload(session_factory, job.id)).status == "running"

    async with session_factory() as session:
        assert (
            await jobs.complete(session, job_id=job.id, worker_id="w1", progress={"a": 1}) is True
        )
    stored = await _reload(session_factory, job.id)
    assert stored.status == "succeeded"
    assert stored.progress == {"a": 1}
    assert stored.finished_at is not None
    assert stored.lease_owner is None


@pytest.mark.anyio
async def test_fail_retries_then_gives_up(migrated_test_database, session_factory):
    job_type = _job_type("fail")
    retryable = await _make_job(
        session_factory, job_type=job_type, status="running", lease_owner="w1", attempt_count=1
    )

    async with session_factory() as session:
        status = await jobs.fail(
            session,
            job_id=retryable.id,
            worker_id="w1",
            error_code="document_unavailable",
            error_message="x",
            retryable=True,
            backoff_seconds=60,
        )
    assert status == "retry_wait"
    stored = await _reload(session_factory, retryable.id)
    assert stored.available_at > datetime.now(UTC)
    assert stored.finished_at is None
    assert stored.lease_owner is None

    # 最后一次尝试（attempt_count 已达 max_attempts）不再是"可重试"，直接进入人工处理
    last = await _make_job(
        session_factory,
        job_type=job_type,
        status="running",
        lease_owner="w1",
        attempt_count=3,
        max_attempts=3,
    )
    async with session_factory() as session:
        status = await jobs.fail(
            session,
            job_id=last.id,
            worker_id="w1",
            error_code="parse_rejected",
            error_message="broken",
            retryable=True,
            backoff_seconds=60,
        )
    assert status == "failed"
    stored = await _reload(session_factory, last.id)
    assert stored.finished_at is not None
    assert stored.error_code == "parse_rejected"


@pytest.mark.anyio
async def test_fail_is_discarded_when_the_lease_is_gone(migrated_test_database, session_factory):
    job = await _make_job(
        session_factory, job_type=_job_type("lost"), status="running", lease_owner="w1"
    )

    async with session_factory() as session:
        status = await jobs.fail(
            session,
            job_id=job.id,
            worker_id="w2",
            error_code="internal_error",
            error_message="x",
            retryable=True,
            backoff_seconds=60,
        )

    assert status is None
    assert (await _reload(session_factory, job.id)).status == "running"


@pytest.mark.anyio
async def test_reclaim_expired_returns_jobs_to_retry(migrated_test_database, session_factory):
    job_type = _job_type("stale")
    stale = await _make_job(
        session_factory,
        job_type=job_type,
        status="running",
        lease_owner="dead",
        lease_expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    exhausted = await _make_job(
        session_factory,
        job_type=job_type,
        status="running",
        lease_owner="dead",
        lease_expires_at=datetime.now(UTC) - timedelta(minutes=5),
        attempt_count=3,
        max_attempts=3,
    )
    fresh = await _make_job(
        session_factory,
        job_type=job_type,
        status="running",
        lease_owner="alive",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    async with session_factory() as session:
        reclaimed = await jobs.reclaim_expired(session, job_types=[job_type])

    assert reclaimed == 2
    stored = await _reload(session_factory, stale.id)
    assert stored.status == "retry_wait"
    assert stored.error_code == jobs.ERROR_LEASE_EXPIRED
    assert stored.lease_owner is None
    # 用尽重试次数的任务直接失败，不无限重试
    assert (await _reload(session_factory, exhausted.id)).status == "failed"
    # 租约未过期的任务不受影响
    assert (await _reload(session_factory, fresh.id)).status == "running"


@pytest.mark.anyio
async def test_cancel_only_affects_unstarted_jobs(migrated_test_database, session_factory):
    job_type = _job_type("cancel")
    pending = await _make_job(session_factory, job_type=job_type)
    running = await _make_job(
        session_factory, job_type=job_type, status="running", lease_owner="w1"
    )

    async with session_factory() as session:
        assert await jobs.cancel(session, job_id=pending.id) is True
        # 正在执行的任务不会被强制终止（设计 §12.2）
        assert await jobs.cancel(session, job_id=running.id) is False

    assert (await _reload(session_factory, pending.id)).status == "cancelled"
    assert (await _reload(session_factory, running.id)).status == "running"
