"""最小可用的证据约束问答（设计 §9、§8.2；P6 的第一刀）。

**这不是 P6 的全部**，别当成做完了：没有 LangGraph 编排、没有引用校验、没有语义核验、没有
风险分流与拒答策略、没有异步 answer-run。它只把一条链路打通——**检索 → 拼证据 → 本地模型
生成 → 带引用返回**——用来验证「本地模型这条路走得通」，以及量一下本机的实际速度。

**证据约束怎么做的**：

- 提示里**只放检索到的条文原文**，模型看不到的东西不许说；
- 返回的 ``citations`` 是**检索命中的条款版本**，不是模型写出来的条号——所以引用天然可核验；
- 检索**一条都没命中**时不调用模型，直接返回「依据不足」（§9.5「本地模型不满足质量标准时提供
  证据检索与人工审核，不开放正式自动结论」）；
- **效力状态由回答层强制**，不问模型：依据不是现行有效版本就带提示，全部不是就**直接拒答**
  （§8.3、§9.5）。实测模型自己不会理会这个标注，所以这条不能交给提示词。

**只用本地模型**：§9.5 的决策锁定本地部署、默认关闭外部 API，所以这里没有外部回落路径。
"""

from dataclasses import dataclass
from time import perf_counter
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import generation
from app.core.config import get_settings
from app.core.security import Principal
from app.modules.retrieval.schemas import SearchQuery
from app.modules.retrieval.service import search_provisions

# ⚠️ **指令放在用户消息里，不放 system 角色**——这是实测出来的，不是风格问题。
#
# 同一份证据、同一个问题，用 system 角色给指令时 Qwen2.5-1.5B 一律回「依据不足」（它抓住了
# 那句退路照抄），并进用户消息后立刻答对。小模型对 system 角色的指令跟随很脆，把拒绝条款单独
# 放进 system 尤其容易触发。**换更大的模型时也建议保持这个形态**，除非重新评测过。
INSTRUCTIONS = (
    "请只依据下面给出的条文原文回答问题，不要使用条文以外的知识，并在结论后标注依据的条号"
    "（格式如：《中华人民共和国监狱法》第五十条）。\n"
    "只陈述条文写了什么，不要给出法律意见或推测。\n"
    "条文后的括号里是效力状态；其中「施行日期未知」只表示没提取到施行日期，"
    "**不影响你依据条文内容作答**。\n"
    "只有当条文确实与问题无关时，才回答「依据不足」，并说明还缺什么。"
)

# 效力状态要**翻成中文再给模型**：直接把 ``unknown`` 这个英文枚举值塞进提示，实测会让小模型
# 读成「这条不可靠」然后拒答（同一份证据、同一个问题，去掉括号就答对了）。
# 设计 §8.3 要求把效力状态回传给**回答层**（由它决定能不能下结论），不等于要把原始枚举喂给模型。
STATUS_LABELS = {
    "effective": "现行有效",
    "not_yet_effective": "尚未生效，不得作为现行依据",
    "repealed": "已被取代",
    "unknown": "施行日期未知",
}

NO_EVIDENCE = "依据不足：没有检索到与问题相关的条文，无法回答。"
# §9.5「本地模型不满足质量标准时提供证据检索与人工审核，不开放正式自动结论」。
# **不回落外部服务**——配置缺失就是不可用。
MODEL_UNAVAILABLE = "本地生成模型不可用，未生成结论。以下是检索到的依据，请人工判读。"

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
    #: 依据的效力状态提示（回答层**确定性**算出，不依赖模型）。没有非现行有效依据时为 None。
    status_notice: str | None = None


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


def build_evidence(hits) -> str:
    """把检索命中拼成证据块。**只放条文原文与出处**，不放别的东西。"""
    blocks = []
    for index, hit in enumerate(hits, start=1):
        status = STATUS_LABELS.get(hit.legal_status, hit.legal_status)
        blocks.append(
            f"[{index}] {hit.instrument_title} {hit.provision_display or hit.provision_number}"
            f"（{status}）\n{hit.text}"
        )
    return "\n\n".join(blocks)


def build_messages(hits, question: str) -> list[dict]:
    """拼出送进本地模型的对话消息。

    **单独抽出来是为了让评测与线上用同一条提示词**——生成质量评测
    （`scripts/evaluate_answering.py`，§9.5）要测的就是这条提示词加这个模型，
    如果评测另写一份提示，测出来的东西就不是线上跑的东西了。
    """
    return [
        {
            "role": "user",
            "content": f"{INSTRUCTIONS}\n\n条文原文：\n\n{build_evidence(hits)}\n\n问题：{question}",
        },
    ]


async def answer_question(
    session: AsyncSession,
    principal: Principal,
    question: str,
    *,
    limit: int = 5,
    max_new_tokens: int = 512,
    model: str | None = None,
) -> Answer:
    """检索 → 生成 → 带引用返回。**检索为空就拒答，不调用模型。**

    **效力状态由这一层强制，不问模型**（§8.3「检索结果应携带效力状态……供回答层与人工核验判断
    依据是否现行有效」）：只要有依据不是现行有效版本，答案就带上 ``status_notice``；**若全部
    依据都不是现行有效，直接拒答、不生成结论**（§9.5「不开放正式自动结论」）。
    实测模型自己不会理会这个标注（见 ``NON_CURRENT_NOTICE`` 的注释），所以不能靠提示词。
    """
    started = perf_counter()
    # 复用 P4 的检索：`semantic` 走级联（先关键词、命中为空才向量），并做授权复核与未生效屏蔽
    search = await search_provisions(
        session, principal, SearchQuery(semantic=question, limit=limit)
    )
    hits = list(search.hits)
    if not hits:
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
    if not generation.available(model_name):
        # **不回落外部服务**：本地模型不可用就如实降级成「只给证据 + 转人工」（§9.5）
        return Answer(
            question=question,
            answer=MODEL_UNAVAILABLE,
            citations=citations,
            path=search.path,
            model=None,
            seconds=perf_counter() - started,
            status_notice=notice,
        )

    messages = build_messages(hits, question)
    text = generation.generate(model_name, messages, max_new_tokens=max_new_tokens)
    return Answer(
        question=question,
        answer=text,
        citations=citations,
        path=search.path,
        model=model_name,
        seconds=perf_counter() - started,
        status_notice=notice,
    )
