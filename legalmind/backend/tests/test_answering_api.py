"""问答接口（设计 §9.1、§9.3 第三层、§9.4）。

**不连 Redis、不加载模型**：缓存用内存替身，运行记录直接写库。
重点钉两件事：**SSE 只在终态推正式答案**（§9.4「正式答案通过门禁后发送」），
以及**内容取不到时如实说取不到**（运行记录里本来就没有正文，§21）。
"""

import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.models import AnswerRun, Job
from app.modules.answering import router as answering_router
from app.modules.answering import runs as answer_runs
from tests.helpers import login

pytestmark = pytest.mark.anyio


class _FakeCache:
    """内存版短期缓存。"""

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


@pytest.fixture
def cache(monkeypatch):
    fake = _FakeCache()
    monkeypatch.setattr("app.adapters.cache.from_settings", lambda _settings: fake)
    return fake


async def _sign_in(client, make_user, *roles: str):
    """登录**已经打开**的客户端。

    ⚠️ 顺序不能反：httpx 的 `AsyncClient` 在第一次请求时就打开了，之后再 `async with` 会抛
    `Cannot open a client instance more than once`（实测踩过）。所以先进入上下文、再登录。
    """
    user = await make_user(*(roles or ("knowledge_admin",)))
    await login(client, user.username)
    return user


async def _submit(client, question: str = "工资可以用实物发放吗？", **extra) -> dict:
    response = await client.post("/api/v1/answers", json={"question": question, **extra})
    assert response.status_code == 202, response.text
    return response.json()


async def _finish(session_factory, run_id: str, **fields) -> None:
    """模拟 worker 跑完：直接改运行记录。"""
    async with session_factory() as session, session.begin():
        run = await session.get(AnswerRun, run_id)
        for key, value in fields.items():
            setattr(run, key, value)


async def _drain(session_factory, run_id: str) -> None:
    """把该运行已有的任务标成成功——模拟 worker 已经消费掉初次任务。"""
    async with session_factory() as session, session.begin():
        for job in await session.scalars(
            select(Job).where(Job.payload["run_id"].astext == str(run_id))
        ):
            job.status = "succeeded"


async def _read_events(client, run_id: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    async with client.stream("GET", f"/api/v1/answers/{run_id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        name = None
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: ") and name:
                events.append((name, json.loads(line[len("data: ") :])))
    return events


async def test_submit_requires_authentication(make_client):
    async with make_client() as client:
        response = await client.post("/api/v1/answers", json={"question": "工资怎么发？"})
    assert response.status_code == 401


async def test_submit_without_the_short_term_cache_returns_503(make_client, make_user):
    """**没有缓存就不接异步**——调用方拿到 run id 却取不到结果，不如直接说不行。"""
    async with make_client() as client:
        await _sign_in(client, make_user)
        response = await client.post("/api/v1/answers", json={"question": "工资怎么发？"})
    assert response.status_code == 503
    # ⚠️ 错误响应体是 `{code, message, trace_id}`（全局错误处理器统一过），**不是** FastAPI 的 `detail`
    assert response.json()["code"] == "service_unavailable"
    assert "CACHE_URL" in response.json()["message"]


async def test_submit_creates_a_run_and_a_job(cache, make_client, make_user):
    async with make_client() as client:
        await _sign_in(client, make_user)
        body = await _submit(client, limit=3)
    assert body["state"] == "CREATED"
    # 还没跑，缓存里自然没有内容——**如实回 false**，不是空字符串
    assert body["content_available"] is False
    assert body["answer"] is None
    assert body["evidence_count"] == 0


async def test_get_run_returns_the_cached_content(cache, make_client, make_user):
    async with make_client() as client:
        await _sign_in(client, make_user)
        created = await _submit(client)
        await cache.put(answer_runs.question_key(created["id"]), "工资怎么发？")
        await cache.put(
            f"legalmind:answer:{created['id']}",
            "1. 工资应当以货币形式按月支付（依据：劳动法第五十条）",
        )
        response = await client.get(f"/api/v1/answers/{created['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["content_available"] is True
    assert "工资应当以货币形式按月支付" in body["answer"]


async def test_get_run_says_so_when_content_is_gone(cache, make_client, make_user):
    """**缓存过期就说过期**，不拿别的东西冒充（运行记录里只有哈希，§21）。"""
    async with make_client() as client:
        await _sign_in(client, make_user)
        created = await _submit(client)
        body = (await client.get(f"/api/v1/answers/{created['id']}")).json()
    assert body["content_available"] is False
    assert body["answer"] is None


async def test_sse_pushes_state_then_the_answer_only_at_a_terminal_state(
    cache, monkeypatch, make_client, make_user, session_factory
):
    """§9.4：进度随状态推；**正式答案只在终态推**。"""
    monkeypatch.setattr(answering_router, "_session_factory", lambda: session_factory)
    async with make_client() as client:
        await _sign_in(client, make_user)
        created = await _submit(client)
        run_id = created["id"]
        await cache.put(answer_runs.question_key(run_id), "工资怎么发？")
        await cache.put(f"legalmind:answer:{run_id}", "1. 工资应当以货币形式按月支付")
        await _finish(
            session_factory,
            run_id,
            previous_state="VERIFYING",
            state="ANSWERED",
            published=True,
            evidence=[{"provision_version_id": str(uuid4()), "cited": True}],
        )
        events = await _read_events(client, run_id)

    assert [name for name, _ in events] == ["state", "answer", "done"]
    assert events[0][1]["state"] == "ANSWERED"
    assert events[1][1]["published"] is True
    assert "工资应当以货币形式按月支付" in events[1][1]["answer"]


async def test_sse_marks_a_blocked_run_as_not_published(
    cache, monkeypatch, make_client, make_user, session_factory
):
    """门禁拦下的终态：推的是**拒答说明**，`published=false` —— 不是结论。"""
    monkeypatch.setattr(answering_router, "_session_factory", lambda: session_factory)
    async with make_client() as client:
        await _sign_in(client, make_user)
        created = await _submit(client, "我能不能领低保？")
        run_id = created["id"]
        await cache.put(
            f"legalmind:answer:{run_id}", "这个问题问的是提问者本人的情形，不生成个人结论。"
        )
        await _finish(
            session_factory,
            run_id,
            state="NEEDS_REVIEW",
            blocked_by="scope",
            review_required=True,
        )
        events = await _read_events(client, run_id)

    answer = dict(events)["answer"]
    assert answer["published"] is False
    assert answer["blocked_by"] == "scope"
    assert answer["review_required"] is True
    assert "不生成个人结论" in answer["answer"]


async def test_sse_waits_for_the_result_to_land(
    cache, monkeypatch, make_client, make_user, session_factory
):
    """**终态先写、结果后写**——`answer` 事件不能自相矛盾。

    实测（真实 HTTP 服务）出现过：「状态 ANSWERED，但 `answer` 事件里 `published=false`、
    `evidence_count=0`、内容也取不到」——因为终态是链路写的第一件东西，结果与缓存内容随后才落。
    这里复现那一刻：状态先到终态、结果稍后才落，流**应该等结果**而不是抢先推一个空答案。
    """
    monkeypatch.setattr(answering_router, "_session_factory", lambda: session_factory)
    async with make_client() as client:
        await _sign_in(client, make_user)
        created = await _submit(client)
        run_id = created["id"]
        await _finish(session_factory, run_id, state="ANSWERED")  # 终态先写，结果还没落

        async def land_result():
            await asyncio.sleep(0.3)
            await _finish(
                session_factory,
                run_id,
                published=True,
                answer_sha256="a" * 64,
                evidence=[{"provision_version_id": str(uuid4()), "cited": True}],
            )
            await cache.put(f"legalmind:answer:{run_id}", "1. 工资应当以货币形式按月支付")

        task = asyncio.create_task(land_result())
        events = await _read_events(client, run_id)
        await task

    answer = dict(events)["answer"]
    assert answer["content_available"] is True
    assert answer["published"] is True
    assert answer["evidence_count"] == 1
    assert "工资应当以货币形式按月支付" in answer["answer"]


async def test_sse_times_out_without_pretending(
    cache, monkeypatch, make_client, make_user, session_factory
):
    """**不假装还在跑**：超时就推 `timeout` 并结束，让调用方去别处查。"""
    # 流式生成器**刻意不用请求级会话**（一条 SSE 可能挂几分钟），所以要替换它的会话工厂
    monkeypatch.setattr(answering_router, "_session_factory", lambda: session_factory)
    # ⚠️ **直接换掉配置对象，不走环境变量**：`monkeypatch.setenv` 在本文件里实测不生效
    # （`os.environ` 里已经是 "0"，`Settings()` 读出来仍是 300），原因未查明。
    # 直接给一个超时 1 秒的配置最稳，也把「测试不依赖环境变量传播」这件事写死在代码里。
    monkeypatch.setattr(
        answering_router,
        "get_settings",
        lambda: Settings(
            database_url="postgresql+asyncpg://unused", answer_stream_timeout_seconds=1
        ),
    )
    async with make_client() as client:
        await _sign_in(client, make_user)
        created = await _submit(client)
        events = await _read_events(client, created["id"])

    assert [name for name, _ in events] == ["state", "timeout"]
    assert events[1][1]["detail"].startswith("超过流式等待上限")


async def test_review_marks_and_refuses_a_second_time(cache, make_client, make_user):
    async with make_client() as client:
        await _sign_in(client, make_user)
        created = await _submit(client, "我能不能领低保？")
        first = await client.post(
            f"/api/v1/answers/{created['id']}/review", json={"note": "已核对条文"}
        )
        second = await client.post(f"/api/v1/answers/{created['id']}/review", json={})
    assert first.status_code == 200
    assert first.json()["review_note"] == "已核对条文"
    assert first.json()["reviewed_at"] is not None
    assert second.status_code == 409


async def test_run_is_scoped_to_the_organization(cache, make_client, make_user):
    """问答历史是私有数据（§21.2）——别的组织看不到（**404，不泄露存在性**）。"""
    async with make_client() as owner_client, make_client() as other_client:
        await _sign_in(owner_client, make_user)
        await _sign_in(other_client, make_user)
        created = await _submit(owner_client)
        assert (await other_client.get(f"/api/v1/answers/{created['id']}")).status_code == 404
        assert (
            await other_client.post(f"/api/v1/answers/{created['id']}/review", json={})
        ).status_code == 404


async def test_pending_queue_lists_only_review_required(
    cache, make_client, make_user, session_factory
):
    async with make_client() as client:
        await _sign_in(client, make_user)
        created = await _submit(client, "我能不能领低保？")
        assert (await client.get("/api/v1/answers?pending=true")).json() == []
        await _finish(session_factory, created["id"], state="NEEDS_REVIEW", review_required=True)
        pending = (await client.get("/api/v1/answers?pending=true")).json()
    assert [item["id"] for item in pending] == [created["id"]]


async def test_a_reader_sees_only_their_own_runs(cache, make_client, make_user):
    """⚠️ **普通提问者也要看得到自己问过什么**。

    列表接口原先对**两种模式**都要求 `review.decide`，于是界面上「我提交的运行」对读者直接 403——
    而「列运行列表」本来就不是复核动作。

    同时钉住**不返回全组织**：同组织里别人提交的运行不该出现在我的列表里——问题正文在短期缓存里，
    往往就是当事人的具体情形。**复核人要看别人的运行走待审队列。**
    """
    mine = await make_user("reader")
    theirs = await make_user("reader", organization_id=mine.organization_id)
    async with make_client() as my_client, make_client() as their_client:
        await login(my_client, mine.username)
        await login(their_client, theirs.username)
        my_run = await _submit(my_client, "我的问题：工资怎么发？")
        await _submit(their_client, "同事的问题：单位欠缴社保费怎么办？")

        rows = (await my_client.get("/api/v1/answers")).json()

    assert [item["id"] for item in rows] == [my_run["id"]]


async def test_a_reader_cannot_open_the_review_queue(cache, make_client, make_user):
    """待审队列是**复核人**的视图（组织内别人提交的也要看得到），所以额外要 `review.decide`。"""
    async with make_client() as client:
        await _sign_in(client, make_user, "reader")
        assert (await client.get("/api/v1/answers?pending=true")).status_code == 403


async def test_a_single_run_is_still_readable_across_the_organization(
    cache, make_client, make_user
):
    """**按 id 取单条仍是组织范围**（§21「用户私有数据按租户隔离」），复核流程要靠它。

    ⚠️ 这与「列表只给自己的」**不对称，是有意的**：列表是浏览，单条是**有明确目标**的访问
    （复核人从待审队列点进来、或别人把运行 id 发给你）。**要收紧到「只有本人和复核人能看单条」，
    那是另一个决定**——它会动到复核流程，得先过设计。
    """
    mine = await make_user("reader")
    theirs = await make_user("reader", organization_id=mine.organization_id)
    async with make_client() as my_client, make_client() as their_client:
        await login(my_client, mine.username)
        await login(their_client, theirs.username)
        my_run = await _submit(my_client, "我的问题：工资怎么发？")

        assert (await their_client.get(f"/api/v1/answers/{my_run['id']}")).status_code == 200


async def test_job_payload_never_holds_the_question(cache, make_client, make_user, session_factory):
    """**问题不进数据库**（§21）——它只走短期缓存。"""
    secret = "我身份证是110101199003072316"
    async with make_client() as client:
        await _sign_in(client, make_user)
        created = await _submit(client, f"{secret}，能不能领低保？")
    async with session_factory() as session:
        job = await session.scalar(select(Job).where(Job.payload["run_id"].astext == created["id"]))
    assert job is not None
    assert secret not in json.dumps(job.payload, ensure_ascii=False)


async def test_vague_question_is_asked_back_instead_of_answered(
    cache, monkeypatch, make_client, make_user, session_factory
):
    """笼统问题 → `CLARIFYING`；`answer` 字段里是**反问**，`clarifying=true`。"""
    monkeypatch.setattr(answering_router, "_session_factory", lambda: session_factory)
    async with make_client() as client:
        await _sign_in(client, make_user)
        created = await _submit(client, "工资怎么办？")
        run_id = created["id"]
        await _finish(session_factory, run_id, state="CLARIFYING", blocked_by="clarifying")
        await cache.put(f"legalmind:answer:{run_id}", "你说的工资问题是指哪方面？")
        detail = (await client.get(f"/api/v1/answers/{run_id}")).json()
        events = await _read_events(client, run_id)

    assert detail["clarifying"] is True
    assert detail["published"] is False
    assert detail["blocked_by"] == "clarifying"
    # **流推 `clarify` 就收尾**——不能占着连接等用户（可能等很久）
    assert [name for name, _ in events] == ["state", "clarify", "done"]
    clarify_event = dict(events)["clarify"]
    assert clarify_event["question"] == "你说的工资问题是指哪方面？"
    assert clarify_event["resume"].endswith(f"/{run_id}/clarify")


async def test_clarify_endpoint_continues_the_run(cache, make_client, make_user, session_factory):
    async with make_client() as client:
        await _sign_in(client, make_user)
        created = await _submit(client, "工资怎么办？")
        # 运行要真的停在 CLARIFYING（真实流程里由 worker 跑出来），否则接口会正确拒绝
        await _finish(session_factory, created["id"], state="CLARIFYING")
        await _drain(session_factory, created["id"])
        response = await client.post(
            f"/api/v1/answers/{created['id']}/clarify",
            json={"supplement": "用人单位拖欠工资该怎么办"},
        )
    assert response.status_code == 202
    merged = await cache.get(answer_runs.question_key(created["id"]))
    assert "工资怎么办？" in merged and "用人单位拖欠工资" in merged


async def test_clarify_endpoint_rejects_a_run_that_is_not_waiting(cache, make_client, make_user):
    async with make_client() as client:
        await _sign_in(client, make_user)
        created = await _submit(client)
        response = await client.post(
            f"/api/v1/answers/{created['id']}/clarify", json={"supplement": "补充"}
        )
    assert response.status_code == 409
    # 错误响应体是 `{code, message, trace_id}`，不是 FastAPI 的 `detail`
    assert response.json()["code"] == "conflict"


async def test_clarify_endpoint_is_scoped_to_the_organization(cache, make_client, make_user):
    async with make_client() as owner_client, make_client() as other_client:
        await _sign_in(owner_client, make_user)
        await _sign_in(other_client, make_user)
        created = await _submit(owner_client)
        response = await other_client.post(
            f"/api/v1/answers/{created['id']}/clarify", json={"supplement": "补充"}
        )
    assert response.status_code == 404
