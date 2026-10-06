"""生成质量评测的纯逻辑：数据集自检 + 确定性打分（设计 §9.5、§9.3 第一层）。

**两件事都不调用模型、不碰数据库**，所以可单元测试：

1. **数据集自检**（`validate_structure` / `validate_against_corpus`）。金标准的价值在于
   「期望是**推导的、可复核的**」，不是手写一段参考答案：
   - 每条 `required_facts` 必须是**期望条款正文的去空白子串**——答案里没说到就是没覆盖到；
   - 每条 `forbidden_facts` 必须**真的出现在某条证据里**（否则「陷阱」根本不存在），
     且**不出现在期望条款里**（否则它就是正确答案的一部分）；
   - `expect_citations` 必须是 `evidence` 的子集。
   去空白是因为 PDF 条款带折行空格（实测「民用航 空器」），按原文直接比会漏。

2. **打分**（`score_case`）。指标全是可复现的确定量：
   引用召回 / 凭空引用（模型自己编的条号）/ 关键要素覆盖 / 禁项触犯 / 拒答正确性。
   **引用从答案文本里抽**，与「引用来自检索结果」是两回事——后者保证引用可核验，前者测模型
   到底把依据说成了哪一条。
   **效力状态提示只作诊断、不计入达标**：那是回答层的职责（§8.3），由 `answering/service.py`
   确定性强制、集成测试守，不是模型该做的事。首跑时把它当模型指标是**定错了范围**。
"""

import json
import re
from pathlib import Path
from typing import Any

from app.modules.legal_corpus.structure import chinese_number_to_int

#: 用例分类。`grounded` 单条依据、`multi` 需多条依据、`status` 依据未生效、`abstain` 该拒答。
CATEGORIES = ("grounded", "multi", "status", "abstain")
ABSTAIN_KINDS = ("no_evidence", "irrelevant")

#: 打分要用的阈值键——`summarise` 按这些键算「是否达标」（§9.5「达标后再接入正式问答」）。
#:
#: ⚠️ **`status_flag_rate` 不在其中**：效力状态提示是**回答层的职责**（§8.3「检索结果应携带效力
#: 状态……供回答层与人工核验判断依据是否现行有效」），不是模型该做的事。首跑时把它当模型指标是
#: **定错了范围**——模型指标就该只量模型。它现在作为**诊断项**报告（见 `DIAGNOSTIC_KEYS`），
#: 用来记录「模型会不会自己想到说」，不计入达标。回答层的强制提示由集成测试守
#: （`tests/test_answering.py`）。
THRESHOLD_KEYS = (
    "citation_recall",
    "no_fabrication_rate",
    "fact_coverage",
    "forbidden_rate",
    "abstain_accuracy",
    "over_abstain_rate",
)

#: 只报告、不计入达标的诊断项。
DIAGNOSTIC_KEYS = ("status_flag_rate",)

_WS = re.compile(r"[\s\u3000]+")
# 条号允许中文数字或阿拉伯数字（模型两种都可能写）；与 `structure.normalize_article_number`
# 共用同一个中文数字转换器，避免出现第二套口径
_ARTICLE_IN_TEXT = re.compile(
    r"第([零一二三四五六七八九十百千0-9]{1,8})条(?:之([零一二三四五六七八九十0-9]{1,4}))?"
)

#: 拒答信号。**刻意只收明确的退路说法**，不收「未规定」「不适用」这类正常表述——
#: 否则一句「本条未规定……」会把一个正确答案误判成拒答。
ABSTAIN_MARKERS = (
    "依据不足",
    "无法回答",
    "不能回答",
    "没有检索到",
    "未检索到",
    "缺乏依据",
    "不足以回答",
)

#: 效力状态提示。未生效/已失效的依据**不能当作现行依据**（§8.3、§9.5）。
STATUS_FLAG_MARKERS = (
    "尚未生效",
    "未生效",
    "尚未施行",
    "不得作为现行依据",
    "已被取代",
    "已失效",
    "已经失效",
    "已废止",
)


def normalize_for_match(text: str) -> str:
    """比对前统一去空白（含全角空格）——PDF 折行空格会让逐字子串匹配失效。"""
    return _WS.sub("", text or "")


def short_title(title: str) -> str:
    """法律简称：《中华人民共和国劳动法》→ 劳动法。"""
    return title.replace("中华人民共和国", "")


def _to_article_number(token: str, suffix: str | None) -> str | None:
    """把抽取到的条号转成 `provision_identities.provision_number` 的形态。"""
    if token.isdigit():
        base = str(int(token))
    else:
        value = chinese_number_to_int(token)
        if value is None:
            return None
        base = str(value)
    if not suffix:
        return base
    if suffix.isdigit():
        tail = str(int(suffix))
    else:
        converted = chinese_number_to_int(suffix)
        if converted is None:
            return None
        tail = str(converted)
    return f"{base}之{tail}"


def extract_article_numbers(text: str) -> list[str]:
    """从答案文本里抽条号（按出现顺序，含重复）。

    「第一百条」「第100条」「第八十七条之一」都能抽出来；「第一款」这类不抽。
    """
    numbers = []
    for match in _ARTICLE_IN_TEXT.finditer(text or ""):
        number = _to_article_number(match.group(1), match.group(2))
        if number is not None:
            numbers.append(number)
    return numbers


def _mentioned_laws(answer_flat: str, laws: list[str]) -> set[str]:
    """答案里出现了哪些**证据涉及的法律**（用简称比，模型一般写简称）。"""
    return {law for law in laws if short_title(law) in answer_flat or law in answer_flat}


def score_case(case: dict, answer: str, evidence: list[dict]) -> dict:
    """给一条用例的答案打分。`evidence` 是**实际喂给模型**的证据（顺序一致）。

    每个 `evidence` 项需要 `law` / `article` / `text` / `legal_status` 四个字段。
    """
    answer_flat = normalize_for_match(answer)
    mentioned = extract_article_numbers(answer)
    mentioned_set = set(mentioned)
    evidence_articles = {item["article"] for item in evidence}
    evidence_laws = [item["law"] for item in evidence]
    mentioned_laws = _mentioned_laws(answer_flat, evidence_laws)

    expect = [(law, article) for law, article in case.get("expect_citations", [])]
    cited_expected = []
    for law, article in expect:
        # 只有一部证据法律时，光有条号也算命中（模型写「第一百条」没写法律名）
        law_ok = len(set(evidence_laws)) == 1 or law in mentioned_laws
        if article in mentioned_set and law_ok:
            cited_expected.append([law, article])

    # 凭空引用：答案里出现、却不在本次证据里的条号。**这是最该盯住的幻觉信号**——
    # 模型把不存在的条号说成依据，比答得笼统危险得多。
    unwarranted = sorted(mentioned_set - evidence_articles, key=lambda item: (len(item), item))

    required = [normalize_for_match(fact) for fact in case.get("required_facts", [])]
    required = [fact for fact in required if fact]
    hit_facts = [fact for fact in required if fact in answer_flat]
    forbidden = [fact for fact in case.get("forbidden_facts", []) if normalize_for_match(fact)]
    forbidden_hits = [fact for fact in forbidden if normalize_for_match(fact) in answer_flat]

    abstained = any(marker in answer_flat for marker in ABSTAIN_MARKERS)
    expect_abstain = bool(case.get("expect_abstain"))

    return {
        "id": case["id"],
        "category": case.get("category"),
        "expect_abstain": expect_abstain,
        "answer_chars": len(answer_flat),
        "citation_recall": (len(cited_expected) / len(expect)) if expect else None,
        "cited_expected": cited_expected,
        "expected_citations": [list(pair) for pair in expect],
        "unwarranted_citations": unwarranted,
        "fact_coverage": (len(hit_facts) / len(required)) if required else None,
        "missing_facts": [fact for fact in required if fact not in answer_flat],
        "forbidden_hits": forbidden_hits,
        "abstained": abstained,
        "abstain_correct": abstained == expect_abstain,
        "status_flag_required": bool(case.get("status_flag_required")),
        "status_flagged": any(marker in answer_flat for marker in STATUS_FLAG_MARKERS),
    }


def _mean(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def summarise(records: list[dict], thresholds: dict) -> dict:
    """把逐条结果聚合成 §9.5 要的那几个指标，并对照阈值给出「达标 / 未达标」。

    **未达标不是错误**——它正是评测要告诉你的东西；只有「指标没有可评的用例」才是数据集问题。
    """
    citation_recalls = [
        record["citation_recall"] for record in records if record["citation_recall"] is not None
    ]
    fact_coverages = [
        record["fact_coverage"] for record in records if record["fact_coverage"] is not None
    ]
    abstain_records = [record for record in records if record["expect_abstain"]]
    grounded_records = [record for record in records if not record["expect_abstain"]]
    status_records = [record for record in records if record["status_flag_required"]]

    metrics: dict[str, float | None] = {
        "citation_recall": _mean(citation_recalls),
        "no_fabrication_rate": (
            sum(1 for r in records if not r["unwarranted_citations"]) / len(records)
            if records
            else None
        ),
        "fact_coverage": _mean(fact_coverages),
        "forbidden_rate": (
            sum(1 for r in records if r["forbidden_hits"]) / len(records) if records else None
        ),
        "abstain_accuracy": (
            sum(1 for r in abstain_records if r["abstain_correct"]) / len(abstain_records)
            if abstain_records
            else None
        ),
        "over_abstain_rate": (
            sum(1 for r in grounded_records if r["abstained"]) / len(grounded_records)
            if grounded_records
            else None
        ),
        "status_flag_rate": (
            sum(1 for r in status_records if r["status_flagged"]) / len(status_records)
            if status_records
            else None
        ),
    }

    checks = {}
    for key in THRESHOLD_KEYS:
        value = metrics.get(key)
        threshold = thresholds.get(key)
        checks[key] = {
            "value": value,
            "threshold": threshold,
            # 「越低越好」的指标：越低越达标
            "lower_is_better": key in ("forbidden_rate", "over_abstain_rate"),
            "ok": (
                None
                if value is None or threshold is None
                else (
                    value <= threshold
                    if key in ("forbidden_rate", "over_abstain_rate")
                    else value >= threshold
                )
            ),
        }

    diagnostics = {
        key: {"value": metrics.get(key), "note": "回答层职责，仅报告、不计入达标（§8.3）"}
        for key in DIAGNOSTIC_KEYS
    }

    return {
        "cases": len(records),
        "counts": {
            "abstain": len(abstain_records),
            "grounded": len(grounded_records),
            "status_flag_required": len(status_records),
        },
        "metrics": metrics,
        "checks": checks,
        "diagnostics": diagnostics,
        "passed": all(check["ok"] for check in checks.values() if check["ok"] is not None),
    }


def load_dataset(path: str | Path) -> dict:
    """读金标准；不做校验（校验交给 `validate_structure` / `validate_against_corpus`）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _pair(value: Any) -> tuple[str, str] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return str(value[0]), str(value[1])
    return None


def validate_structure(dataset: dict) -> list[str]:
    """查数据集的结构问题（不依赖语料）。返回问题列表，空列表代表通过。"""
    problems: list[str] = []

    if not isinstance(dataset.get("cases"), list) or not dataset["cases"]:
        problems.append("cases 缺失或为空")
        return problems

    thresholds = dataset.get("thresholds")
    if not isinstance(thresholds, dict):
        problems.append("thresholds 缺失")
    else:
        for key in THRESHOLD_KEYS:
            if key not in thresholds:
                problems.append(f"thresholds 缺 {key}")

    seen: set[str] = set()
    for case in dataset["cases"]:
        case_id = case.get("id")
        if not case_id:
            problems.append("存在没有 id 的用例")
            continue
        if case_id in seen:
            problems.append(f"{case_id}: id 重复")
        seen.add(case_id)

        if case.get("category") not in CATEGORIES:
            problems.append(f"{case_id}: category 非法（{case.get('category')}）")
        if not case.get("question"):
            problems.append(f"{case_id}: question 为空")

        evidence = [pair for pair in (_pair(item) for item in case.get("evidence", [])) if pair]
        if len(evidence) != len(case.get("evidence", [])):
            problems.append(f"{case_id}: evidence 里有非 [法, 条] 形式的项")

        expect_abstain = bool(case.get("expect_abstain"))
        expect = [
            pair for pair in (_pair(item) for item in case.get("expect_citations", [])) if pair
        ]
        if len(expect) != len(case.get("expect_citations", [])):
            problems.append(f"{case_id}: expect_citations 里有非 [法, 条] 形式的项")

        if expect_abstain:
            if case.get("category") != "abstain":
                problems.append(f"{case_id}: expect_abstain 的用例 category 必须是 abstain")
            if case.get("abstain_kind") not in ABSTAIN_KINDS:
                problems.append(f"{case_id}: abstain_kind 非法（{case.get('abstain_kind')}）")
            if expect:
                problems.append(f"{case_id}: 拒答用例不应有 expect_citations")
        else:
            if not expect:
                problems.append(f"{case_id}: 非拒答用例必须给出 expect_citations")
            if not case.get("required_facts"):
                problems.append(f"{case_id}: 非拒答用例必须给出 required_facts")
            missing = [pair for pair in expect if pair not in evidence]
            if missing:
                problems.append(f"{case_id}: expect_citations 不在 evidence 里：{missing}")

    return problems


def validate_against_corpus(dataset: dict, corpus: dict[tuple[str, str], dict]) -> list[str]:
    """查「期望是推导的、可复核的」——每条事实都要能在语料里找到出处。

    `corpus` 键是 `(法, 条)`，值是 `{"text": ..., "legal_status": ...}`。
    """
    problems: list[str] = []
    for case in dataset.get("cases", []):
        case_id = case.get("id")
        evidence = [pair for pair in (_pair(item) for item in case.get("evidence", [])) if pair]
        expect = [
            pair for pair in (_pair(item) for item in case.get("expect_citations", [])) if pair
        ]

        for pair in evidence:
            if pair not in corpus:
                problems.append(f"{case_id}: 证据条款不在语料里：{pair}")
        for pair in expect:
            if pair not in corpus:
                problems.append(f"{case_id}: 期望条款不在语料里：{pair}")
        if problems and problems[-1].startswith(f"{case_id}: "):
            continue  # 条款都不在，下面的子串比对没有意义

        expect_text = normalize_for_match(
            "".join(corpus[pair]["text"] for pair in expect if pair in corpus)
        )
        evidence_text = normalize_for_match(
            "".join(corpus[pair]["text"] for pair in evidence if pair in corpus)
        )

        for fact in case.get("required_facts", []):
            flat = normalize_for_match(fact)
            if flat and flat not in expect_text:
                problems.append(f"{case_id}: required_fact 不是期望条款正文的子串：{fact!r}")
        for fact in case.get("forbidden_facts", []):
            flat = normalize_for_match(fact)
            if not flat:
                continue
            if flat in expect_text:
                problems.append(f"{case_id}: forbidden_fact 出现在期望条款里（矛盾）：{fact!r}")
            if flat not in evidence_text:
                problems.append(f"{case_id}: forbidden_fact 不在任何证据里（陷阱不存在）：{fact!r}")

    return problems
