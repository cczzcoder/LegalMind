"""最小可用的证据约束问答（设计 §9、§8.2；P6 的第一刀）。

**这不是 P6 的全部**，别当成做完了：没有 LangGraph 编排、没有语义核验（§9.3 第二层）、没有
风险分流、没有异步 answer-run。它打通的是——**检索 → 拼证据 → 本地模型生成 → 核验 → 带引用返回**。

**证据约束怎么做的**：

- 提示里**只放检索到的条文原文**（`evidence.build_messages`），模型看不到的东西不许说；
- 返回的 ``citations`` 是**检索命中的条款版本**，不是模型写出来的条号——所以引用天然可核验；
- 检索**一条都没命中**时不调用模型，直接返回「依据不足」（§9.5）；
- **效力状态由回答层强制**，不问模型（§8.3）：依据不是现行有效版本就带提示，全部不是就**直接拒答**；
- **模型写出来的依据要过确定性核验**（§9.3 第一层，`verification.verify`）：答案里的法律名、
  条号、数值、引文必须落在本次证据里；**没过就不当正式答案发布**，只把草稿交人工判读；
- **证据按上下文预算装配**（§8.2 第 9 步、§9.4，`assembly.assemble_evidence`）：检索只限条数
  不限长度，装不下时推理引擎会**静默从前面截断**（先吃掉指令），所以自己算预算；装不下就
  **如实限定回答范围**，一条都装不下就转人工；
- **问的是本人情形就不给个人结论**（§20.2、§9.3 第三层，`scope.scope_notice`）：本工具是法律
  信息辅助工具，不提供法律服务——实测模型会在「我能不能领」这类问题上补一句「因此你可以领取」，
  那一步「你能领」就是越界。命中即不生成结论、只给条文与下一步；
- **每个输出都带「不构成法律意见」声明与生成时间**（§20.2）：做成 `Answer` 的**字段**而不是
  调用方的自觉，结构上发布不出一份不带声明的结论。

后四条都**不能交给提示词**——实测模型自己不会理会效力状态标注，也会在个性化问题上越界；
安全属性要由回答层确定性兜住。

**只用本地模型**：§9.5 的决策锁定本地部署、默认关闭外部 API，所以这里没有外部回落路径。
"""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from hashlib import sha256
from time import perf_counter
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import generation
from app.adapters.cache import cache_key
from app.core.config import get_settings
from app.core.security import Principal
from app.modules.answering import claims, clarify, runs
from app.modules.answering.assembly import assemble_evidence
from app.modules.answering.evidence import (
    INSTRUCTIONS,
    STATUS_LABELS,
    build_evidence,
    build_messages,
    evidence_display,
)
from app.modules.answering.runs import RunTracker, record_run
from app.modules.answering.scope import DISCLAIMER, scope_notice

# ⚠️ **按名字导入，不要导入模块本身**：本文件的第一个形参就叫 `session`（`AsyncSession`），
# 导入一个叫 `session` 的模块会被参数名遮住。
from app.modules.answering.session import compose_query, read_turns, record_turn

# ⚠️ 这里**按名字导入**，不要写成 `from ... import verification`——`Answer` 有个同名字段
# `verification`，而注解在类体里是先赋值后求值，模块名会被字段值（None）盖掉。
from app.modules.answering.verification import VerificationResult, verify_claims
from app.modules.redaction.service import redact
from app.modules.retrieval.schemas import SearchQuery
from app.modules.retrieval.service import search_provisions

__all__ = [
    "DISCLAIMER",
    "INSTRUCTIONS",
    "STATUS_LABELS",
    "Answer",
    "Citation",
    "answer_question",
    "build_evidence",
    "build_messages",
    "non_current_citations",
    "status_notice_for",
]

NO_EVIDENCE = "依据不足：没有检索到与问题相关的条文，无法回答。"
# §9.5「本地模型不满足质量标准时提供证据检索与人工审核，不开放正式自动结论」。
# **不回落外部服务**——配置缺失就是不可用。
MODEL_UNAVAILABLE = "本地生成模型不可用，未生成结论。以下是检索到的依据，请人工判读。"
# §9.3 第一层没过：**不当正式答案发布**，但草稿要留着给人工看（第三层人工审核需要它）。
VERIFICATION_FAILED_NOTICE = (
    "结论未通过核验，不作为正式答案发布（设计 §9.3 第一层）。核验发现：{issues}。"
    "以下是模型生成但未通过核验的草稿，仅供参考，请人工判读。"
)
# §9.2 要求模型只输出结构化主张与证据 ID；模型没照做时**不当正式答案发布**。
STRUCTURE_FAILED_NOTICE = (
    "模型没有按约定输出结构化结论（{reason}），未作为正式答案发布（设计 §9.2）。"
    "以下是模型的原始输出，请人工判读。"
)

#: 不能作为「现行依据」的效力状态（§8.3）。
#:
#: ⚠️ ``unknown`` **不算**——它只表示没提取到施行日期，不等于失效（提示词里也是这么告诉模型的：
#: 「施行日期未知只表示没提取到施行日期，不影响你依据条文内容作答」）。把它算进来会把大量
#: 正常条文判成不可用。
NON_CURRENT_STATUSES = frozenset({"repealed", "not_yet_effective"})

#: 全部依据都不是现行有效时的**确定性拒答**（§9.5「提供证据检索与人工审核，不开放正式自动结论」）。
#:
#: **为什么由回答层强制、而不是写进提示词让模型自己说**：实测（§9.5 的生成质量评测，22 条用例）
#: 证据块里明写「（尚未生效，不得作为现行依据）」，模型**照样把未生效的法律当现行依据陈述**——
#: `status_flag_rate` 为 0。安全属性不能交给模型自觉，这是本系统「授权、证据绑定、审计由本系统
#: 实现，不依赖框架与模型隐式行为」的一贯口径。
NON_CURRENT_NOTICE = (
    "依据提示：本次检索到的条文均不是现行有效版本（{detail}），不能作为现行依据下结论。"
    "以下仅列出检索到的条文，请人工判读，或改查现行版本。"
)


@dataclass(frozen=True)
class Citation:
    """一条可核验的依据——来自**检索结果**，不是模型写的。"""

    instrument_title: str
    provision_number: str
    provision_display: str
    legal_status: str
    provision_version_id: UUID


@dataclass(frozen=True)
class Answer:
    question: str
    answer: str
    citations: tuple[Citation, ...]
    path: str
    model: str | None
    seconds: float
    # ---- 回答层**确定性**算出来的各种提示，都不是模型写的 ----
    #: 依据的效力状态提示（§8.3）。没有非现行有效依据时为 None。
    status_notice: str | None = None
    #: 证据装配的**范围**提示（§8.2 第 9 步、§9.4）：装不下全部依据时说明只覆盖了哪几条。
    evidence_notice: str | None = None
    #: 提问范围的边界说明（§20.2、§9.3 第三层）：问题问的是本人情形时**不给个人结论**。
    scope_notice: str | None = None
    #: 澄清请求（§9.1 `CLARIFYING`）：问题太笼统时**反问**，等用户补充后继续同一个运行。
    clarifying_question: str | None = None
    #: 这次运行在 `answer_runs` 里的 id（`record=False` 或还没落库时为 None）。
    #: 调用方要靠它才能续跑澄清（`clarify-run --run <id>`）。
    run_id: UUID | None = None
    #: 这次运行属于哪个会话（§9.6 多轮追问）。单轮问答为 None。
    session_id: UUID | None = None
    #: **补全后的自足问题**（§9.6）：`question` 是用户实际敲的那句（可能是「那如果是这样呢？」），
    #: 这个字段是**拼上上文之后真正交给检索与模型的那句**。两者相同时为 None。
    #: 分开记是为了**可追溯**——否则事后看 `question` 会以为系统答非所问。
    resolved_question: str | None = None
    #: §20.2 要求每个正式输出都带的「不构成法律意见」声明。**恒有**，不是可选项。
    disclaimer: str = DISCLAIMER
    #: §20.2 要求标注的生成时间（ISO 8601，UTC）。
    generated_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    # ---- 门禁 ----
    #: §9.3 第一层的核验结果。没走到生成那一步时为 None。
    verification: VerificationResult | None = None
    #: 核验未通过时，模型生成的原始草稿（供人工判读）。
    draft: str | None = None
    #: 哪道门禁/环节拦下了正式结论：``"scope"`` / ``"verification"`` / ``"format"`` /
    #: ``"clarifying"``（在等用户补充，§9.1）；None 表示没被拦。
    blocked_by: str | None = None
    #: 本次结论**实际引用**的证据编号（服务端从结构化主张里取，§9.2）。拒答或未生成时为空。
    cited_evidence_ids: tuple[str, ...] = ()
    #: 模型输出需要**容错修复**才能解析（见 `claims.parse`）。用来观察模型的规矩程度。
    repaired_output: bool = False

    @property
    def published(self) -> bool:
        """这份结论能不能当**正式答案**用。

        门禁拦下过就不算——调用方（CLI / 未来的 API）应当据此决定怎么展示。
        """
        return self.model is not None and self.blocked_by is None


def non_current_citations(citations: tuple[Citation, ...]) -> tuple[Citation, ...]:
    """挑出不能作为现行依据的那几条（§8.3）。"""
    return tuple(item for item in citations if item.legal_status in NON_CURRENT_STATUSES)


def status_notice_for(citations: tuple[Citation, ...]) -> str | None:
    """依据里有非现行有效的版本时给出一句提示；都没有则返回 None。"""
    flagged = non_current_citations(citations)
    if not flagged:
        return None
    detail = "；".join(
        f"{item.instrument_title}{item.provision_display}"
        f"（{STATUS_LABELS.get(item.legal_status, item.legal_status)}）"
        for item in flagged
    )
    return NON_CURRENT_NOTICE.format(detail=detail)


async def _advance(run: RunTracker, target: str, on_state) -> None:
    """转移状态，并把新状态报给观察者。

    异步运行靠它把进度写进运行记录——**§9.4「SSE 只在核验前发送进度状态」**要有东西可发，
    就得在转移发生的那一刻记下来，而不是等整条链路跑完。
    """
    run.to(target)
    if on_state is not None:
        await on_state(target)


async def answer_question(
    session: AsyncSession,
    principal: Principal,
    question: str,
    *,
    session_id: UUID | None = None,
    limit: int = 5,
    max_new_tokens: int = 512,
    model: str | None = None,
    record: bool = True,
    on_state=None,
    run_row=None,
    cache=None,
) -> Answer:
    """检索 → 生成 → 核验 → 带引用返回，**并把这次运行记下来**（§9.1、§9.4）。

    `record=False` 只给「不想留痕」的调用方（目前只有性能探测用），默认都记。

    `session_id` 非空时走**多轮追问**（§9.6）：先把追问补全成自足的问题再进链路。
    **只有「问题」参与补全，历史答案不参与**——答案里的结论进了提示词就会变成模型的「依据」，
    而它既没经过本轮的授权复核、也没绑定条款版本（§9.6 边界 1）。
    """
    # §9.6：补全只影响「检索与提示用哪句问题」，链路本身仍是单轮的、门禁一条不少
    resolved = question
    if session_id is not None:
        previous = await read_turns(cache, session_id, principal)
        resolved = compose_query(previous, question)

    run = RunTracker()
    answer = await _answer_pipeline(
        session,
        principal,
        resolved,
        run,
        limit=limit,
        max_new_tokens=max_new_tokens,
        model=model,
        on_state=on_state,
    )
    # `Answer` 是冻结的：把「用户实际敲的那句」与「实际处理的那句」分开记，否则事后看
    # `question` 会以为系统答非所问（见字段注释）
    answer = replace(
        answer,
        question=question,
        session_id=session_id,
        resolved_question=resolved if resolved != question else None,
    )
    if record:
        # 落库记**实际处理的那句**（`resolved`）——运行记录要描述这次运行真正跑了什么
        recorded = await _record(
            session, principal, resolved, answer, run, limit, max_new_tokens, run_row
        )
        if recorded is not None:
            # `Answer` 是冻结的，落库后才知道 run id——用 replace 补上，别为了这个把落库提前
            answer = replace(answer, run_id=recorded)
            await _cache_clarification(cache, recorded, question, answer)
            if session_id is not None:
                # 会话里记**用户实际敲的那句**（脱敏后）——它是下一轮要拼接的上文
                await record_turn(cache, session_id, principal, question, answer, recorded)
    return answer


async def _cache_clarification(cache, run_id, question: str, answer: Answer) -> None:
    """澄清要**能续跑**：把脱敏后的原问题与澄清请求放进短期缓存。

    异步路径本来就会把问题写进缓存（worker 要读）；**同步路径不会**，于是「同步 ask 问了澄清、
    却没法接着答」——实测踩到（`test_continuation_merges_the_supplement_and_finishes` 暴露）。
    所以这里补上，键与异步路径**完全一致**（`runs.question_key` / `cache.cache_key`）。

    **只有澄清才写**：正式结论不进缓存（§21「客户数据不落库」），异步路径写缓存是为了复核。
    """
    if cache is None or not cache.available or not answer.clarifying_question:
        return
    await cache.put(runs.question_key(run_id), redact(question)[0])
    await cache.put(cache_key(run_id), redact(answer.answer)[0])


async def _record(
    session: AsyncSession,
    principal: Principal,
    question: str,
    answer: Answer,
    run: RunTracker,
    limit: int,
    max_new_tokens: int,
    run_row=None,
) -> UUID | None:
    """落库这次运行，返回运行记录 id（拿不到就 None）。**只记元数据、证据引用与配置快照**，问题与回答正文只留 sha256（§21）。"""
    settings = get_settings()
    config = {
        "model": answer.model or settings.generation_model,
        "retrieval_path": answer.path,
        "limit": limit,
        "max_new_tokens": max_new_tokens,
        "context_tokens": settings.generation_context_tokens,
        "embedding_model": settings.embedding_model,
        # 提示词没有版本号，用哈希代替——复核时要能判断「是不是换了提示词之后才变成这样」
        "instructions_sha256": sha256(INSTRUCTIONS.encode("utf-8")).hexdigest(),
    }
    # 调用方可能已经开了事务（测试与未来的 API 层），两种都支持。
    # ⚠️ **两个分支都要把 `run=run_row` 传下去**：异步路径在 `submit_question` 时已经建好了行，
    # 漏传就会**再建一行**（同一次运行留两条记录，V1.26 踩过）。
    if session.in_transaction():
        recorded = await record_run(
            session,
            principal=principal,
            question=question,
            answer=answer,
            tracker=run,
            config=config,
            run=run_row,
        )
    else:
        async with session.begin():
            recorded = await record_run(
                session,
                principal=principal,
                question=question,
                answer=answer,
                tracker=run,
                config=config,
                run=run_row,
            )
    return recorded.id


async def _answer_pipeline(
    session: AsyncSession,
    principal: Principal,
    question: str,
    run: RunTracker,
    *,
    limit: int,
    max_new_tokens: int,
    model: str | None,
    on_state=None,
) -> Answer:
    """真正的链路。**每个出口都声明自己停在哪个状态**（§9.1）——状态不是日志，是转移表的一部分。

    **两道确定性门禁都在这一层，都不问模型**：

    - 效力状态（§8.3）：有依据不是现行有效版本就带 ``status_notice``；**全部不是则直接拒答、
      不生成结论**——拿未生效或已被取代的法律下结论，生成出来也没有意义；
    - 依据核验（§9.3 第一层）：模型答案里的法律名、条号、数值、引文必须落在本次证据里；
      **没过就不当正式答案发布**，把草稿留在 ``draft`` 里交人工判读（§9.4「正式答案通过
      门禁后发送」）。
    """
    started = perf_counter()

    # §9.1 的 `CREATED → CLARIFYING / RETRIEVING`：**澄清发生在检索之前**（FR-06 第 2、3 步）。
    # 判据是确定性的（见 `clarify.needs_clarification`），**不问模型**——实测 7B 判不准。
    reason = clarify.needs_clarification(question)
    if reason is not None:
        model_name = model or get_settings().generation_model

        def ask(messages, schema):
            return generation.generate(model_name, messages, schema=schema, max_new_tokens=256)

        # ⚠️ **先把问题生成出来，再进 `CLARIFYING`**：状态是给调用方看的，`CLARIFYING` 应当意味着
        # 「**已经问出去了**」。反过来写的话，生成澄清问题那几十秒里状态已经是 `CLARIFYING`、
        # 而问题还不存在——实测真实 HTTP 上出现过「状态 CLARIFYING、`clarify` 事件里问题为空」
        # 的 35 秒窗口（那 35 秒是模型在加载与生成）。
        asked = clarify.clarifying_question(question, ask)
        await _advance(run, "CLARIFYING", on_state)
        return Answer(
            question=question,
            answer=asked.render(),
            citations=(),
            # 没有走检索，`path` 如实标成 clarify（不是 exact/keyword/vector 里的任何一个）
            path="clarify",
            # 模板问法没有用模型——`model=None` 如实反映这一点
            model=model_name if asked.asked_by == "model" else None,
            seconds=perf_counter() - started,
            blocked_by="clarifying",
            clarifying_question=asked.render(),
        )

    await _advance(run, "RETRIEVING", on_state)
    # 复用 P4 的检索：`semantic` 走级联（先关键词、命中为空才向量），并做授权复核与未生效屏蔽
    search = await search_provisions(
        session, principal, SearchQuery(semantic=question, limit=limit)
    )
    hits = list(search.hits)
    if not hits:
        await _advance(run, "INSUFFICIENT_EVIDENCE", on_state)
        return Answer(
            question=question,
            answer=NO_EVIDENCE,
            citations=(),
            path=search.path,
            model=None,
            seconds=perf_counter() - started,
        )

    citations = tuple(
        Citation(
            instrument_title=hit.instrument_title,
            provision_number=hit.provision_number,
            provision_display=hit.provision_display or hit.provision_number,
            legal_status=hit.legal_status,
            provision_version_id=hit.citation.provision_version_id,
        )
        for hit in hits
    )
    notice = status_notice_for(citations)

    if notice is not None and len(non_current_citations(citations)) == len(citations):
        # 全部依据都不是现行有效：**生成结论没有意义**——拿一部尚未生效或已被取代的法律去
        # 下结论，正是 §8.3 要防的那件事。不调用模型，只把提示与条文交回人工（§9.5）。
        await _advance(run, "NEEDS_REVIEW", on_state)
        return Answer(
            question=question,
            answer=notice,
            citations=citations,
            path=search.path,
            model=None,
            seconds=perf_counter() - started,
            status_notice=notice,
        )

    model_name = model or get_settings().generation_model
    personal = scope_notice(question)
    if personal is not None:
        # §20.2「不提供法律服务、不替代执业律师判断」+ §9.3 第三层「高风险个性化判断」转人工：
        # 问题问的是本人情形，就**不生成个人结论**，只把条文与下一步交回提问者。
        # 实测模型会在这种问题上加一句「因此，作为特困人员，可以领取社会救助」——引用没错、
        # 内容也没错，但那一步「你能领」正是越界的地方，而且它还跳过了「须经认定程序」这个前提。
        await _advance(run, "NEEDS_REVIEW", on_state)
        return Answer(
            question=question,
            answer=personal,
            citations=citations,
            path=search.path,
            model=None,
            seconds=perf_counter() - started,
            status_notice=notice,
            scope_notice=personal,
            blocked_by="scope",
        )

    if not generation.available(model_name):
        # **不回落外部服务**：本地模型不可用就如实降级成「只给证据 + 转人工」（§9.5）
        await _advance(run, "FAILED", on_state)
        return Answer(
            question=question,
            answer=MODEL_UNAVAILABLE,
            citations=citations,
            path=search.path,
            model=None,
            seconds=perf_counter() - started,
            status_notice=notice,
        )

    # 上下文预算（§8.2 第 9 步、§9.4）：检索只限条数不限长度，装不下时推理引擎会**静默从前面
    # 截断**——而指令就在提示最前面，那等于模型先失去全部约束。所以这里自己算、自己如实报告。
    await _advance(run, "ASSEMBLING_EVIDENCE", on_state)
    assembly = assemble_evidence(
        hits,
        question,
        instructions_chars=len(INSTRUCTIONS),
        max_new_tokens=max_new_tokens,
        context_tokens=get_settings().generation_context_tokens,
    )
    if not assembly.included:
        # 一条都装不下：不生成结论，只把条文与提示交回人工（§9.4「限制回答范围或转人工」）
        await _advance(run, "NEEDS_REVIEW", on_state)
        return Answer(
            question=question,
            answer=assembly.notice,
            citations=citations,
            path=search.path,
            model=None,
            seconds=perf_counter() - started,
            status_notice=notice,
            evidence_notice=assembly.notice,
        )

    await _advance(run, "GENERATING", on_state)
    text = generation.generate(
        model_name,
        build_messages(assembly.included, question),
        max_new_tokens=max_new_tokens,
        # **约束解码**：只写 format="json" 挡不住残缺 JSON（见 generation.generate 的注释）
        schema=claims.SCHEMA,
    )
    # §9.2：模型只给「主张 + 证据编号」，引用由服务端从编号渲染——所以先解析，再核验，再渲染
    parsed = claims.parse(text)
    if not parsed.ok:
        # 模型没按约定输出结构化结果：**不当正式答案发布**，原始输出留给人工
        await _advance(run, "NEEDS_REVIEW", on_state)
        return Answer(
            question=question,
            answer=STRUCTURE_FAILED_NOTICE.format(reason=parsed.error),
            citations=citations,
            path=search.path,
            model=model_name,
            seconds=perf_counter() - started,
            status_notice=notice,
            evidence_notice=assembly.notice,
            draft=text,
            blocked_by="format",
        )

    # 核验对着**模型实际看到的那几条**——它没见过的条文不可能被它正确引用
    await _advance(run, "VERIFYING", on_state)
    result = verify_claims(parsed.answer, question, assembly.included)
    if not result.ok:
        # §9.3 第一层没过：不当正式答案发布，但草稿留着给人工（§9.4「正式答案通过门禁后发送」）
        await _advance(run, "NEEDS_REVIEW", on_state)
        return Answer(
            question=question,
            answer=VERIFICATION_FAILED_NOTICE.format(issues=result.summary()),
            citations=citations,
            path=search.path,
            model=model_name,
            seconds=perf_counter() - started,
            status_notice=notice,
            verification=result,
            draft=text,
            evidence_notice=assembly.notice,
            blocked_by="verification",
        )

    # `PARTIAL` = 发布了但**限定了回答范围**（§9.4「证据无法完整装配时，限制回答范围或转人工」）
    await _advance(run, "PARTIAL" if assembly.notice else "ANSWERED", on_state)
    return Answer(
        question=question,
        answer=claims.render(parsed.answer, evidence_display(assembly.included)),
        citations=citations,
        path=search.path,
        model=model_name,
        seconds=perf_counter() - started,
        status_notice=notice,
        verification=result,
        evidence_notice=assembly.notice,
        cited_evidence_ids=parsed.answer.cited_evidence_ids,
        repaired_output=parsed.repaired,
    )
