"""worker 端到端（设计 §3.1、§7、§12.2）：需要 TEST_DATABASE_URL。

覆盖：任务被领取并处理 → 解析版本落库且任务 succeeded；确定性失败进入人工处理；
原件不可用可重试；重复投递幂等；没有可领任务时不空转。

**为什么每个用例用独立的 job_type**：测试库是共享且跨运行累积的——其他测试模块每跑一次
都会留下待处理的 ``document.parse`` 任务，本模块自己的用例也会留下 retry_wait 任务。
若 worker 直接领 ``document.parse``，断言就无法确定它领到的是哪个任务。这里让每个用例用
唯一 job_type 建任务，**处理器仍是真实的** ``handle_document_parse``，runner 与错误分类
也都是真实实现；另有一项单测断言真实 job_type 确实注册了处理器。
"""

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.adapters.storage import LocalFileStorage, get_storage
from app.models import Job, ParseRevision, SourceArtifact
from app.workers import runner
from app.workers.handlers import HANDLERS, handle_document_parse
from tests.helpers import build_minimal_pdf, import_document_for_parsing

pytestmark = pytest.mark.anyio


def test_real_job_type_is_registered():
    assert HANDLERS["document.parse"] is handle_document_parse


@pytest.fixture
def storage(tmp_path, make_client):
    # make_client 结束时会清空 dependency_overrides
    from app.main import app

    storage = LocalFileStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: storage
    return storage


def _unique_job_type() -> str:
    """每个用例独立的 job_type，隔离共享测试库里其他运行残留的任务。"""
    return f"test.document.parse.{uuid4().hex[:8]}"


async def _queue_job(session_factory, job_type: str, document_id) -> Job:
    async with session_factory() as session, session.begin():
        job = Job(
            job_type=job_type,
            payload={"document_id": str(document_id)},
            idempotency_key=f"{job_type}-{uuid4()}",
            status="pending",
            attempt_count=0,
            max_attempts=3,
        )
        session.add(job)
        await session.flush()
        return job


async def _run_once(session_factory, storage, job_type: str) -> bool:
    return await runner.run_once(
        session_factory,
        storage,
        worker_id="test-worker",
        lease_seconds=60,
        backoff_base=30,
        backoff_max=900,
        handlers={job_type: handle_document_parse},
    )


async def _drain(session_factory, storage, job_type: str) -> int:
    processed = 0
    while await _run_once(session_factory, storage, job_type):
        processed += 1
        if processed > 10:
            pytest.fail("worker did not drain the queue")
    return processed


async def _job(session_factory, job_id) -> Job:
    async with session_factory() as session:
        return await session.scalar(select(Job).where(Job.id == job_id))


async def _revision_count(session_factory, document_id) -> int:
    async with session_factory() as session:
        return await session.scalar(
            select(func.count())
            .select_from(ParseRevision)
            .where(ParseRevision.artifact_id == document_id)
        )


async def test_worker_parses_the_document_and_completes_the_job(
    make_client, make_user, storage, session_factory
):
    job_type = _unique_job_type()
    content = build_minimal_pdf([["Hello Legal Mind"]], marker=uuid4().hex)
    _, document_id = await import_document_for_parsing(make_client, make_user, content, "law.pdf")
    job = await _queue_job(session_factory, job_type, document_id)

    assert await _drain(session_factory, storage, job_type) == 1

    stored = await _job(session_factory, job.id)
    assert stored.status == "succeeded"
    assert stored.error_code is None
    assert stored.attempt_count == 1
    assert stored.finished_at is not None
    assert stored.lease_owner is None
    assert stored.progress["quality_status"] == "ok"
    assert stored.progress["parse_revision_id"]
    assert await _revision_count(session_factory, document_id) == 1


async def test_import_registers_a_real_parse_job(make_client, make_user, storage, session_factory):
    """导入接口登记的是真实的 document.parse 任务，worker 的处理器就挂在它上面。

    必须请求 ``storage`` fixture：否则上传走真实的 ``get_storage()``，把原件写进仓库的
    ``data/artifacts/``（污染开发环境）。
    """
    content = build_minimal_pdf([["Hello"]], marker=uuid4().hex)
    _, document_id = await import_document_for_parsing(make_client, make_user, content, "real.pdf")

    async with session_factory() as session:
        job = await session.scalar(
            select(Job).where(
                Job.payload["document_id"].astext == str(document_id),
                Job.job_type == "document.parse",
            )
        )
    assert job is not None
    assert job.status in ("pending", "retry_wait", "running", "succeeded", "failed")


async def test_worker_records_a_deterministic_failure(
    make_client, make_user, storage, session_factory
):
    job_type = _unique_job_type()
    # 通过导入校验（无主动内容特征）但无法真正解析：确定性失败，重试无用
    broken = f"%PDF-1.7\nbroken {uuid4().hex}".encode()
    _, document_id = await import_document_for_parsing(make_client, make_user, broken, "broken.pdf")
    job = await _queue_job(session_factory, job_type, document_id)

    await _drain(session_factory, storage, job_type)

    stored = await _job(session_factory, job.id)
    assert stored.status == "failed"
    assert stored.error_code == "parse_rejected"
    assert stored.finished_at is not None
    assert stored.lease_owner is None


async def test_worker_retries_when_the_original_is_unavailable(
    make_client, make_user, storage, session_factory
):
    job_type = _unique_job_type()
    content = build_minimal_pdf([["Hello"]], marker=uuid4().hex)
    _, document_id = await import_document_for_parsing(make_client, make_user, content, "gone.pdf")
    job = await _queue_job(session_factory, job_type, document_id)

    async with session_factory() as session:
        key = await session.scalar(
            select(SourceArtifact.object_key).where(SourceArtifact.id == document_id)
        )
    storage._path(key).unlink()

    await _drain(session_factory, storage, job_type)

    stored = await _job(session_factory, job.id)
    # 原件可能恢复，属可重试失败：退回 RETRY_WAIT 并退避
    assert stored.status == "retry_wait"
    assert stored.error_code == "document_unavailable"
    assert stored.attempt_count == 1
    assert stored.finished_at is None
    assert stored.lease_owner is None


async def test_reprocessing_the_same_job_is_idempotent(
    make_client, make_user, storage, session_factory
):
    job_type = _unique_job_type()
    content = build_minimal_pdf([["Hello Legal Mind"]], marker=uuid4().hex)
    _, document_id = await import_document_for_parsing(make_client, make_user, content, "dup.pdf")
    job = await _queue_job(session_factory, job_type, document_id)

    await _drain(session_factory, storage, job_type)

    # 模拟重复投递：把任务退回 pending（设计 §7.1 的幂等要求）
    async with session_factory() as session, session.begin():
        row = await session.scalar(select(Job).where(Job.id == job.id))
        row.status = "pending"
        row.attempt_count = 0
        row.available_at = func.now()

    await _drain(session_factory, storage, job_type)

    stored = await _job(session_factory, job.id)
    assert stored.status == "succeeded"
    assert await _revision_count(session_factory, document_id) == 1, (
        "重复处理不得产生第二个解析版本"
    )


async def test_worker_reports_no_work_when_nothing_is_claimable(session_factory, storage):
    # 该 job_type 从未建过任务，因此必然领不到
    assert await _run_once(session_factory, storage, _unique_job_type()) is False
