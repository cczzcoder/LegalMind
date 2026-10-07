"""问答接口（设计 §9.1、§9.3 第三层、§9.4、§13）。

**为什么问答要异步**：§14.1 把生成模型列在「暂不启动（常驻）」里——**重型任务不常驻 API 进程**
（§14.2 的分阶段串行原则）。所以 API 只负责**提交任务**，worker 在后台跑，进度与结果通过下面
三个端点交付。这既是资源档位的要求，也顺带让长耗时的生成不占住请求连接。

**⚠️ SSE 只在核验前发送进度状态，正式答案通过门禁后发送**（§9.4 原话）：

- 每次状态转移推一个 `state` 事件；
- **只有运行进入终态**（`ANSWERED` / `PARTIAL` / `NEEDS_REVIEW` / `INSUFFICIENT_EVIDENCE` /
  `FAILED`）才推 `answer` 事件——门禁没过的那几种，`answer` 里是**拒答说明**（`published=false`），
  不是结论；
- 然后推 `done` 结束。

**⚠️ 流式端点不用请求级的会话**：一条 SSE 连接可能挂几分钟，占着一个连接池的位置不划算。
生成器**每次轮询开一个短会话**（`_session_factory()`），用完即还。
"""

import asyncio
import json
from datetime import UTC, datetime
from time import monotonic
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import cache as answer_cache
from app.core.config import get_settings
from app.core.database import SessionFactory, get_session
from app.core.security import Principal
from app.models import ANSWER_RUN_TERMINAL_STATES, AnswerRun
from app.modules.answering import runs as answer_runs
from app.modules.answering.schemas import (
    AnswerRunOut,
    ClarifyRequest,
    ReviewRequest,
    SubmitQuestion,
)
from app.modules.authorization.service import (
    DOCUMENT_READ,
    REVIEW_DECIDE,
    AuthorizationService,
    require_permission,
)

router = APIRouter(tags=["answering"])

#: 终态到了、但结果还没落库/进缓存时，最多再等这么多次轮询（默认 0.5s × 10 = 5s）。
#: 这只是给「终态先写、结果后写」这个固有顺序留的宽限，不是重试机制。
CONTENT_GRACE_POLLS = 10

#: **挂起状态**：运行没结束（还要等用户补充后继续），但流不能一直挂着等——推完就收尾。
#: §9.1 的 `CLARIFYING → RETRIEVING` 决定了它不是终态，所以这里单独列一份。
SUSPENDED_STATES = ("CLARIFYING",)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ReaderDep = Annotated[Principal, Depends(require_permission(DOCUMENT_READ))]
ReviewerDep = Annotated[Principal, Depends(require_permission(REVIEW_DECIDE))]


def _session_factory():
    """流式生成器用的会话工厂。

    **抽成函数是为了让测试能替换它**——测试里的 `SessionFactory` 指向占位库，请求级的
    `get_session` 才是被覆盖过的那个；流式端点刻意不用请求级会话（见模块 docstring）。
    """
    return SessionFactory


async def _load(session: AsyncSession, principal: Principal, run_id: UUID) -> AnswerRun:
    """按组织取运行记录。**取不到与无权访问表现一致**（不泄露存在性）。"""
    run = await session.get(AnswerRun, run_id)
    if run is None or run.organization_id != principal.organization_id:
        raise HTTPException(status_code=404, detail="Answer run not found")
    return run


async def _to_out(run: AnswerRun, cache) -> AnswerRunOut:
    """把运行记录 + 短期缓存里的内容拼成响应。"""
    answer = await answer_runs.read_answer(cache, run.id)
    question = await answer_runs.read_question(cache, run.id) if answer is not None else None
    # 会话 id 在配置快照里（§9.6）；存的是字符串，转回 UUID，坏值按「没有会话」处理
    raw_session = (run.config or {}).get("session_id")
    try:
        session_id = UUID(raw_session) if raw_session else None
    except (ValueError, TypeError):
        session_id = None
    return AnswerRunOut(
        id=run.id,
        state=run.state,
        previous_state=run.previous_state,
        published=run.published,
        blocked_by=run.blocked_by,
        clarifying=run.state in SUSPENDED_STATES,
        review_required=run.review_required,
        reviewed_by=run.reviewed_by,
        reviewed_at=run.reviewed_at,
        review_note=run.review_note,
        evidence_count=len(run.evidence or []),
        seconds=run.seconds,
        created_at=run.created_at,
        content_available=answer is not None,
        session_id=session_id,
        question=question,
        answer=answer,
    )


@router.post("/answers", response_model=AnswerRunOut, status_code=202)
async def submit_question(data: SubmitQuestion, session: SessionDep, principal: ReaderDep):
    """提交一次异步问答（设计 §9.4）；需 `document.read`。

    **需要短期缓存**（`CACHE_URL`）：调用方拿到的是 run id，答案要自己去取，没有缓存就没有结果
    可交付——所以缓存不可用时**直接拒绝**，而不是让它跑完再发现取不到。
    """
    cache = answer_cache.from_settings(get_settings())
    try:
        if not cache.available:
            raise HTTPException(
                status_code=503,
                detail="Short-term cache is not configured (CACHE_URL); async answering is unavailable",
            )
        async with session.begin():
            run, _job = await answer_runs.submit_question(
                session,
                principal,
                data.question,
                cache=cache,
                # §9.6：留空即单轮——**单轮是主链路，不能因为会话功能而不可用**
                session_id=data.session_id,
                limit=data.limit,
                max_new_tokens=data.max_new_tokens,
                model=data.model,
            )
        return await _to_out(run, cache)
    finally:
        await cache.close()


@router.get("/answers", response_model=list[AnswerRunOut])
async def list_runs(
    session: SessionDep,
    principal: ReaderDep,
    pending: bool = Query(default=False, description="只看待人工复核的（待审队列）"),
    limit: int = Query(default=50, ge=1, le=200),
):
    """列出**我提交的**问答运行；`pending=true` 即**待审队列**（设计 §9.3 第三层）。

    ⚠️ **两种模式的权限不同，这是有意的**（此前两种模式都按 `review.decide` 要求，
    结果是**普通提问者连自己问过什么都看不到**——界面上的「我提交的运行」直接 403）：

    - `pending=false` → **我提交的运行**（`requested_by` 是本人）。问过问题的人就该看得到自己问过
      什么，所以只要 `document.read`。
    - `pending=true` → **待审队列**：组织内 `NEEDS_REVIEW` 且未复核的运行，**别人提交的也要看得到**，
      所以额外要 `review.decide`。

    ⚠️ **`pending=false` 刻意不返回全组织的运行**：那会让任何一个能提问的人看到同事问过什么
    （问题正文在短期缓存里，往往就是当事人的具体情形）。**复核人要看别人的运行，走待审队列**；
    按 id 取单条仍是组织范围（§21「用户私有数据按租户隔离」，且复核流程需要它）。
    """
    if pending and not AuthorizationService.can(principal, REVIEW_DECIDE):
        raise HTTPException(status_code=403, detail="Permission denied")

    cache = answer_cache.from_settings(get_settings())
    try:
        if pending:
            rows = await answer_runs.pending_reviews(session, principal, limit=limit)
        else:
            rows = list(
                await session.scalars(
                    select(AnswerRun)
                    .where(
                        AnswerRun.organization_id == principal.organization_id,
                        AnswerRun.requested_by == principal.user_id,
                    )
                    .order_by(AnswerRun.created_at.desc())
                    .limit(limit)
                )
            )
        return [await _to_out(run, cache) for run in rows]
    finally:
        await cache.close()


@router.get("/answers/{run_id}", response_model=AnswerRunOut)
async def get_run(run_id: UUID, session: SessionDep, principal: ReaderDep):
    """查看一次问答运行（含短期缓存里的问题与回答，均已脱敏）；需 `document.read`。"""
    cache = answer_cache.from_settings(get_settings())
    try:
        run = await _load(session, principal, run_id)
        return await _to_out(run, cache)
    finally:
        await cache.close()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _event_stream(run_id: UUID, principal: Principal):
    """SSE 事件流（设计 §9.4）。**每次轮询开一个短会话**，不占请求级的连接。"""
    settings = get_settings()
    cache = answer_cache.from_settings(settings)
    deadline = monotonic() + settings.answer_stream_timeout_seconds
    last_state: str | None = None
    grace_polls = 0
    try:
        while True:
            async with _session_factory()() as session:
                run = await session.get(AnswerRun, run_id)
            if run is None or run.organization_id != principal.organization_id:
                yield _sse("error", {"detail": "运行记录不存在"})
                return

            if run.state != last_state:
                last_state = run.state
                yield _sse(
                    "state",
                    {
                        "state": run.state,
                        "previous_state": run.previous_state,
                        "at": datetime.now(UTC).isoformat(timespec="seconds"),
                    },
                )

            if run.state in SUSPENDED_STATES:
                # 两种情况都长得像「在等用户」，但**只有一种真的在等**：
                #   ① 刚提交补充、任务还在飞 → 运行马上就会有进展，**流要继续跟**；
                #   ② 确实没人接手 → 这才是「在等用户」，推 clarify 就收尾（不占着连接等下去）。
                # 判据用任务表（`has_in_flight_job`）——**任务表才是「在飞」的事实来源**。
                async with _session_factory()() as session:
                    in_flight = await answer_runs.has_in_flight_job(session, run.id)
                clarification = await answer_runs.read_answer(cache, run.id)
                if in_flight or (clarification is None and grace_polls < CONTENT_GRACE_POLLS):
                    # 结果还没落（终态先写、内容后写的固有顺序，见下）也再等等
                    if clarification is None:
                        grace_polls += 1
                    if monotonic() < deadline:
                        await asyncio.sleep(settings.answer_stream_poll_seconds)
                        continue
                yield _sse(
                    "clarify",
                    {
                        "state": run.state,
                        "question": clarification,
                        "resume": f"/api/v1/answers/{run.id}/clarify",
                    },
                )
                yield _sse("done", {})
                return

            if run.state in ANSWER_RUN_TERMINAL_STATES:
                # **正式答案通过门禁后发送**（§9.4）：终态才推 answer。
                # 门禁没过的那几种，这里推的是**拒答说明**，`published=false`。
                #
                # ⚠️ **终态先写、结果后写**：终态由链路的 `on_state` 回调写（那一刻结果字段还没填），
                # 结果与缓存内容随后才落。实测（真实 HTTP）因此出现过「状态 ANSWERED、但 answer
                # 事件里 published=false、evidence_count=0、内容也取不到」的**自相矛盾**。
                # 所以这里给结果一个**有界的宽限**：终态到了但结果还没就位就继续等，
                # 等到结果与内容都在了再推 answer。
                answer = await answer_runs.read_answer(cache, run.id)
                if (
                    answer is None
                    and run.answer_sha256 is None
                    and monotonic() < deadline
                    and grace_polls < CONTENT_GRACE_POLLS
                ):
                    grace_polls += 1
                    await asyncio.sleep(settings.answer_stream_poll_seconds)
                    continue
                question = await answer_runs.read_question(cache, run.id) if answer else None
                yield _sse(
                    "answer",
                    {
                        "state": run.state,
                        "published": run.published,
                        "blocked_by": run.blocked_by,
                        "review_required": run.review_required,
                        "evidence_count": len(run.evidence or []),
                        "content_available": answer is not None,
                        "question": question,
                        "answer": answer,
                        "note": None
                        if answer is not None
                        else "内容不可用：短期缓存已过期或未配置（运行记录里不存正文，设计 §21）",
                    },
                )
                yield _sse("done", {})
                return

            if monotonic() > deadline:
                # **不假装还在跑**：如实告诉调用方「还没跑完」，并给出去哪儿看
                yield _sse(
                    "timeout",
                    {
                        "state": run.state,
                        "detail": "超过流式等待上限，运行仍在后台继续；请改用 GET /answers/{id} 查询",
                    },
                )
                return

            await asyncio.sleep(settings.answer_stream_poll_seconds)
    finally:
        await cache.close()


@router.get("/answers/{run_id}/events")
async def stream_run(run_id: UUID, principal: ReaderDep):
    """SSE 进度流（设计 §9.4）；需 `document.read`。

    `state` 事件随状态转移推送，**`answer` 事件只在终态推送**（门禁未过的推的是拒答说明），
    最后是 `done`。超时推 `timeout` 后结束——**不假装还在跑**。
    """
    return StreamingResponse(
        _event_stream(run_id, principal),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/answers/{run_id}/clarify", response_model=AnswerRunOut, status_code=202)
async def clarify_run(
    run_id: UUID, data: ClarifyRequest, session: SessionDep, principal: ReaderDep
):
    """回答澄清问题、**继续同一个运行**（设计 §9.1 的 `CLARIFYING → RETRIEVING`）；需 `document.read`。

    补充会与原问题合并（都先脱敏）后重新投递任务；运行记录里仍然只有哈希。
    不在待澄清状态 → 409；上一次补充还在处理中 → 409。
    """
    cache = answer_cache.from_settings(get_settings())
    try:
        if not cache.available:
            raise HTTPException(
                status_code=503,
                detail="Short-term cache is not configured (CACHE_URL); cannot continue",
            )
        async with session.begin():
            try:
                run, _job = await answer_runs.submit_clarification(
                    session, principal, run_id, data.supplement, cache=cache
                )
            except LookupError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except ValueError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
        return await _to_out(run, cache)
    finally:
        await cache.close()


@router.post("/answers/{run_id}/review", response_model=AnswerRunOut)
async def review_run(
    run_id: UUID, data: ReviewRequest, session: SessionDep, principal: ReviewerDep
):
    """人工复核（设计 §9.3 第三层）；需 `review.decide`，写审计。

    ⚠️ **复核不改变运行状态**——状态机记的是「当时怎么走的」，复核是之后发生的另一件事，
    结论写在 `review_note`。重复复核返回 409。
    """
    cache = answer_cache.from_settings(get_settings())
    try:
        async with session.begin():
            try:
                run = await answer_runs.mark_reviewed(session, principal, run_id, note=data.note)
            except LookupError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except ValueError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
        return await _to_out(run, cache)
    finally:
        await cache.close()
