"""§9.1 的执行状态机与运行记录：把一次问答「走到哪、为什么停」显式留下来。

设计 §9.1 的原话是「状态定义、转移条件、每步授权检查和核验门禁**由本系统显式声明**；不使用
框架的隐式重试或自动补全绕过权限、证据绑定和核验」。所以在接入编排框架之前，先把两件东西立起来：

1. **转移表**（`app.models.ANSWER_RUN_TRANSITIONS`）——不在表里的转移是**非法的**，
   `RunTracker.to()` 当场报错，而不是悄悄走成别的状态；
2. **运行记录**（`answer_runs`）——跑完就丢的分支判断，看不出一次运行停在哪、为什么停。

**只记元数据、证据引用与配置快照，不记问题与回答正文**（正文只留 sha256）：§9.4 要求保存证据与
配置快照，§21 要求「客户个性化问答采用临时检索，**客户数据不落库**」。两者合起来就是这张表的形状。

**为什么先不引入 LangGraph**：状态机由本系统显式声明这一条已经满足，而当前链路是线性的六步；
引入 langgraph + langchain 会带来一整棵依赖树，换不到可测收益——与本项目拒绝 Elasticsearch、
拒绝迁 Neo4j、拒绝把重排序放进默认是同一条口径（见《技术决策与踩坑记录》§6）。**需要时再按第 18
节的触发条件评估**：真正的触发点是「多分支 + 需要中断恢复 + 需要流式进度」同时成立，也就是异步
answer-run 落地的时候。
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.cache import AnswerCache, cache_key
from app.core.security import Principal
from app.models import (
    ANSWER_RUN_TERMINAL_STATES,
    ANSWER_RUN_TRANSITIONS,
    AnswerRun,
    Job,
)
from app.modules.authorization.grants import record_audit
from app.modules.redaction.service import redact

#: 异步问答的任务类型。
QUESTION_JOB = "answer.question"
#: 幂等键里带的配置版本——**改了检索参数、提示词或生成模型就要递增**，否则同一个 run 重投会被
#: 幂等键挡住（同一个 run 只会投一次，这里的版本号是为了让「配置变了」这件事在键上留痕）。
ANSWER_CONFIG_VERSION = "1"


class IllegalTransition(RuntimeError):
    """非法状态转移。

    **不静默接受**（§9.1「不使用框架的隐式重试或自动补全绕过……核验」）——走到不该走的状态，
    说明代码与转移表对不上，宁可当场炸掉也不要记一条假的状态轨迹。
    """


def can_transition(source: str, target: str) -> bool:
    return target in ANSWER_RUN_TRANSITIONS.get(source, ())


@dataclass
class RunTracker:
    """一次运行的**状态轨迹**。每一步都必须声明自己转移到哪个状态。

    `trail` 只留最后一步的来源（表里就一列 `previous_state`）——完整轨迹靠日志，不靠表：
    表是给人查「这次停在哪、为什么」的，不是事件流。
    """

    state: str = "CREATED"
    previous: str | None = None
    trail: list[str] = field(default_factory=list)

    def to(self, target: str) -> None:
        if not can_transition(self.state, target):
            raise IllegalTransition(
                f"§9.1 的转移表里没有 {self.state} → {target}"
                f"（允许：{ANSWER_RUN_TRANSITIONS.get(self.state, ())}）"
            )
        self.trail.append(self.state)
        self.previous = self.state
        self.state = target

    @property
    def terminal(self) -> bool:
        return self.state in ANSWER_RUN_TERMINAL_STATES


def _digest(text: str | None) -> str | None:
    return None if not text else sha256(text.encode("utf-8")).hexdigest()


def evidence_refs(citations, cited_ids: tuple[str, ...] = ()) -> list[dict]:
    """证据引用：**条款版本 ID + 效力状态 + 是否被主张引用**。

    刻意**不存条文正文**——正文在公共表里，问答记录属用户私有数据（§21.2）。
    """
    cited = set(cited_ids)
    return [
        {
            "provision_version_id": str(item.provision_version_id),
            "instrument_title": item.instrument_title,
            "provision_display": item.provision_display,
            "legal_status": item.legal_status,
            "cited": str(index) in cited,
        }
        for index, item in enumerate(citations, start=1)
    ]


def apply_run_result(
    run: AnswerRun, *, question: str, answer, tracker: RunTracker, config: dict
) -> AnswerRun:
    """把一次运行的**结果**填进运行记录。

    **新建与更新走同一条路**：同步问答在这里建行；异步问答在 `submit_question` 时就建了行
    （状态 `CREATED`），执行完只更新它。⚠️ 实测踩过：异步路径早期让 `record_run` 又建了一行，
    结果**同一次运行留下两条记录**——一条只有状态、一条只有结果。**一次运行只能有一行。**
    """
    run.state = tracker.state
    run.previous_state = tracker.previous
    run.question_sha256 = _digest(question) or run.question_sha256
    run.answer_sha256 = _digest(answer.answer)
    run.evidence = evidence_refs(answer.citations, answer.cited_evidence_ids)
    # ⚠️ **合并，不是覆盖**：`submit_question` 在**提交时**就往 config 里写了提交参数（如
    # `session_id`），而这里补的是**运行时**的配置快照（模型、检索通路、提示词哈希）。
    # 整个覆盖会把提交侧的键**悄悄抹掉**——实测踩过：`session_id` 提交时写进去了，
    # 跑完却没了，接口回传的 `session_id` 恒为 null，前端因此永远拼不出上文。
    # 两个来源的键**不重叠**，合并才是「配置快照」该有的样子。
    run.config = {**(run.config or {}), **config}
    run.blocked_by = answer.blocked_by
    run.published = answer.published
    run.seconds = answer.seconds
    # 待审队列只收 `NEEDS_REVIEW`——`PARTIAL` 是「限制回答范围」，按 §9.4 不必转人工
    run.review_required = tracker.state == "NEEDS_REVIEW"
    return run


async def record_run(
    session: AsyncSession,
    *,
    principal: Principal,
    question: str,
    answer,
    tracker: RunTracker,
    config: dict,
    run: AnswerRun | None = None,
) -> AnswerRun:
    """把一次运行落库。**调用方负责事务**。

    ``run`` 给了就**更新它**（异步路径在 `submit_question` 时已经建好行），没给就新建。
    """
    if run is None:
        run = AnswerRun(
            organization_id=principal.organization_id,
            requested_by=principal.user_id,
        )
        session.add(run)
    apply_run_result(run, question=question, answer=answer, tracker=tracker, config=config)
    await session.flush()
    return run


async def pending_reviews(
    session: AsyncSession, principal: Principal, *, limit: int = 50
) -> list[AnswerRun]:
    """待人工复核的运行（§9.3 第三层）。**按组织过滤**——问答历史是私有数据（§21.2）。"""
    statement = (
        select(AnswerRun)
        .where(
            AnswerRun.organization_id == principal.organization_id,
            AnswerRun.review_required.is_(True),
            AnswerRun.reviewed_at.is_(None),
        )
        .order_by(AnswerRun.created_at)
        .limit(limit)
    )
    return list(await session.scalars(statement))


async def mark_reviewed(
    session: AsyncSession,
    principal: Principal,
    run_id,
    *,
    note: str | None = None,
) -> AnswerRun:
    """标记一条待审运行已人工复核。**调用方负责事务**，并应校验权限。

    ⚠️ 复核**不改变** `state`——状态机是「这次运行当时怎么走的」的历史事实，复核是之后发生的
    另一件事。要表达「复核结论」用 `review_note`。
    """
    run = await session.get(AnswerRun, run_id)
    if run is None or run.organization_id != principal.organization_id:
        raise LookupError("运行记录不存在")
    if run.reviewed_at is not None:
        raise ValueError("该运行已经复核过了")
    run.reviewed_by = principal.user_id
    run.reviewed_at = datetime.now(UTC)
    run.review_note = note
    record_audit(
        session,
        principal,
        run.id,
        "answer_run.reviewed",
        {"state": run.state, "blocked_by": run.blocked_by, "note": note},
    )
    await session.flush()
    return run


class CacheUnavailable(RuntimeError):
    """未配置短期缓存（`CACHE_URL` 为空）时提交异步问答。

    **异步靠缓存交付结果**——调用方拿到的是 run id，答案要自己去缓存里取。没有缓存就没有结果
    可交付，所以这里**直接拒绝**，而不是让它跑完再发现取不到。同步问答不受影响。
    """


def question_key(run_id) -> str:
    """问题的缓存键。

    **问题也要经缓存**：异步的 worker 在另一个进程里，问题得传过去；而 `jobs.payload` 是**数据库
    里的 JSONB**——把问题写进去就违反 §21「客户数据不落库」。所以问题走短期缓存，与答案同命运
    （TTL 到期即焚）。
    """
    return f"legalmind:question:{run_id}"


async def submit_question(
    session: AsyncSession,
    principal: Principal,
    question: str,
    *,
    cache: AnswerCache,
    session_id: UUID | None = None,
    limit: int = 5,
    max_new_tokens: int = 512,
    model: str | None = None,
) -> tuple[AnswerRun, Job]:
    """提交一次异步问答（设计 §9.4）：建运行记录（`CREATED`）+ 投递任务，**立刻返回**。

    ⚠️ **问题先脱敏再进缓存**（它必须经缓存传给 worker）。这带来一个**有意的不对称**：同步问答
    用原问题检索，异步用脱敏后的问题——因为只有异步的问题需要离开进程。脱敏只替换身份证 / 案号 /
    电话这类标识，不影响检索（语料是法律法规，不含这些）。

    ⚠️ `question_sha256` 取**脱敏后**文本的哈希：短问题的原文哈希可被穷举反推（比如一个身份证号），
    脱敏后的哈希没有这个风险。

    `session_id` 非空时走**多轮追问**（§9.6）：它随任务一起传给 worker——**补全发生在 worker 里**
    （那里才读得到会话缓存），所以这里只是把它带过去，不在这里读会话。
    """
    if not cache.available:
        raise CacheUnavailable("未配置短期缓存（CACHE_URL），无法提交异步问答；请改用同步 ask")

    redacted_question, _ = redact(question)
    run = AnswerRun(
        state="CREATED",
        organization_id=principal.organization_id,
        requested_by=principal.user_id,
        question_sha256=_digest(redacted_question) or "",
        evidence=[],
        config={
            "limit": limit,
            "max_new_tokens": max_new_tokens,
            "model": model,
            # 会话 id 进配置快照：复核时要能看出「这次是追问、属于哪个会话」（§9.4 要求配置快照）
            "session_id": str(session_id) if session_id is not None else None,
        },
        published=False,
        review_required=False,
    )
    session.add(run)
    await session.flush()

    await cache.put(question_key(run.id), redacted_question)

    job = Job(
        job_type=QUESTION_JOB,
        # **只放 run id 与参数，不放问题**——问题在缓存里（见 `question_key` 的注释）
        payload={
            "run_id": str(run.id),
            "limit": limit,
            "max_new_tokens": max_new_tokens,
            "model": model,
            "session_id": str(session_id) if session_id is not None else None,
        },
        idempotency_key=f"{run.id}:{QUESTION_JOB}:{ANSWER_CONFIG_VERSION}",
        status="pending",
        attempt_count=0,
        max_attempts=3,
    )
    session.add(job)
    await session.flush()
    return run, job


async def read_question(cache: AnswerCache, run_id) -> str | None:
    return await cache.get(question_key(run_id))


async def store_answer(cache: AnswerCache, run_id, text: str) -> bool:
    """把**已脱敏**的答案写进短期缓存。调用方负责先脱敏（见 `workers.handlers`）。"""
    return await cache.put(cache_key(run_id), text)


async def read_answer(cache: AnswerCache, run_id) -> str | None:
    """取回当时的答案。**取不到就是取不到**——TTL 到期、没配缓存、或本来就失败，一律返回 None，
    由调用方如实显示「内容不可用」，而不是拿别的东西冒充。"""
    return await cache.get(cache_key(run_id))


async def has_in_flight_job(session: AsyncSession, run_id) -> bool:
    """这个运行有没有**在飞**的任务（pending / running / retry_wait）。

    **任务表才是「在飞」的事实来源**：运行状态在提交后到 worker 领取前有一小段窗口，那期间
    状态还停在原处（`CLARIFYING`）却已经有任务在跑了。两处都要用这个判据：
    `submit_clarification` 用它挡连点，SSE 用它区分「真在等用户」与「刚续跑、马上就有进展」。
    """
    count = await session.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.payload["run_id"].astext == str(run_id),
            Job.status.in_(("pending", "running", "retry_wait")),
        )
    )
    return bool(count)


async def submit_clarification(
    session: AsyncSession,
    principal: Principal,
    run_id,
    supplement: str,
    *,
    cache: AnswerCache,
) -> tuple[AnswerRun, Job]:
    """回答澄清问题、**继续同一个运行**（§9.1 的 `CLARIFYING → RETRIEVING`）。

    「原问题 + 补充」合并后**覆盖缓存里的那一份**——worker 只读 `question_key`，写回去就等于把
    这次运行的问题补全了。运行记录里仍然只有哈希（§21「客户数据不落库」）。

    ⚠️ **同一运行只允许一份补充在飞**：提交后运行仍停在 `CLARIFYING`（等 worker 去推进），
    所以「状态检查」挡不住连点两次。真正的防线是查 `jobs` 里有没有 pending/running/retry_wait 的
    同 run 任务——**任务表才是「在飞」的事实来源**。
    """
    if not cache.available:
        raise CacheUnavailable("未配置短期缓存（CACHE_URL），无法提交补充")

    run = await session.get(AnswerRun, run_id)
    if run is None or run.organization_id != principal.organization_id:
        raise LookupError("运行记录不存在")
    if run.state != "CLARIFYING":
        raise ValueError(f"运行不在待澄清状态（当前 {run.state}）")

    if await has_in_flight_job(session, run.id):
        raise ValueError("上一次补充还在处理中")

    original = await read_question(cache, run.id)
    if original is None:
        raise LookupError("原问题已随短期缓存过期，无法继续")

    merged, _ = redact(f"{original}\n补充：{supplement}")
    await cache.put(question_key(run.id), merged)

    sent = await session.scalar(
        select(func.count()).select_from(Job).where(Job.payload["run_id"].astext == str(run.id))
    )
    job = Job(
        job_type=QUESTION_JOB,
        payload={
            "run_id": str(run.id),
            "limit": run.config.get("limit", 5),
            "max_new_tokens": run.config.get("max_new_tokens", 512),
            "model": run.config.get("model"),
        },
        idempotency_key=f"{run.id}:{QUESTION_JOB}:{ANSWER_CONFIG_VERSION}:{sent}",
        status="pending",
        attempt_count=0,
        max_attempts=3,
    )
    session.add(job)
    await session.flush()
    return run, job
