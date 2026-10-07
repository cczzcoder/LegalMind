"""§9.3 第二层的**语义校验**：这条证据到底支不支持这条主张。

**为什么第一层不够**：第一层只能查「条号在不在证据集里」。实测（V1.22）问「我是名特困人员，我能否
领到社会救助？」，模型引用**完全正确**——社会救助法第十六条就是特困人员供养——但结论越了界。
第一层对这种事**结构上无能为力**：它不判断「这条文是否支持这主张」。

**判官只能是本地模型**（§9.5「P6 的生成与核验只走本地模型」）。但「让模型去判模型」如果手里没有
尺子，就只是把信任从一处搬到另一处。所以这一层配了**断言级金标准**
（`evaluations/datasets/claim_verification.json`）与度量脚本 `scripts/evaluate_semantics.py`：
**先把判官抓得住什么、漏掉什么量清楚，再决定要不要让它拦结论。**

**判官看得到什么**：主张正文、它引用的条文（全文），以及**同一批证据里其余条文的全文**——
最后这项是为了让 §9.3 的「是否遗漏条件、例外或**相反依据**」有判断材料。

**判错了怎么办**：两类错误代价不同——**放行坏断言**会让错误结论出到用户面前；**拦下好断言**只是
多转一次人工。所以判定失败一律走「不发布正式结论 + 待人工」。
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.modules.answering.evidence import STATUS_LABELS
from app.modules.answering.verification import normalize_for_match

#: 判官交出的原句至少要有这么长（去空白后）才算数。
MIN_QUOTE = 6

#: 缺陷类型（§9.3 第二层四项的落地口径）。
ISSUE_KINDS = (
    "not_supported",
    "over_generalized",
    "missing_condition",
    "missing_exception",
    "contradicts",
    "misattributed",
)

INSTRUCTIONS = (
    "下面是一条「主张」以及本次检索到的条文。请判断：**这些条文是否支持这条主张**。\n"
    "只输出 JSON：\n"
    '{"supported": true, "evidence_quote": "条文里支持该主张的原句", "issues": []}\n'
    "\n"
    "判断规则（**两个方向不对称，别搞反**）：\n"
    "- 主张比条文**说得少**是允许的——**不要因为主张没覆盖条文的全部内容就判它不支持**。"
    "例如条文写「探矿权的期限为5年，期限届满可以续期，续期最多不超过3次」，"
    "主张只说「探矿权的期限为5年」，**算支持**。\n"
    "- 主张里出现**条文没写的内容**才是不支持：加了条文没有的主体、范围或结论（not_supported）；"
    "把条文的范围放大（over_generalized）。\n"
    "- 把条文的「可以」说成「应当」（或反之）——over_generalized。\n"
    "- 改了条文里的数字、主体、称谓——not_supported。\n"
    "- 主张与条文**相反**——contradicts。\n"
    "- 主张引用的条文不是讲这件事的——misattributed。\n"
    "- 主张用了「一律」「均」「都」这类**穷尽**说法，而条文其实有条件或例外——"
    "missing_condition / missing_exception。\n"
    "\n"
    "要求：\n"
    "- **判 supported=true 时，evidence_quote 必须照抄条文里的原句**（不要改写、不要拼凑）；"
    "找不到这样的句子就不要判支持。\n"
    "- 判 supported=false 时 evidence_quote 填空字符串，并在 issues 里写明多了什么或改了哪里。\n"
    "- issues 为空当且仅当 supported 为 true。"
)

#: 判官的输出结构。给 Ollama 走**约束解码**——只写 `format: "json"` 挡不住残缺 JSON
#: （见《技术决策与踩坑记录》§5.11）。
REVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "supported": {"type": "boolean"},
        "evidence_quote": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(ISSUE_KINDS)},
                    "detail": {"type": "string"},
                },
                "required": ["kind", "detail"],
            },
        },
    },
    "required": ["supported", "evidence_quote", "issues"],
}


class Issue(BaseModel):
    kind: str
    detail: str = ""


#: 服务端加上的缺陷类型（不在给模型的枚举里）——判官说支持，却交不出条文里的原句。
UNVERIFIABLE_QUOTE = "unverifiable_quote"


class ClaimReview(BaseModel):
    """一条主张的核验结论。

    ``evidence_quote`` 是判官**声称支持该主张的条文原句**。要求它交原句是为了给语义判断加一道
    **可确定性核对**的锚：说支持却引不出原文，就降级为不支持（见 `review_claim`）。
    """

    supported: bool
    evidence_quote: str = ""
    issues: list[Issue] = Field(default_factory=list)

    def summary(self) -> str:
        return "；".join(f"[{item.kind}] {item.detail}" for item in self.issues)


@dataclass(frozen=True)
class SemanticReview:
    """整份结论的语义核验结果。

    ``unavailable`` 为真表示**判官没能给出结论**（模型不可用、超时、输出解析不出来）。
    §9.4 明令「语义核验模型不可用时，**不跳过核验**发布完整结论」——所以这一路也按不通过处理，
    但把它单独标出来，免得和「判官判它不行」混为一谈。
    """

    ok: bool
    reviews: tuple[ClaimReview, ...]
    unavailable: bool = False
    reason: str | None = None


def _label(item) -> str:
    status = STATUS_LABELS.get(item.legal_status, item.legal_status or "")
    return (
        f"《{item.instrument_title}》{item.provision_display or item.provision_number}（{status}）"
    )


def build_review_messages(claim_text: str, question: str, cited: list, others: list) -> list[dict]:
    """拼出判官的对话消息。

    ⚠️ 与生成一样，**指令放用户消息里、不放 system 角色**（实测小模型对 system 角色的指令跟随
    很脆，见 `evidence.INSTRUCTIONS` 的注释）。
    """
    blocks = [f"[{index}] {_label(item)}\n{item.text}" for index, item in enumerate(cited, start=1)]
    body = "\n\n".join(blocks) if blocks else "（这条主张没有引用任何条文）"
    tail = ""
    if others:
        tail = "\n\n同一批检索里还有（**本条主张没有引用它们**，请判断是否遗漏了其中的条件或相反依据）：\n\n"
        tail += "\n\n".join(f"[未引用] {_label(item)}\n{item.text}" for item in others)
    return [
        {
            "role": "user",
            "content": (
                f"{INSTRUCTIONS}\n\n问题：{question}\n\n"
                f"主张：{claim_text}\n\n主张引用的条文：\n\n{body}{tail}"
            ),
        }
    ]


def parse_review(raw: str) -> ClaimReview | None:
    """解析判官输出；解析不出来返回 None（由调用方按「核验不可用」处理）。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    try:
        payload: Any = json.loads(text.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or "supported" not in payload:
        return None
    try:
        return ClaimReview.model_validate(payload)
    except ValidationError:
        return None


def review_claim(claim, question: str, evidence: list, generate) -> ClaimReview | None:
    """核验一条主张。`generate` 是 `generation.generate` 的注入点（便于测试与评测复用）。"""
    index_of = {str(index): item for index, item in enumerate(evidence, start=1)}
    cited = [index_of[item] for item in claim.evidence_ids if item in index_of]
    others = [
        item
        for index, item in enumerate(evidence, start=1)
        if str(index) not in set(claim.evidence_ids)
    ]
    raw = generate(
        build_review_messages(claim.text or "", question, cited, others),
        schema=REVIEW_SCHEMA,
    )
    review = parse_review(raw)
    if review is None or not review.supported:
        return review

    # 判官说支持，就得交出条文里的原句——**这是给语义判断加的可核对锚点**。
    # 实测（2026-10-07）首版没有这道核对，判官会声称条文「未明确」写着的东西（原句就在眼前）。
    # 核对不过就**降级为不支持**：宁可多转一次人工，也不要放行一条引不出原文的「支持」。
    if not quote_in_evidence(review.evidence_quote, cited):
        return ClaimReview(
            supported=False,
            evidence_quote=review.evidence_quote,
            issues=[
                Issue(
                    kind=UNVERIFIABLE_QUOTE,
                    detail=f"判官判了支持，但引不出条文原句：{review.evidence_quote[:40]!r}",
                )
            ],
        )

    # 引文既然可核对，就顺手把「可以 / 应当」这类**确定性**的一致性也核一遍（见 `modal_mismatch`）
    mismatch = modal_mismatch(claim.text or "", review.evidence_quote)
    if mismatch:
        return ClaimReview(
            supported=False,
            evidence_quote=review.evidence_quote,
            issues=[Issue(kind="over_generalized", detail=mismatch)],
        )
    return review


def quote_in_evidence(quote: str, cited: list) -> bool:
    """判官交的原句是否真的在条文里（去空白比对）。

    **为什么要容忍前缀**：实测判官会把出处一起抄进原句
    （`《中华人民共和国社会救助法》第十五条 社会救助分为……`），而条文正文里没有法律名称那一段。
    所以先剥掉开头的书名号与条号，再比对；整句不行就按句读拆开，任一片段（够长）命中即可。
    太短的片段（< `MIN_QUOTE`）不算——「工资」「应当」这种到处都有，命中了也证明不了什么。
    """
    flat = normalize_for_match(quote)
    if len(flat) < MIN_QUOTE:
        return False
    evidence = [normalize_for_match(item.text or "") for item in cited]
    candidates = [flat, _strip_citation_prefix(flat)]
    candidates.extend(
        fragment
        for candidate in list(candidates)
        for fragment in re.split(r"[。；]", candidate)
        if len(fragment) >= MIN_QUOTE
    )
    return any(candidate in text for candidate in candidates for text in evidence)


def _strip_citation_prefix(text: str) -> str:
    """剥掉开头的《法律名》与「第X条」——判官抄原句时常把出处一起带上。"""
    result = text
    while True:
        stripped = _CITATION_PREFIX.sub("", result)
        if stripped == result:
            return result
        result = stripped


_CITATION_PREFIX = re.compile(
    r"^(?:《[^》]{1,80}》|[（(]?第[零一二三四五六七八九十百千0-9]{1,8}条(?:之[0-9]+)?[）)]?)"
)


def modal_mismatch(claim_text: str, quote: str) -> str | None:
    """「可以」与「应当」是否被互换——**给语义判断补一个确定性的锚点**。

    这是 §9.3「是否存在过度概括」里最要命的一种：把条文的**裁量**说成**义务**（或反之），
    在法条场景里是实质性错误。实测判官能抄对原句（含「可以加收滞纳金」），却看不出主张把它写成了
    「应当加收滞纳金」——所以这里用确定性规则补上：**原句是判断依据，规则只做一致性核对**。

    只在「一边有、另一边没有」时报警：主张与引文都含「应当」不算错。
    """
    claim = normalize_for_match(claim_text)
    source = normalize_for_match(quote)
    if not claim or not source:
        return None
    for stronger, weaker in (("应当", "可以"), ("可以", "应当")):
        if (
            stronger in claim
            and weaker not in claim
            and weaker in source
            and stronger not in source
        ):
            return f"主张用了「{stronger}」，而引用的条文用的是「{weaker}」"
    return None


def review(answer, question: str, evidence: list, generate) -> SemanticReview:
    """逐条核验整份结构化结论。

    **一条不过就整份不通过**——§9.4「正式答案通过门禁后发送」，部分通过也还是没通过。
    判官给不出结论（超时、输出解析不出来）时按 §9.4「语义核验模型不可用**不跳过核验**」处理：
    同样不发布，但标成 ``unavailable``，免得和「判官判它不行」混为一谈。
    """
    reviews: list[ClaimReview] = []
    for claim in answer.claims:
        item = review_claim(claim, question, evidence, generate)
        if item is None:
            return SemanticReview(
                ok=False,
                reviews=tuple(reviews),
                unavailable=True,
                reason="判官没有给出可解析的结论",
            )
        reviews.append(item)
    return SemanticReview(ok=all(item.supported for item in reviews), reviews=tuple(reviews))
