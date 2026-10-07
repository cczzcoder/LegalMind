"""§9.1 的执行状态机与运行记录（`app/modules/answering/runs.py`）。

两类测试：**状态机本身是纯逻辑**（转移表合法、非法转移会炸），**运行记录要落库**（状态、证据引用、
配置快照、待审队列与复核）。

⚠️ 其中一条是**隐私保证**：运行记录里**不得出现条文正文**——§9.4 要留证据与配置快照，而 §21 要求
「客户个性化问答采用临时检索，**客户数据不落库**」，证据只存条款版本 ID 与效力状态。
"""

import json
from uuid import uuid4

import pytest

from app.core.security import Principal
from app.models import (
    ANSWER_RUN_STATES,
    ANSWER_RUN_TERMINAL_STATES,
    ANSWER_RUN_TRANSITIONS,
    AnswerRun,
)
from app.modules.answering import runs, service
from app.modules.retrieval.schemas import CitationOut, ProvisionHit, SearchResponse

pytestmark = pytest.mark.anyio

# ---------------------------------------------------------------- 状态机（纯逻辑）


def test_illegal_transition_is_rejected_loudly():
    """§9.1「不使用框架的隐式重试绕过门禁」——走到不该走的状态要当场炸，不能记假轨迹。"""
    tracker = runs.RunTracker()
    tracker.to("RETRIEVING")
    with pytest.raises(runs.IllegalTransition):
        tracker.to("ANSWERED")  # 检索完不可能直接就是「已作答」


def test_tracker_records_previous_state():
    tracker = runs.RunTracker()
    tracker.to("RETRIEVING")
    tracker.to("ASSEMBLING_EVIDENCE")
    assert tracker.state == "ASSEMBLING_EVIDENCE"
    assert tracker.previous == "RETRIEVING"
    assert tracker.trail == ["CREATED", "RETRIEVING"]
    assert tracker.terminal is False


def test_every_transition_target_is_a_known_state():
    """转移表里的目标必须是已声明的状态——防手滑写错一个词。"""
    for source, targets in ANSWER_RUN_TRANSITIONS.items():
        assert source in ANSWER_RUN_STATES, source
        for target in targets:
            assert target in ANSWER_RUN_STATES, f"{source} → {target}"


def test_terminal_states_are_terminal():
    for state in ANSWER_RUN_TERMINAL_STATES:
        assert ANSWER_RUN_TRANSITIONS.get(state, ()) == (), f"{state} 是终态，不该有出边"
    for state in ANSWER_RUN_STATES:
        if state not in ANSWER_RUN_TERMINAL_STATES:
            assert ANSWER_RUN_TRANSITIONS.get(state), f"{state} 不是终态，必须有出边"


def test_every_state_is_reachable_from_created():
    """没有孤岛状态——否则表里那些状态只是装饰。"""
    seen = {"CREATED"}
    frontier = ["CREATED"]
    while frontier:
        for target in ANSWER_RUN_TRANSITIONS.get(frontier.pop(), ()):
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    assert seen == set(ANSWER_RUN_STATES)


# ---------------------------------------------------------------- 落库

EVIDENCE_TEXT = "第五十条　工资应当以货币形式按月支付给劳动者本人。"


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


async def _principal_for(make_user) -> Principal:
    user = await make_user("reader")
    return Principal(
        organization_id=user.organization_id, user_id=user.id, roles=frozenset({"reader"})
    )


async def _latest_run(session) -> AnswerRun:
    from sqlalchemy import select

    return await session.scalar(select(AnswerRun).order_by(AnswerRun.created_at.desc()).limit(1))


async def test_successful_answer_records_an_answered_run(monkeypatch, session_factory, make_user):
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
        _pinned_search([_hit("中华人民共和国劳动法", "50", EVIDENCE_TEXT)]),
    )
    principal = await _principal_for(make_user)

    async with session_factory() as session:
        answer = await service.answer_question(session, principal, "工资怎么发？")
        run = await _latest_run(session)

    assert answer.published is True
    assert run is not None
    assert run.state == "ANSWERED"
    assert run.published is True
    assert run.blocked_by is None
    assert run.review_required is False
    assert run.previous_state == "VERIFYING"
    # 配置快照要能还原「当时是怎么跑的」
    assert run.config["model"]
    assert run.config["limit"] == 5
    assert len(run.config["instructions_sha256"]) == 64
    assert run.evidence[0]["cited"] is True
    assert run.evidence[0]["legal_status"] == "effective"


async def test_run_record_never_contains_the_provision_text(
    monkeypatch, session_factory, make_user
):
    """**隐私保证**：§21「客户数据不落库」——证据只存条款版本 ID 与效力状态，不存条文正文。"""
    monkeypatch.setattr(service.generation, "available", lambda *_a, **_k: True)
    monkeypatch.setattr(
        service.generation,
        "generate",
        lambda *_a, **_k: json.dumps({"claims": [{"text": "结论", "evidence_ids": ["1"]}]}),
    )
    monkeypatch.setattr(
        service,
        "search_provisions",
        _pinned_search([_hit("中华人民共和国劳动法", "50", EVIDENCE_TEXT)]),
    )
    principal = await _principal_for(make_user)

    async with session_factory() as session:
        await service.answer_question(session, principal, "工资怎么发？")
        run = await _latest_run(session)
        payload = json.dumps({"evidence": run.evidence, "config": run.config}, ensure_ascii=False)

    assert EVIDENCE_TEXT not in payload
    assert "工资应当以货币形式按月支付" not in payload
    # 正文只留哈希
    assert len(run.question_sha256) == 64
    assert len(run.answer_sha256) == 64


async def test_blocked_answer_records_needs_review(monkeypatch, session_factory, make_user):
    """被门禁拦下 → `NEEDS_REVIEW` 且进待审队列（§9.3 第三层）。"""
    monkeypatch.setattr(service, "search_provisions", _pinned_search([]))
    principal = await _principal_for(make_user)

    async with session_factory() as session:
        await service.answer_question(session, principal, "量子计算专利怎么申请？")
        run = await _latest_run(session)

    assert run.state == "INSUFFICIENT_EVIDENCE"
    assert run.review_required is False  # 没命中不是「待审」，是「没料」
    assert run.published is False


async def test_personal_question_records_needs_review_and_enters_the_queue(
    monkeypatch, session_factory, make_user
):
    monkeypatch.setattr(
        service,
        "search_provisions",
        _pinned_search([_hit("中华人民共和国社会救助法", "16", "第十六条　…")]),
    )
    principal = await _principal_for(make_user)

    async with session_factory() as session:
        await service.answer_question(session, principal, "我是名特困人员，我能否领到社会救助？")
        run = await _latest_run(session)
        queue = await runs.pending_reviews(session, principal)

    assert run.state == "NEEDS_REVIEW"
    assert run.blocked_by == "scope"
    assert run.review_required is True
    assert [item.id for item in queue] == [run.id]


async def test_marking_reviewed_is_recorded_and_idempotent_guard(
    monkeypatch, session_factory, make_user
):
    monkeypatch.setattr(
        service,
        "search_provisions",
        _pinned_search([_hit("中华人民共和国社会救助法", "16", "第十六条　…")]),
    )
    principal = await _principal_for(make_user)

    async with session_factory() as session:
        await service.answer_question(session, principal, "我能不能申请最低生活保障？")
        run = await _latest_run(session)
        reviewed = await runs.mark_reviewed(session, principal, run.id, note="已核对条文，属实")
        await session.commit()
        queue = await runs.pending_reviews(session, principal)

    assert reviewed.reviewed_at is not None
    assert reviewed.review_note == "已核对条文，属实"
    # 复核**不改状态**——状态机记的是「当时怎么走的」，复核是之后另一件事
    assert reviewed.state == "NEEDS_REVIEW"
    assert queue == []


async def test_review_is_scoped_to_the_organization(monkeypatch, session_factory, make_user):
    """问答历史是私有数据（§21.2）——别的组织看不到、也复核不了。"""
    monkeypatch.setattr(
        service,
        "search_provisions",
        _pinned_search([_hit("中华人民共和国社会救助法", "16", "第十六条　…")]),
    )
    owner = await _principal_for(make_user)
    other = await _principal_for(make_user)  # `make_user` 每次建新组织

    async with session_factory() as session:
        await service.answer_question(session, owner, "我能不能申请最低生活保障？")
        run = await _latest_run(session)
        assert await runs.pending_reviews(session, other) == []
        with pytest.raises(LookupError):
            await runs.mark_reviewed(session, other, run.id)
