"""§9.1 的澄清分支（`app/modules/answering/clarify.py`）。

**触发条件是确定性的**（复用改写层剥掉疑问框架后的剩余实词），所以这里主要钉**判据表**；
澄清问题本身由模型生成，钉的是「模型坏了要如实退回模板」，而不是「模型问得好不好」。
最后两条是**闭环**：澄清 → 补充 → 同一个运行跑到 `ANSWERED`（§9.1 的 `CLARIFYING → RETRIEVING`）。
"""

import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.adapters.generation import GenerationUnavailable
from app.core.security import Principal
from app.models import AnswerRun, Job
from app.modules.answering import clarify, service
from app.modules.answering import runs as answer_runs
from app.modules.retrieval.schemas import CitationOut, ProvisionHit, SearchResponse

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------- 触发判据（纯逻辑）


@pytest.mark.parametrize(
    "question",
    [
        "怎么办",  # 剥离后是空的
        "怎么处理",
        "这个算不算？",  # 只剩代词
        "工资怎么办？",  # 只剩「工资」——没说工资的什么
        "   ",
    ],
)
def test_vague_questions_need_clarification(question):
    assert clarify.needs_clarification(question) is not None


@pytest.mark.parametrize(
    "question",
    [
        "拖欠工资",
        "用人单位无故不缴纳社会保险费，会被怎么处理？",
        "我能不能领低保？",
        "劳动法第五十条说什么？",
        "量子计算专利怎么申请？",
        "监狱提请减刑、假释建议，要经过哪些程序？",
    ],
)
def test_questions_with_substance_go_straight_to_retrieval(question):
    assert clarify.needs_clarification(question) is None


def test_reason_says_what_was_left():
    reason = clarify.needs_clarification("怎么办")
    assert "剥离疑问框架后是空的" in reason
    assert "实质内容不足" in clarify.needs_clarification("工资怎么办？")


def test_substance_keeps_content_words_only():
    assert clarify.substance("工资怎么办？") == "工资"
    assert clarify.substance("这个算不算？") == ""
    assert clarify.substance("拖欠工资") == "拖欠工资"


def test_known_gap_is_documented_in_the_trigger():
    """**已知漏报**：「我想问一下那个事情」剥完还剩内容词，绕过了判据。

    这是规则不是语义防线（模块 docstring 写明了）。钉住它是为了**让这个缺口可见**——
    改 `_FRAMES` 或空泛词表时，这条会提醒你回来重新看。
    """
    assert clarify.needs_clarification("我想问一下那个事情") is None


# ---------------------------------------------------------------- 澄清问题生成


def test_parses_a_clean_clarification():
    parsed = clarify.parse_clarification(
        json.dumps({"question": "你说的工资问题是指哪方面？", "options": ["支付方式", "被拖欠"]})
    )
    assert parsed is not None
    assert parsed.question == "你说的工资问题是指哪方面？"
    assert parsed.options == ("支付方式", "被拖欠")
    assert parsed.asked_by == "model"


def test_unusable_output_falls_back_to_the_template():
    assert clarify.parse_clarification("我理解你的意思是……") is None
    assert clarify.parse_clarification('{"options": ["甲"]}') is None  # 没有问题正文
    assert clarify.parse_clarification("") is None


def test_model_failure_falls_back_to_the_template():
    """模型不可用时**如实退回模板问法**——澄清不是结论，模板问不算「编内容」。"""

    def broken(_messages, schema=None):
        raise OSError("ollama is down")

    asked = clarify.clarifying_question("怎么办", broken)
    assert asked.asked_by == "template"
    assert asked.question == clarify.TEMPLATE.question


def test_generation_unavailable_also_falls_back_to_the_template():
    """⚠️ V1.36 起 `generate()` 把传输失败统一包装成 `GenerationUnavailable`（探测得到 ≠ 用得了）。

    它**不是** `OSError` / `URLError` 的子类，所以那条 except 必须显式认它——少了这一句，
    模型一挂澄清就从「如实退回模板」变成一次 500。
    """

    def broken(_messages, schema=None):
        raise GenerationUnavailable("本地生成模型调用失败：llama-server binary not found")

    asked = clarify.clarifying_question("怎么办", broken)
    assert asked.asked_by == "template"
    assert asked.question == clarify.TEMPLATE.question


def test_garbage_output_falls_back_to_the_template():
    asked = clarify.clarifying_question("怎么办", lambda *_a, **_k: "呃，你想问什么？")
    assert asked.asked_by == "template"


def test_template_renders_without_options():
    assert clarify.TEMPLATE.render() == clarify.TEMPLATE.question
    parsed = clarify.Clarification(question="问什么？", options=("甲", "乙"), asked_by="model")
    assert parsed.render().splitlines() == ["问什么？", "", "1. 甲", "2. 乙"]


# ---------------------------------------------------------------- 闭环（澄清 → 补充 → 完成）

EVIDENCE_TEXT = "第五十条　工资应当以货币形式按月支付给劳动者本人。"


class _FakeCache:
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


async def _drain_jobs(session, run_id) -> None:
    """把该运行已有的任务标成成功——**模拟 worker 已经把它消费掉了**。

    真实的澄清流程里，运行能走到 `CLARIFYING` 就说明初次任务已经跑完；测试若跳过这一步，
    「同一运行只允许一份补充在飞」的检查会把初次任务当成在飞任务，直接挡下补充。
    """
    for job in await session.scalars(
        select(Job).where(Job.payload["run_id"].astext == str(run_id))
    ):
        job.status = "succeeded"


async def test_vague_question_records_a_clarifying_run(monkeypatch, session_factory, make_user):
    """笼统问题 → `CLARIFYING`、不检索、不发布；正文是**反问**而不是结论。"""
    called = False

    async def explode(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("笼统问题不该去检索")

    monkeypatch.setattr(service, "search_provisions", explode)
    monkeypatch.setattr(
        service.generation,
        "generate",
        lambda *_a, **_k: json.dumps(
            {"question": "你说的工资问题是指哪方面？", "options": ["支付方式", "被拖欠"]}
        ),
    )
    principal = await _principal(make_user)

    async with session_factory() as session:
        answer = await service.answer_question(session, principal, "工资怎么办？")
        run = await session.scalar(
            select(AnswerRun).where(AnswerRun.organization_id == principal.organization_id)
        )

    assert called is False
    assert answer.blocked_by == "clarifying"
    assert answer.published is False
    assert answer.path == "clarify"
    assert answer.citations == ()
    assert "是指哪方面" in answer.answer
    assert answer.run_id == run.id
    assert run.state == "CLARIFYING"
    assert run.published is False
    # **不是待审**：它在等用户补充，不是等人判读（§9.3 第三层的队列只收 NEEDS_REVIEW）
    assert run.review_required is False


async def test_continuation_merges_the_supplement_and_finishes(
    monkeypatch, session_factory, make_user, semantics_pass
):
    """**闭环**：澄清 → 补充 → 同一个运行走到 `ANSWERED`（§9.1 的 `CLARIFYING → RETRIEVING`）。"""
    monkeypatch.setattr(service.generation, "available", lambda *_a, **_k: True)

    # ⚠️ 签名要与 `generation.generate(name, messages, *, schema=…)` 对齐——第一个位置参数是模型名
    def generate(_name, _messages, *, schema=None, **_kwargs):
        if schema is clarify.CLARIFY_SCHEMA:
            return json.dumps({"question": "你说的工资问题是指哪方面？", "options": []})
        return json.dumps(
            {"claims": [{"text": "工资应当以货币形式按月支付", "evidence_ids": ["1"]}]},
            ensure_ascii=False,
        )

    monkeypatch.setattr(service.generation, "generate", generate)
    monkeypatch.setattr(
        service,
        "search_provisions",
        _pinned_search([_hit("中华人民共和国劳动法", "50", EVIDENCE_TEXT)]),
    )
    principal = await _principal(make_user)
    cache = _FakeCache()

    # 1) 笼统问题 → 澄清（**同步路径也要传缓存**，否则原问题没进缓存、后面没法续跑）
    async with session_factory() as session:
        first = await service.answer_question(session, principal, "工资怎么办？", cache=cache)
        run_id = first.run_id
    assert first.blocked_by == "clarifying"

    # 2) 补充 → 续跑（同一个运行）；初次任务已跑完，先排空它
    async with session_factory() as session, session.begin():
        await _drain_jobs(session, run_id)
        run, job = await answer_runs.submit_clarification(
            session, principal, run_id, "用人单位拖欠工资该怎么办", cache=cache
        )
    assert run.state == "CLARIFYING", "提交补充后运行仍停在 CLARIFYING，由 worker 推进"
    merged = await answer_runs.read_question(cache, run_id)
    assert "工资怎么办？" in merged and "用人单位拖欠工资" in merged

    # 3) worker 跑：**这一步会用到转移表里的 CLARIFYING → RETRIEVING**
    from app.workers import handlers

    monkeypatch.setattr(handlers, "build_answer_cache", lambda: cache)
    monkeypatch.setattr(handlers, "SessionFactory", session_factory)
    async with session_factory() as session:
        await handlers.handle_answer_question(session, None, job)

    async with session_factory() as session:
        saved = await session.get(AnswerRun, run_id)
    assert saved.state == "ANSWERED"
    assert saved.published is True
    assert saved.evidence and saved.evidence[0]["cited"] is True
    assert "工资应当以货币形式按月支付" in (await answer_runs.read_answer(cache, run_id))


async def test_continuation_rejects_a_run_that_is_not_waiting(session_factory, make_user):
    principal = await _principal(make_user)
    cache = _FakeCache()
    async with session_factory() as session:
        async with session.begin():
            run, _ = await answer_runs.submit_question(
                session, principal, "工资怎么办？", cache=cache
            )
        await _drain_jobs(session, run.id)
        with pytest.raises(ValueError, match="不在待澄清状态"):
            await answer_runs.submit_clarification(session, principal, run.id, "补充", cache=cache)


async def test_continuation_rejects_a_second_submission_while_one_is_in_flight(
    session_factory, make_user
):
    """**任务表才是在飞的事实来源**：状态还停在 CLARIFYING，所以状态检查挡不住连点两次。"""
    principal = await _principal(make_user)
    cache = _FakeCache()
    async with session_factory() as session:
        async with session.begin():
            run, _ = await answer_runs.submit_question(
                session, principal, "工资怎么办？", cache=cache
            )
            run.state = "CLARIFYING"
            await _drain_jobs(session, run.id)
            await answer_runs.submit_clarification(
                session, principal, run.id, "第一次", cache=cache
            )
        with pytest.raises(ValueError, match="还在处理中"):
            await answer_runs.submit_clarification(
                session, principal, run.id, "第二次", cache=cache
            )
        jobs = list(
            await session.scalars(select(Job).where(Job.payload["run_id"].astext == str(run.id)))
        )
    assert len(jobs) == 2  # 初次提交 + 一次补充，第二次被挡下


async def test_continuation_requires_the_original_question(session_factory, make_user):
    principal = await _principal(make_user)
    cache = _FakeCache()
    async with session_factory() as session:
        async with session.begin():
            run, _ = await answer_runs.submit_question(
                session, principal, "工资怎么办？", cache=cache
            )
            run.state = "CLARIFYING"
            await _drain_jobs(session, run.id)
            await cache.delete(answer_runs.question_key(run.id))
        with pytest.raises(LookupError, match="过期"):
            await answer_runs.submit_clarification(session, principal, run.id, "补充", cache=cache)
