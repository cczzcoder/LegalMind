"""单进程 worker（设计 §3.1、§12.2、§14.2）。

按需启动、**单并发**：一次只领取并处理一个任务，处理完再领下一个。解析这类重任务结束即
释放资源，不常驻模型进程（设计 §14.2 的 ``worker_concurrency: 1``）。

- 领取、心跳、完成/失败各自是独立短事务，耗时处理不占用长事务（设计 §12.2）。
- 心跳在后台任务里续租；若租约丢失（崩溃后被回收），完成/失败写入会作废，任务由回收逻辑
  退回重试，处理器幂等即可自愈。
- 单个任务失败不会终止 worker：错误被分类写入任务记录，然后继续领下一个。
"""

import asyncio
import logging
import os
import socket
from collections.abc import Mapping
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.storage import LocalFileStorage
from app.modules.jobs import service as jobs
from app.workers.handlers import HANDLERS, Handler, classify

logger = logging.getLogger("legalmind.worker")


def new_worker_id() -> str:
    """worker 标识：主机名 + 进程号 + 随机后缀，用于租约归属判定。"""
    return f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"


async def _heartbeat_loop(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id,
    worker_id: str,
    lease_seconds: int,
    stop: asyncio.Event,
) -> None:
    """在任务执行期间周期续租；租约丢失即停止（结果会被丢弃）。"""
    interval = max(1.0, lease_seconds / 3)
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        async with session_factory() as session:
            if not await jobs.heartbeat(
                session, job_id=job_id, worker_id=worker_id, lease_seconds=lease_seconds
            ):
                logger.warning("job %s lost its lease; its result will be discarded", job_id)
                return


async def run_once(
    session_factory: async_sessionmaker[AsyncSession],
    storage: LocalFileStorage,
    *,
    worker_id: str,
    lease_seconds: int,
    backoff_base: int,
    backoff_max: int,
    handlers: Mapping[str, Handler] = HANDLERS,
) -> bool:
    """回收过期租约、领取并处理一个任务；返回本次是否处理了任务。"""
    job_types = tuple(handlers)
    async with session_factory() as session:
        reclaimed = await jobs.reclaim_expired(session, job_types=job_types)
        if reclaimed:
            logger.warning("reclaimed %d job(s) with an expired lease", reclaimed)
        job = await jobs.claim_next(
            session,
            worker_id=worker_id,
            job_types=job_types,
            lease_seconds=lease_seconds,
        )
    if job is None:
        return False

    logger.info("claimed job %s (%s), attempt %d", job.id, job.job_type, job.attempt_count)
    stop_heartbeat = asyncio.Event()
    heartbeat = asyncio.create_task(
        _heartbeat_loop(
            session_factory,
            job_id=job.id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            stop=stop_heartbeat,
        )
    )

    try:
        async with session_factory() as session:
            progress = await handlers[job.job_type](session, storage, job)
    except Exception as error:  # noqa: BLE001 — worker 不能因单个任务失败而退出
        # 错误分类后写入任务记录（设计 §12.2）；未知异常按可重试处理，由重试次数兜底
        failure = classify(error)
        logger.warning("job %s failed: %s - %s", job.id, failure.error_code, failure.message)
        async with session_factory() as session:
            status = await jobs.fail(
                session,
                job_id=job.id,
                worker_id=worker_id,
                error_code=failure.error_code,
                error_message=failure.message,
                retryable=failure.retryable,
                backoff_seconds=jobs.backoff_seconds(
                    job.attempt_count, base=backoff_base, maximum=backoff_max
                ),
            )
        if status is None:
            logger.warning("job %s no longer holds its lease; failure not recorded", job.id)
        else:
            logger.info("job %s -> %s (%s)", job.id, status, failure.error_code)
    else:
        async with session_factory() as session:
            completed = await jobs.complete(
                session, job_id=job.id, worker_id=worker_id, progress=progress
            )
        if completed:
            logger.info("job %s -> succeeded", job.id)
        else:
            logger.warning("job %s no longer holds its lease; result discarded", job.id)
    finally:
        stop_heartbeat.set()
        await heartbeat

    return True


async def run_forever(
    session_factory: async_sessionmaker[AsyncSession],
    storage: LocalFileStorage,
    *,
    poll_seconds: float,
    lease_seconds: int,
    backoff_base: int,
    backoff_max: int,
    handlers: Mapping[str, Handler] = HANDLERS,
) -> None:
    """持续领取任务；没有任务时按 ``poll_seconds`` 轮询。Ctrl+C 停止。"""
    worker_id = new_worker_id()
    logger.info("worker %s started; job types: %s", worker_id, ", ".join(sorted(handlers)))
    while True:
        processed = await run_once(
            session_factory,
            storage,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            backoff_base=backoff_base,
            backoff_max=backoff_max,
            handlers=handlers,
        )
        if not processed:
            await asyncio.sleep(poll_seconds)
