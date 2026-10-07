"""异步 answer-run 与短期内容缓存（设计 §9.4、§9.3 第三层、§21）。

**不连 Redis、不加载模型**：缓存用内存替身，链路用钉住的检索与生成替身。
真 Redis 的行为由 `app/adapters/cache.py` 的接口约束（`put`/`get`/`delete`/`available`）。

这里钉的是三件事：**不落库**（问题与回答都不进数据库）、**缓存前必脱敏**、
**一次运行只有一行记录**（实测踩过：异步路径曾同时留下「只有状态的」和「只有结果的」两行）。
"""

import json
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.adapters.cache import AnswerCache
from app.core.security import Principal
from app.models import AnswerRun, Job
from app.modules.answering import runs as answer_runs
from app.modules.answering import service
from app.modules.retrieval.schemas import CitationOut, ProvisionHit, SearchResponse
from app.workers import handlers

pytestmark = pytest.mark.anyio

#: 一个真实的身份证号格式——脱敏正则能命中（末位校验位不校验）
PII = "110101199003072316"


class _FakeCache:
    """内存版短期缓存。**键与 TTL 的语义与 Redis 版一致**，只是不过期。"""

    def __init__(self):
        self.store: dict[str, str] = {}

    @property
    def available(self) -> bool:
        return True

    async def put(self, key: str, text: str) -> bool:
        self.store[key] = text
        return True

    async def get(self, key: str):
        return self.store.get(key)

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)

    async def close(self) -> None:
        return None


def _hit(law: str, number: str, text: str) -> ProvisionHit:
    return ProvisionHit(
        instrument_title=law,
        instrument_type="law",
        jurisdiction="CN",
        issuing_body="测试机关",
        document_number=None,
        version_label="2026年",
        legal_status="effective",
        review_status="approved",
        promulgated_on=None,
        effective_from=None,
        effective_to=None,
        provision_type="article",
        provision_number=number,
        provision_display=f"第{number}条",
        text=text,
        text_sha256="0" * 64,
        citation=CitationOut(
            instrument_id=uuid4(),
            legal_version_id=uuid4(),
            provision_version_id=uuid4(),
            provision_identity_id=uuid4(),
            artifact_id=uuid4(),
            chunk_id=uuid4(),
        ),
    )


def _pinned_search(hits):
    async def search(_session, _principal, _query):
        return SearchResponse(hits=list(hits), truncated=False, path="keyword")

    return search


async def _principal(make_user) -> Principal:
    user = await make_user("reader")
    return Principal(
        organization_id=user.organization_id, user_id=user.id, roles=frozenset({"reader"})
    )


async def test_submit_refuses_without_the_short_term_cache(session_factory, make_user):
    """异步靠缓存交付结果——没配缓存就**直接拒绝**，而不是让它跑完再发现取不到。"""
    principal = await _principal(make_user)
    async with session_factory() as session:
        with pytest.raises(answer_runs.CacheUnavailable):
            await answer_runs.submit_question(
                session, principal, "工资怎么发？", cache=AnswerCache()
            )


async def test_submit_creates_a_created_run_and_a_job_without_the_question(
    session_factory, make_user
):
    """**问题不进数据库**：`jobs.payload` 是 DB 里的 JSONB，放进去就违反 §21「客户数据不落库」。"""
    principal = await _principal(make_user)
    cache = _FakeCache()
    async with session_factory() as session:
        run, job = await answer_runs.submit_question(
            session, principal, f"我身份证是{PII}，能不能领低保？", cache=cache
        )
        await session.commit()

    assert run.state == "CREATED"
    assert job.job_type == answer_runs.QUESTION_JOB
    payload = json.dumps(job.payload, ensure_ascii=False)
    assert "能不能领低保" not in payload
    assert PII not in payload
    assert job.payload["run_id"] == str(run.id)


async def test_submitted_question_is_redacted_before_caching(session_factory, make_user):
    """问题必须经缓存传给 worker，所以**先脱敏再进缓存**。"""
    principal = await _principal(make_user)
    cache = _FakeCache()
    async with session_factory() as session:
        run, _ = await answer_runs.submit_question(
            session, principal, f"我身份证是{PII}，能不能领低保？", cache=cache
        )
        await session.commit()

    cached = await answer_runs.read_question(cache, run.id)
    assert cached is not None
    assert PII not in cached
    assert "[身份证_1]" in cached
    # 哈希取的是**脱敏后**文本——短问题的原文哈希可被穷举反推
    assert PII not in run.question_sha256


async def test_worker_updates_the_same_run_and_caches_the_answer(
    monkeypatch, session_factory, make_user
):
    """**一次运行只有一行记录**：worker 更新 submit 时建好的那一行，不再建新行。"""
    principal = await _principal(make_user)
    cache = _FakeCache()
    monkeypatch.setattr(handlers, "build_answer_cache", lambda: cache)
    monkeypatch.setattr(handlers, "SessionFactory", session_factory)
    monkeypatch.setattr(service.generation, "available", lambda *_a, **_k: True)
    monkeypatch.setattr(
        service.generation,
        "generate",
        lambda *_a, **_k: json.dumps(
            {"claims": [{"text": "工资应当以货币形式按月支付", "evidence_ids": ["1"]}]},
            ensure_ascii=False,
        ),
    )
    monkeypatch.setattr(
        service,
        "search_provisions",
        _pinned_search(
            [_hit("中华人民共和国劳动法", "50", "第五十条　工资应当以货币形式按月支付。")]
        ),
    )

    async with session_factory() as session:
        run, job = await answer_runs.submit_question(
            session, principal, "工资怎么发？", cache=cache
        )
        await session.commit()

    async with session_factory() as session:
        await handlers.handle_answer_question(session, None, job)

    async with session_factory() as session:
        # ⚠️ **按组织过滤**：测试库跨运行累积，直接 count 整张表会数到别的用例留下的行
        rows = list(
            await session.scalars(
                select(AnswerRun)
                .where(AnswerRun.organization_id == principal.organization_id)
                .order_by(AnswerRun.created_at)
            )
        )

    assert len(rows) == 1, "一次运行只应有一行记录"
    saved = rows[0]
    assert saved.id == run.id
    assert saved.state == "ANSWERED"
    assert saved.published is True
    assert saved.evidence and saved.evidence[0]["cited"] is True
    assert saved.config["instructions_sha256"]
    cached_answer = await answer_runs.read_answer(cache, run.id)
    assert cached_answer and "工资应当以货币形式按月支付" in cached_answer


async def test_cached_answer_is_redacted(monkeypatch, session_factory, make_user):
    """**缓存前必脱敏**（设计 §21.3）——缓存里不得出现未脱敏的客户内容。"""
    principal = await _principal(make_user)
    cache = _FakeCache()
    monkeypatch.setattr(handlers, "build_answer_cache", lambda: cache)
    monkeypatch.setattr(handlers, "SessionFactory", session_factory)
    monkeypatch.setattr(service.generation, "available", lambda *_a, **_k: True)
    monkeypatch.setattr(
        service.generation,
        "generate",
        lambda *_a, **_k: json.dumps(
            {"claims": [{"text": f"劳动者{PII}的工资应当按月支付", "evidence_ids": ["1"]}]},
            ensure_ascii=False,
        ),
    )
    monkeypatch.setattr(
        service,
        "search_provisions",
        _pinned_search(
            [_hit("中华人民共和国劳动法", "50", "第五十条　工资应当以货币形式按月支付。")]
        ),
    )

    async with session_factory() as session:
        run, job = await answer_runs.submit_question(
            session, principal, "工资怎么发？", cache=cache
        )
        await session.commit()

    async with session_factory() as session:
        await handlers.handle_answer_question(session, None, job)

    cached_answer = await answer_runs.read_answer(cache, run.id)
    assert cached_answer is not None
    assert PII not in cached_answer
    assert "[身份证_1]" in cached_answer


async def test_retry_resets_the_run_before_rerunning(monkeypatch, session_factory, make_user):
    """任务重试是**同一次运行的下一次尝试**——不复位就会撞上转移表（`GENERATING → RETRIEVING` 不合法）。"""
    principal = await _principal(make_user)
    cache = _FakeCache()
    monkeypatch.setattr(handlers, "build_answer_cache", lambda: cache)
    monkeypatch.setattr(handlers, "SessionFactory", session_factory)
    monkeypatch.setattr(service.generation, "available", lambda *_a, **_k: True)
    monkeypatch.setattr(
        service.generation,
        "generate",
        lambda *_a, **_k: json.dumps({"claims": [{"text": "结论", "evidence_ids": ["1"]}]}),
    )
    monkeypatch.setattr(
        service,
        "search_provisions",
        _pinned_search(
            [_hit("中华人民共和国劳动法", "50", "第五十条　工资应当以货币形式按月支付。")]
        ),
    )

    async with session_factory() as session:
        run, job = await answer_runs.submit_question(
            session, principal, "工资怎么发？", cache=cache
        )
        # 模拟上一次尝试死在半路
        run.state = "GENERATING"
        run.previous_state = "ASSEMBLING_EVIDENCE"
        await session.commit()

    async with session_factory() as session:
        await handlers.handle_answer_question(session, None, job)

    async with session_factory() as session:
        saved = await session.get(AnswerRun, run.id)
    assert saved.state == "ANSWERED"


async def test_expired_question_fails_the_run_without_retrying(
    monkeypatch, session_factory, make_user
):
    """问题随缓存过期了（TTL 短于排队时间）——重试也没用，如实失败。"""
    principal = await _principal(make_user)
    cache = _FakeCache()
    monkeypatch.setattr(handlers, "build_answer_cache", lambda: cache)
    monkeypatch.setattr(handlers, "SessionFactory", session_factory)

    async with session_factory() as session:
        run, job = await answer_runs.submit_question(
            session, principal, "工资怎么发？", cache=cache
        )
        await session.commit()

    await cache.delete(answer_runs.question_key(run.id))

    async with session_factory() as session:
        with pytest.raises(HTTPException):
            await handlers.handle_answer_question(session, None, job)

    async with session_factory() as session:
        saved = await session.get(AnswerRun, run.id)
    assert saved.state == "FAILED"


async def test_read_answer_is_none_when_the_cache_is_unavailable(session_factory, make_user):
    """取不到就返回 None——由调用方**如实显示「内容不可用」**，不拿别的东西冒充。"""
    principal = await _principal(make_user)
    async with session_factory() as session:
        run, _ = await answer_runs.submit_question(
            session, principal, "工资怎么发？", cache=_FakeCache()
        )
        await session.commit()

    assert await answer_runs.read_answer(AnswerCache(), run.id) is None


async def test_job_type_is_registered():
    """任务类型要在 worker 的处理器表里，否则任务永远领不走。"""
    assert answer_runs.QUESTION_JOB in handlers.HANDLERS
    assert handlers.HANDLERS[answer_runs.QUESTION_JOB] is handlers.handle_answer_question
    assert Job.__tablename__ == "jobs"
