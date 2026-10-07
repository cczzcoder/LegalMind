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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.models import (
    ANSWER_RUN_TERMINAL_STATES,
    ANSWER_RUN_TRANSITIONS,
    AnswerRun,
)
from app.modules.authorization.grants import record_audit


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


async def record_run(
    session: AsyncSession,
    *,
    principal: Principal,
    question: str,
    answer,
    tracker: RunTracker,
    config: dict,
) -> AnswerRun:
    """把一次运行落库。**调用方负责事务**。"""
    run = AnswerRun(
        state=tracker.state,
        previous_state=tracker.previous,
        organization_id=principal.organization_id,
        requested_by=principal.user_id,
        question_sha256=_digest(question) or "",
        answer_sha256=_digest(answer.answer),
        evidence=evidence_refs(answer.citations, answer.cited_evidence_ids),
        config=config,
        blocked_by=answer.blocked_by,
        published=answer.published,
        seconds=answer.seconds,
        # 待审队列只收 `NEEDS_REVIEW`——`PARTIAL` 是「限制回答范围」，按 §9.4 不必转人工
        review_required=tracker.state == "NEEDS_REVIEW",
    )
    session.add(run)
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
