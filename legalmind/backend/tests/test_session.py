"""多轮追问的会话（设计 §9.6、需求 FR-16）。

**这里不加载生成模型**（同 `test_answering.py` 的理由）。要钉住的是三条边界里的两条，
以及「越权不泄露」这类错了就直接伤害可信度的行为：

- **边界 1**：补全只影响「检索与提示用哪句问题」，**历史答案不进提示词**；
- **边界 2**：会话历史是客户数据 → 只进短期缓存、**只存脱敏后的问题、不存答案正文**；
- **越权按不存在处理**——不能因为「查别人的会话」而泄露会话是否存在；
- **降级**：缓存不可用时多轮不可用，但**不影响单轮**（单轮是主链路）。
"""

import json
from uuid import uuid4

import pytest

from app.adapters import generation
from app.adapters.cache import AnswerCache, cache_key
from app.core.security import Principal
from app.modules.answering import runs as answer_runs
from app.modules.answering import service
from app.modules.answering.runs import question_key
from app.modules.answering.session import (
    CONTEXT_TURNS,
    MAX_TURNS_KEPT,
    Turn,
    compose_query,
    conversation_key,
    read_turns,
    record_turn,
)
from app.modules.redaction.service import redact
from app.modules.retrieval.schemas import CitationOut, ProvisionHit, SearchResponse

pytestmark = pytest.mark.anyio


class _FakeCache:
    """最小缓存替身——**真 Redis 不该进单元测试**（同 `test_clarify.py` 的做法）。"""

    def __init__(self, available: bool = True):
        self.store: dict[str, str] = {}
        self._available = available

    @property
    def available(self) -> bool:
        return self._available

    async def put(self, key: str, text: str) -> bool:
        if not self._available:
            return False
        self.store[key] = text
        return True

    async def get(self, key: str):
        return self.store.get(key)

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)

    async def close(self) -> None:
        return None


def _principal(user_id=None) -> Principal:
    return Principal(
        organization_id=uuid4(),
        user_id=user_id or uuid4(),
        roles=frozenset({"reader"}),
    )


def _answer_with(version_ids):
    """造一个只有 `citations` 有用的小对象——`record_turn` 只读这一个字段。"""

    class _Answer:
        citations = tuple(
            service.Citation(
                instrument_title="中华人民共和国劳动法",
                provision_number="100",
                provision_display="第一百条",
                legal_status="effective",
                provision_version_id=version_id,
            )
            for version_id in version_ids
        )

    return _Answer()


def _hit(text: str, version_id=None) -> ProvisionHit:
    return ProvisionHit(
        instrument_title="中华人民共和国劳动法",
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
        provision_number="100",
        provision_display="第一百条",
        text=text,
        text_sha256="0" * 64,
        citation=CitationOut(
            instrument_id=uuid4(),
            legal_version_id=uuid4(),
            provision_identity_id=uuid4(),
            provision_version_id=version_id or uuid4(),
            artifact_id=uuid4(),
            chunk_id=uuid4(),
        ),
    )


# --------------------------------------------------------------------------- 补全


def test_followup_without_context_is_returned_unchanged():
    assert compose_query([], "那如果是这样呢？") == "那如果是这样呢？"


def test_followup_is_completed_with_the_previous_question():
    """追问本身没有实质内容，补上上文之后才自足——**这正是多轮的意义**。"""
    composed = compose_query([Turn(question="单位欠缴社保费会被怎么处理")], "那如果是这样呢？")
    assert "单位欠缴社保费" in composed
    assert "那如果是这样呢" in composed


def test_only_the_most_recent_turn_is_used():
    """⚠️ **只取最近 1 轮是有意的**（§9.6）：拼接越长越容易被上文里的无关实体带偏。"""
    assert CONTEXT_TURNS == 1
    composed = compose_query(
        [Turn(question="很早以前问的无关问题"), Turn(question="单位欠缴社保费会被怎么处理")],
        "那如果是这样呢？",
    )
    assert "单位欠缴社保费" in composed
    assert "很早以前问的无关问题" not in composed


def test_blank_previous_questions_do_not_pollute_the_query():
    composed = compose_query([Turn(question="   ")], "拖欠工资怎么办？")
    assert composed == "拖欠工资怎么办？"


# --------------------------------------------------------------------------- 读会话


async def test_reading_a_conversation_without_a_cache_returns_nothing():
    assert await read_turns(AnswerCache(), uuid4(), _principal()) == []


async def test_reading_an_unknown_conversation_returns_nothing():
    assert await read_turns(_FakeCache(), uuid4(), _principal()) == []


async def test_another_users_conversation_looks_like_it_does_not_exist():
    """⚠️ **越权按不存在处理**——返回「无权访问」等于确认了会话存在。"""
    cache = _FakeCache()
    owner = _principal()
    session_id = uuid4()
    await record_turn(cache, session_id, owner, "单位欠缴社保费", _answer_with([uuid4()]))

    assert await read_turns(cache, session_id, owner) != []
    assert await read_turns(cache, session_id, _principal()) == []


async def test_a_corrupted_cache_entry_does_not_crash_reading():
    cache = _FakeCache()
    session_id = uuid4()
    cache.store[conversation_key(session_id)] = "{ 这不是 JSON"
    assert await read_turns(cache, session_id, _principal()) == []

    cache.store[conversation_key(session_id)] = json.dumps({"user_id": "x", "turns": "不是列表"})
    assert await read_turns(cache, session_id, _principal()) == []


# --------------------------------------------------------------------------- 写会话


async def test_recorded_turn_keeps_the_redacted_question_and_the_citation_ids():
    """**边界 2**：只存脱敏后的问题与证据引用，**不存答案正文**。"""
    cache = _FakeCache()
    principal = _principal()
    session_id = uuid4()
    version_id = uuid4()
    question = "我的手机号 13800138000，单位欠缴社保费怎么办？"

    await record_turn(cache, session_id, principal, question, _answer_with([version_id]))

    raw = cache.store[conversation_key(session_id)]
    # 存的是**脱敏后**的问题——与异步路径同一口径（问题离开进程前必须先脱敏）
    assert redact(question)[0] in raw
    if redact(question)[0] != question:
        assert "13800138000" not in raw
    assert str(version_id) in raw

    turns = await read_turns(cache, session_id, principal)
    assert len(turns) == 1
    assert turns[0].question == redact(question)[0]
    assert turns[0].provision_version_ids == (str(version_id),)


async def test_the_conversation_never_contains_the_answer_body():
    """**边界 2**：答案正文另有 `answer:{run_id}`，会话里再存一份早晚不一致。

    这里直接对着缓存里的原始字符串断言——比读回来再比更硬。
    """
    cache = _FakeCache()
    principal = _principal()
    session_id = uuid4()
    secret = "因此，作为特困人员，可以领取社会救助。"

    class _AnswerWithBody:
        citations = ()
        answer = secret

    await record_turn(cache, session_id, principal, "我能领吗", _AnswerWithBody())

    assert secret not in cache.store[conversation_key(session_id)]


async def test_conversation_is_trimmed_to_the_kept_window():
    cache = _FakeCache()
    principal = _principal()
    session_id = uuid4()
    for index in range(MAX_TURNS_KEPT + 3):
        await record_turn(cache, session_id, principal, f"第 {index} 个问题", _answer_with([]))

    turns = await read_turns(cache, session_id, principal)
    assert len(turns) == MAX_TURNS_KEPT
    # 保留的是**最近**的若干轮
    assert turns[-1].question == f"第 {MAX_TURNS_KEPT + 2} 个问题"
    assert turns[0].question == "第 3 个问题"


async def test_recording_without_a_cache_reports_failure_without_raising():
    """**降级**：缓存不可用时多轮不可用，但**不能因此让本次回答失败**。"""
    assert (
        await record_turn(AnswerCache(), uuid4(), _principal(), "问题", _answer_with([])) is False
    )


def test_conversation_keys_do_not_collide_with_answer_or_question_keys():
    run_id = uuid4()
    assert conversation_key(run_id) != cache_key(run_id)
    assert conversation_key(run_id) != question_key(run_id)


# --------------------------------------------------------------------------- 与链路接线


async def test_followup_is_completed_before_retrieval_but_the_typed_question_is_kept(
    monkeypatch, session_factory, make_user
):
    """**边界 1**：补全后的自足问题进检索；`Answer.question` 仍是**用户实际敲的那句**。

    分开记是为了可追溯——否则事后看 `question` 会以为系统答非所问。
    """
    seen: list[str] = []

    async def search(_session, _principal, query):
        seen.append(query.semantic or "")
        return SearchResponse(
            hits=[_hit("第一百条　用人单位无故不缴纳社会保险费的，由劳动行政部门责令其限期缴纳。")],
            truncated=False,
            path="keyword",
        )

    monkeypatch.setattr(service, "search_provisions", search)
    monkeypatch.setattr(generation, "available", lambda *_a, **_k: True)
    monkeypatch.setattr(
        generation,
        "generate",
        lambda *_a, **_k: json.dumps(
            {
                "claims": [
                    {"claim_id": "c1", "text": "由劳动行政部门责令限期缴纳", "evidence_ids": ["1"]}
                ],
                "missing_facts": [],
                "conflicts": [],
            },
            ensure_ascii=False,
        ),
    )

    user = await make_user("reader")
    principal = Principal(
        organization_id=user.organization_id, user_id=user.id, roles=frozenset({"reader"})
    )
    cache = _FakeCache()
    session_id = uuid4()

    async with session_factory() as session:
        # 第一轮：建立上文
        first = await service.answer_question(
            session,
            principal,
            "单位欠缴社保费会被怎么处理？",
            session_id=session_id,
            cache=cache,
        )
        # 第二轮：追问本身没有实质内容
        second = await service.answer_question(
            session, principal, "那如果是这样呢？", session_id=session_id, cache=cache
        )

    assert first.resolved_question is None  # 第一轮没有上文，不需要补全
    assert second.question == "那如果是这样呢？"  # 用户实际敲的那句
    assert second.resolved_question is not None
    assert "单位欠缴社保费" in second.resolved_question
    # 检索收到的是**补全后**的问题，不是那句无从检索的追问
    assert seen[1] == second.resolved_question
    assert "单位欠缴社保费" in seen[1]

    turns = await read_turns(cache, session_id, principal)
    assert [item.question for item in turns] == [
        "单位欠缴社保费会被怎么处理？",
        "那如果是这样呢？",
    ]


async def test_single_turn_does_not_touch_the_conversation_cache(
    monkeypatch, session_factory, make_user
):
    """**单轮是主链路**：不带 `session_id` 时不该留下任何会话痕迹。"""
    monkeypatch.setattr(service, "search_provisions", _pinned_search([]))
    user = await make_user("reader")
    principal = Principal(
        organization_id=user.organization_id, user_id=user.id, roles=frozenset({"reader"})
    )
    cache = _FakeCache()

    async with session_factory() as session:
        answer = await service.answer_question(
            session, principal, "量子计算专利强制许可的审查标准是什么？", cache=cache
        )

    assert answer.session_id is None
    assert answer.resolved_question is None
    assert answer.answer == service.NO_EVIDENCE
    assert cache.store == {}


def _pinned_search(hits, path: str = "keyword"):
    async def search(_session, _principal, _query):
        return SearchResponse(hits=list(hits), truncated=False, path=path)

    return search


async def test_submit_question_carries_the_session_id_to_the_worker(session_factory, make_user):
    """异步路径：会话 id 必须随任务传到 worker——**补全发生在 worker 里**（那里才读得到会话缓存）。"""
    user = await make_user("reader")
    principal = Principal(
        organization_id=user.organization_id, user_id=user.id, roles=frozenset({"reader"})
    )
    cache = _FakeCache()
    session_id = uuid4()

    async with session_factory() as session, session.begin():
        run, job = await answer_runs.submit_question(
            session, principal, "拖欠工资怎么办？", cache=cache, session_id=session_id
        )

    assert job.payload["session_id"] == str(session_id)
    assert run.config["session_id"] == str(session_id)


async def test_running_the_question_does_not_wipe_the_session_id(session_factory, make_user):
    """⚠️ **实测踩过的回归**：`apply_run_result` 原本是 `run.config = config`——**整个覆盖**，
    把 `submit_question` 在提交时写进去的 `session_id` 悄悄抹掉，接口回传因此恒为 null，
    前端永远拼不出上文（现象是「追问完全不带上下文」，但提交请求里明明带着 id）。

    提交侧与运行时写的键**不重叠**，所以正确做法是**合并**。
    """
    user = await make_user("reader")
    principal = Principal(
        organization_id=user.organization_id, user_id=user.id, roles=frozenset({"reader"})
    )
    cache = _FakeCache()
    session_id = uuid4()

    async with session_factory() as session:
        async with session.begin():
            run, _job = await answer_runs.submit_question(
                session, principal, "拖欠工资怎么办？", cache=cache, session_id=session_id
            )
        # 模拟 worker 跑完后的「填结果」那一步
        answer_runs.apply_run_result(
            run,
            question="拖欠工资怎么办？",
            answer=service.Answer(
                question="拖欠工资怎么办？",
                answer="……",
                citations=(),
                path="vector",
                model="m",
                seconds=1.0,
            ),
            tracker=answer_runs.RunTracker(),
            config={"model": "m", "retrieval_path": "vector"},
        )

    assert run.config["session_id"] == str(session_id)  # 提交侧写的键还在
    assert run.config["model"] == "m"  # 运行时写的键也进去了
