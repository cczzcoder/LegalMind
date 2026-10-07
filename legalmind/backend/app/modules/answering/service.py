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

from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from time import perf_counter
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import generation
from app.core.config import get_settings
from app.core.security import Principal
from app.modules.answering import claims
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

# ⚠️ 这里**按名字导入**，不要写成 `from ... import verification`——`Answer` 有个同名字段
# `verification`，而注解在类体里是先赋值后求值，模块名会被字段值（None）盖掉。
from app.modules.answering.verification import VerificationResult, verify_claims
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
    #: 哪道门禁拦下了正式结论：``"scope"`` / ``"verification"`` / ``"format"``；None 表示没被拦。
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


async def answer_question(
    session: AsyncSession,
    principal: Principal,
    question: str,
    *,
    limit: int = 5,
    max_new_tokens: int = 512,
    model: str | None = None,
    record: bool = True,
) -> Answer:
    """检索 → 生成 → 核验 → 带引用返回，**并把这次运行记下来**（§9.1、§9.4）。

    `record=False` 只给「不想留痕」的调用方（目前只有性能探测用），默认都记。
    """
    run = RunTracker()
    answer = await _answer_pipeline(
        session, principal, question, run, limit=limit, max_new_tokens=max_new_tokens, model=model
    )
    if record:
        await _record(session, principal, question, answer, run, limit, max_new_tokens)
    return answer


async def _record(
    session: AsyncSession,
    principal: Principal,
    question: str,
    answer: Answer,
    run: RunTracker,
    limit: int,
    max_new_tokens: int,
) -> None:
    """落库这次运行。**只记元数据、证据引用与配置快照**，问题与回答正文只留 sha256（§21）。"""
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
    # 调用方可能已经开了事务（测试与未来的 API 层），两种都支持
    if session.in_transaction():
        await record_run(
            session,
            principal=principal,
            question=question,
            answer=answer,
            tracker=run,
            config=config,
        )
    else:
        async with session.begin():
            await record_run(
                session,
                principal=principal,
                question=question,
                answer=answer,
                tracker=run,
                config=config,
            )


async def _answer_pipeline(
    session: AsyncSession,
    principal: Principal,
    question: str,
    run: RunTracker,
    *,
    limit: int,
    max_new_tokens: int,
    model: str | None,
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
    run.to("RETRIEVING")
    # 复用 P4 的检索：`semantic` 走级联（先关键词、命中为空才向量），并做授权复核与未生效屏蔽
    search = await search_provisions(
        session, principal, SearchQuery(semantic=question, limit=limit)
    )
    hits = list(search.hits)
    if not hits:
        run.to("INSUFFICIENT_EVIDENCE")
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
        run.to("NEEDS_REVIEW")
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
        run.to("NEEDS_REVIEW")
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
        run.to("FAILED")
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
    run.to("ASSEMBLING_EVIDENCE")
    assembly = assemble_evidence(
        hits,
        question,
        instructions_chars=len(INSTRUCTIONS),
        max_new_tokens=max_new_tokens,
        context_tokens=get_settings().generation_context_tokens,
    )
    if not assembly.included:
        # 一条都装不下：不生成结论，只把条文与提示交回人工（§9.4「限制回答范围或转人工」）
        run.to("NEEDS_REVIEW")
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

    run.to("GENERATING")
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
        run.to("NEEDS_REVIEW")
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
    run.to("VERIFYING")
    result = verify_claims(parsed.answer, question, assembly.included)
    if not result.ok:
        # §9.3 第一层没过：不当正式答案发布，但草稿留着给人工（§9.4「正式答案通过门禁后发送」）
        run.to("NEEDS_REVIEW")
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
    run.to("PARTIAL" if assembly.notice else "ANSWERED")
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
