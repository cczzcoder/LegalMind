"""最小可用的证据约束问答（设计 §9、§8.2；P6 的第一刀）。

**这不是 P6 的全部**，别当成做完了：没有 LangGraph 编排、没有引用校验、没有语义核验、没有
风险分流与拒答策略、没有异步 answer-run。它只把一条链路打通——**检索 → 拼证据 → 本地模型
生成 → 带引用返回**——用来验证「本地模型这条路走得通」，以及量一下本机的实际速度。

**证据约束怎么做的**：

- 提示里**只放检索到的条文原文**，模型看不到的东西不许说；
- 返回的 ``citations`` 是**检索命中的条款版本**，不是模型写出来的条号——所以引用天然可核验；
- 检索**一条都没命中**时不调用模型，直接返回「依据不足」（§9.5「本地模型不满足质量标准时提供
  证据检索与人工审核，不开放正式自动结论」）。

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


async def answer_question(
    session: AsyncSession,
    principal: Principal,
    question: str,
    *,
    limit: int = 5,
    max_new_tokens: int = 512,
    model: str | None = None,
) -> Answer:
    """检索 → 生成 → 带引用返回。**检索为空就拒答，不调用模型。**"""
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
        )

    messages = [
        {
            "role": "user",
            "content": f"{INSTRUCTIONS}\n\n条文原文：\n\n{build_evidence(hits)}\n\n问题：{question}",
        },
    ]
    text = generation.generate(model_name, messages, max_new_tokens=max_new_tokens)
    return Answer(
        question=question,
        answer=text,
        citations=citations,
        path=search.path,
        model=model_name,
        seconds=perf_counter() - started,
    )
