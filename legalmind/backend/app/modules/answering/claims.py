"""§9.2 的结构化输出：模型只给「主张 + 证据 ID」，引用由服务端生成。

设计 §9.2 要求模型只输出结构化主张和允许的证据 ID，并明确「**服务端负责生成引用标题和访问
入口**，不接受模型自行生成的来源链接作为有效证据」。于是：

- **引用不再是正则从自由文本里猜出来的**——模型回填的是 `build_evidence` 给的编号 `[1]` `[2]`，
  核验就是「查这个 ID 在不在本次证据集里」；
- **结论不再是不可拆的一整段**——每条主张各带自己的依据、前提（`conditions`）与未确认事项
  （`limitations`），§9.3 第二层的语义校验才有逐条判断的抓手；
- **模型不再写「依据：《中华人民共和国劳动法》第五十条」这种句子**，那是服务端从 ID 渲染的。

**⚠️ 容错解析是必需的，不是偷懒**：即使打开 Ollama 的 JSON 约束（`format: json`），实测仍出现过
``[]`` 与 ``{...}`` 两段并列的输出（`json.loads` 报 `Extra data`）。所以 `parse()` 先按整体解析，
失败再逐个抽出**括号配平的 JSON 对象**合并。**合并过的结果会被标出来**（`repaired=True`），
评测据此统计「模型到底有多规矩」——**容错不等于看不见**。
"""

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator


class Claim(BaseModel):
    """一条主张。`evidence_ids` 指向 `build_evidence` 给出的编号。"""

    claim_id: str = ""
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @field_validator("text", mode="before")
    @classmethod
    def _text_must_be_string(cls, value):
        return "" if value is None else str(value)

    @field_validator("evidence_ids", "conditions", "limitations", mode="before")
    @classmethod
    def _to_str_list(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            # 编号可能是数字（1）也可能是字符串（"1"），统一成字符串再比
            return [str(item) for item in value if item is not None]
        return []


class StructuredAnswer(BaseModel):
    """模型的结构化输出。`missing_facts` / `conflicts` 只作展示，不参与核验。"""

    claims: list[Claim] = Field(default_factory=list)
    missing_facts: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)

    @field_validator("missing_facts", "conflicts", mode="before")
    @classmethod
    def _flatten(cls, value):
        """实测模型会写成 `[{"fact_id": "f1", "text": "…"}]`，取其中的 text 即可。"""
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if not isinstance(value, list):
            return []
        out = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("fact") or item.get("description")
                if text:
                    out.append(str(text))
        return out

    @property
    def cited_evidence_ids(self) -> tuple[str, ...]:
        """本次被引用到的证据编号（按首次出现顺序去重）。"""
        seen: dict[str, None] = {}
        for claim in self.claims:
            for item in claim.evidence_ids:
                seen.setdefault(item, None)
        return tuple(seen)

    @property
    def abstained(self) -> bool:
        """**拒答的形态是「一条主张都没有」**（§9.2 的 `missing_facts` 说明还缺什么）。"""
        return not self.claims


@dataclass(frozen=True)
class ParsedAnswer:
    """一次解析的结果。`answer` 为 None 表示连容错都救不回来。"""

    answer: StructuredAnswer | None
    error: str | None = None
    #: 整体 `json.loads` 失败、靠抽取括号配平的对象才救回来的
    repaired: bool = False

    @property
    def ok(self) -> bool:
        return self.answer is not None


def _strip_fence(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def _balanced_objects(text: str) -> list[str]:
    """抽出所有**括号配平**的顶层 JSON 对象（忽略字符串里的括号）。"""
    found: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                found.append(text[start : index + 1])
                start = -1
            elif depth < 0:
                depth = 0
    return found


def _merge(payloads: list[dict]) -> dict:
    """把多段输出合并成一个结构化结果（主张按出现顺序拼接）。"""
    merged: dict[str, Any] = {"claims": [], "missing_facts": [], "conflicts": []}
    for payload in payloads:
        for key in ("claims", "missing_facts", "conflicts"):
            value = payload.get(key)
            if isinstance(value, list):
                merged[key].extend(value)
    return merged


def parse(raw: str) -> ParsedAnswer:
    """把模型输出解析成 `StructuredAnswer`；**容错**，但把容错这件事记下来。"""
    text = _strip_fence(raw)
    if not text:
        return ParsedAnswer(answer=None, error="模型输出为空")

    payloads: list[dict] = []
    repaired = False
    try:
        loaded = json.loads(text)
        payloads = (
            [item for item in loaded if isinstance(item, dict)]
            if isinstance(loaded, list)
            else [loaded]
        )
    except json.JSONDecodeError:
        repaired = True
        for chunk in _balanced_objects(text):
            try:
                loaded = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if isinstance(loaded, dict):
                payloads.append(loaded)

    payloads = [item for item in payloads if isinstance(item, dict)]
    if not payloads:
        return ParsedAnswer(answer=None, error="没有解析出任何 JSON 对象", repaired=repaired)

    try:
        return ParsedAnswer(
            answer=StructuredAnswer.model_validate(_merge(payloads)), repaired=repaired
        )
    except ValidationError as error:
        return ParsedAnswer(
            answer=None, error=f"结构不符合约定：{error.error_count()} 处", repaired=repaired
        )


def render(answer: StructuredAnswer, evidence_display: dict[str, str]) -> str:
    """把结构化主张渲染成给人看的正文。

    **引用由服务端拼**（§9.2）——`evidence_display` 是 `{"1": "《中华人民共和国劳动法》第五十条"}`
    这样的映射，由调用方从检索结果生成；模型写的东西一律不进这个映射。
    """
    if not answer.claims:
        parts = ["依据不足：给定的条文中没有可以支持结论的内容。"]
        if answer.missing_facts:
            parts.append("还缺：" + "；".join(answer.missing_facts))
        if answer.conflicts:
            parts.append("存在冲突：" + "；".join(answer.conflicts))
        return "\n".join(parts)

    lines: list[str] = []
    for index, claim in enumerate(answer.claims, start=1):
        markers = [
            evidence_display[item] for item in claim.evidence_ids if item in evidence_display
        ]
        suffix = f"（依据：{'；'.join(markers)}）" if markers else ""
        lines.append(f"{index}. {claim.text}{suffix}")
        if claim.conditions:
            lines.append(f"   前提：{'；'.join(claim.conditions)}")
        if claim.limitations:
            lines.append(f"   未确认：{'；'.join(claim.limitations)}")
    if answer.missing_facts:
        lines.append(f"还缺：{'；'.join(answer.missing_facts)}")
    if answer.conflicts:
        lines.append(f"存在冲突：{'；'.join(answer.conflicts)}")
    return "\n".join(lines)


#: 交给 Ollama 做**约束解码**的 JSON Schema（`format=<schema>`，不只是 `format="json"`）。
#:
#: ⚠️ **只写 `"json"` 挡不住模型写坏 JSON**：实测 22 条金标准里 **11 条**输出
#: `"limitations[]}`——键与空数组之间**漏了冒号**（只在值为空数组时发生）。给完整 schema 后由
#: 语法约束生成，这类残缺不可能出现。
SCHEMA: dict = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "text": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "conditions": {"type": "array", "items": {"type": "string"}},
                    "limitations": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "evidence_ids"],
            },
        },
        "missing_facts": {"type": "array", "items": {"type": "string"}},
        "conflicts": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["claims"],
}
