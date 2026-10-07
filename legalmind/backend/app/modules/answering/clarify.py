"""§9.1 的澄清分支（`CLARIFYING`）：问题太笼统时**先问清楚**，而不是硬答。

FR-06 的流程是「1. 提取问题中的事实及条件 2. **识别缺失信息** 3. **必要时**提出澄清问题」，
且发生在**检索之前**（§9.1 的 `CREATED → CLARIFYING / RETRIEVING`）。

**触发条件是确定性的，不交给模型判断。** 我先试过让 7B 抽「主体/行为/标的/时间」再判缺什么，
**不可用**：8 条探针里 2 个误报、1 个漏报——「用人单位无故不缴纳社会保险费，会被怎么处理？」
这种很具体的问题被判「缺主体/标的/时间」，而「这个算不算？」反而判「不缺」。
这与 §9.3 第二层那次是同一个教训（见《技术决策与踩坑记录》§5.12）。

改用**改写层已有的信号**（`retrieval.rewrite.strip_frames`，V1.13）：它把疑问与口语框架词剥掉、
只留实词。于是：

| 问题 | 剥离后 | 判定 |
| --- | --- | --- |
| 「怎么办」「怎么处理」 | **空串** | 澄清——**它本来就是空的** |
| 「这个算不算？」 | 「这个」 | 澄清（只剩代词） |
| 「工资怎么办？」 | 「工资」 | 澄清（没说工资的**什么**） |
| 「拖欠工资」 | 「拖欠工资」 | 检索（够具体） |

判据：**剥离后剩下的实质内容不足 `MIN_SUBSTANCE_CHARS` 字 → 澄清。**

⚠️ **这是一条规则，不是语义防线**：`_FRAMES`（疑问框架词表）与下面的空泛词表都是**人工维护**的，
「我想问一下那个事情」剥完还剩 12 字就绕过去了。所以它的后果定得很轻——**只是多问一句，不是拒答**；
判错了用户回一句就能继续（`POST /answers/{id}/clarify`）。
"""

import json
import urllib.error
from dataclasses import dataclass

from pydantic import BaseModel, Field, ValidationError

from app.modules.retrieval.rewrite import rewrite

#: 剥离疑问框架后剩下的「实质内容」至少要有这么多字，否则认为问题太笼统。
MIN_SUBSTANCE_CHARS = 3

#: 只剩这些词也算**没有实质内容**（代词与空泛名词）。**人工维护，必然不完整。**
EMPTY_WORDS = (
    "这个",
    "那个",
    "这些",
    "那些",
    "它",
    "它们",
    "事情",
    "东西",
    "问题",
    "情况",
    "啥",
    "什么",
    "咋办",
)

INSTRUCTIONS = (
    "用户的问题太笼统，无法判断该查哪条法律。请提出**一个**澄清问题，帮用户补上关键信息。\n"
    "只输出 JSON：\n"
    '{"question":"要问用户的话","options":["可能的理解一","可能的理解二"]}\n'
    "要求：\n"
    "- question **一句话**，直接问缺什么（例如「你说的工资问题，是指哪一方面？」）。\n"
    "- options 给 2–4 个**常见的可能理解**，让用户可以直接挑一个；不确定就留空数组。\n"
    "- **不要回答问题本身**，也不要猜用户想问什么——你的任务只是问清楚。\n"
    "- 不要提「法律要素」「主体」「标的」这类术语，用日常说法。"
)

#: 澄清问题的输出结构。给 Ollama 走**约束解码**（只写 `format: "json"` 挡不住残缺 JSON，
#: 见《技术决策与踩坑记录》§5.11）。
CLARIFY_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["question", "options"],
}


@dataclass(frozen=True)
class Clarification:
    """一次澄清请求。`options` 给前端做候选按钮，`asked_by` 说明问题是谁提的。"""

    question: str
    options: tuple[str, ...]
    asked_by: str  # "model" | "template"

    def render(self) -> str:
        """给人看的正文（CLI 与 API 都用它）。"""
        lines = [self.question]
        if self.options:
            lines.append("")
            lines.extend(f"{index}. {option}" for index, option in enumerate(self.options, start=1))
        return "\n".join(lines)


class _ClarifyOut(BaseModel):
    question: str = ""
    options: list[str] = Field(default_factory=list)


#: 模型不可用时的兜底问法。**澄清不是结论**，所以用模板问是合理降级，不算「编内容」。
TEMPLATE = Clarification(
    question=(
        "这个问题还需要一点信息才能查。请补充：你想问的是**哪部法律或哪个行为**，"
        "以及**针对什么情形**？（例如「用人单位拖欠工资该怎么办」）"
    ),
    options=(),
    asked_by="template",
)


def substance(question: str) -> str:
    """剥离疑问框架与空泛词后剩下的**实质内容**。"""
    stripped = rewrite(question).stripped
    for word in EMPTY_WORDS:
        stripped = stripped.replace(word, " ")
    return "".join(stripped.split())


def needs_clarification(question: str) -> str | None:
    """问题是否笼统到没法检索。**是则返回命中的理由**，否则 None。"""
    text = substance(question)
    if len(text) >= MIN_SUBSTANCE_CHARS:
        return None
    stripped = rewrite(question).stripped
    detail = f"剥离疑问框架后只剩「{stripped}」" if stripped else "剥离疑问框架后是空的"
    return f"{detail}，实质内容不足 {MIN_SUBSTANCE_CHARS} 字"


def build_clarify_messages(question: str) -> list[dict]:
    """拼出生成澄清问题的对话消息（指令放用户消息里，见 `evidence.INSTRUCTIONS` 的注释）。"""
    return [
        {"role": "user", "content": f"{INSTRUCTIONS}\n\n用户的问题：{question}"},
    ]


def parse_clarification(raw: str, *, asked_by: str = "model") -> Clarification | None:
    """解析模型输出；解析不出来返回 None（由调用方退回模板）。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    try:
        payload = json.loads(text.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        parsed = _ClarifyOut.model_validate(payload)
    except ValidationError:
        return None
    if not parsed.question.strip():
        return None
    return Clarification(
        question=parsed.question.strip(),
        options=tuple(option.strip() for option in parsed.options if option.strip()),
        asked_by=asked_by,
    )


def clarifying_question(question: str, generate) -> Clarification:
    """让模型提出澄清问题；**模型不可用或输出不可解析就退回模板**。

    `generate` 是 `adapters.generation.generate` 的注入点（便于测试与复用）。
    """
    try:
        raw = generate(build_clarify_messages(question), schema=CLARIFY_SCHEMA)
    # 模型不可用（§9.5 不回落外部服务）、或输出解析不出来——**如实降级成模板问法**。
    # 澄清不是结论，用模板问是合理降级，不算「编内容」。
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return TEMPLATE
    parsed = parse_clarification(raw)
    return parsed if parsed is not None else TEMPLATE
