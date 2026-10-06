"""§9.3 第一层的**确定性校验**：模型说出来的依据，必须真的落在本次证据里。

**为什么需要**：返回给调用方的 `citations` 是**检索命中的条款版本**，天然在授权范围内——
它们不可能凭空出现。真正会凭空出现的是**模型自己写在答案里的法律名、条号、数值和引文**。
生成质量评测（§9.5）量过：Qwen2.5-7B 在 22 条用例上没有编造过条号，但那是**观测到的概率**，
不是保证；法条场景里「把依据说成不存在的那一条」是最危险的一类错误，必须有确定性门禁兜住。

**为什么放在回答层而不是写进提示词**：安全属性不能交给模型自觉——与效力状态门禁同一个理由
（见 `service.NON_CURRENT_NOTICE`）。

**本版实现四项**（§9.3 第一层共六项）：

- 引用 ID 是否存在于本次授权证据集 → **结构化输出的主路径**走 `verify_claims`：`evidence_ids`
  必须落在证据编号里、**每条主张至少要有一个编号**（没有编号的主张就是没有依据）；自由文本里
  的**指名引用**（法名 + 同一句内的条号）也查，只提法名而没引条号不算引用——那通常是在说明
  「缺什么」（见 `_check_text` 里的注释）；
- 条号是否一致 → 答案里的**条号**必须是证据条款本身、或证据正文里出现过的条号
  （按 `normalize_article_number` 同一套口径归一后比对——模型写「第一百条」还是「第100条」都认）；
- 数值是否来自输入或明确证据 → 答案里的阿拉伯数值必须出现在证据正文或问题里；
- 引文是否匹配固定版本 → 「」/“”里的引文必须能在证据正文里找到（去空白比对）。

**未实现两项**（不是漏了，是当前没有可依赖的来源）：

- 证据是否发布、可访问且未被撤回——由检索的授权复核（`retrieval/service.py` 里 join 原件 +
  `AuthorizationService.document_scope`）与 §15.3 撤下流程保证，不在这里重复；
- 已确认的适用范围是否匹配——`applicability_records` 的业务尚未实现。

**这里只回答「依据是不是真的」，不回答「说得对不对」**：后者是 §9.3 第二层语义校验的事。
"""

import re
from dataclasses import dataclass

from app.modules.answering.evidence import STATUS_LABELS
from app.modules.legal_corpus.structure import chinese_number_to_int

_WS = re.compile(r"[\s\u3000]+")
# 条号允许中文数字或阿拉伯数字（模型两种都可能写）；与 `structure.normalize_article_number`
# 共用同一个中文数字转换器，避免出现第二套口径
_ARTICLE_IN_TEXT = re.compile(
    r"第([零一二三四五六七八九十百千0-9]{1,8})条(?:之([零一二三四五六七八九十0-9]{1,4}))?"
)
_LAW_IN_TEXT = re.compile(r"《([^》]{1,80})》")
#: 引号里的内容（直角引号与弯引号都收）
_QUOTED = re.compile(r"[「“]([^」”]{2,400})[」”]")
#: 阿拉伯数值。**排除条号里的数字**（`(?<!第)`，「第100条」归条号检查管），
#: 也排除列表序号（后面紧跟 `.`/`、`/`)`/`）` 的不算「数值」）。
_NUMBER = re.compile(r"(?<!第)(\d+(?:\.\d+)?)(?![.、)）])")
_SENTENCE_SPLIT = re.compile(r"[。；！？\n]")

#: 引文短于这个长度就不查——太短的片段（「工资」「不得」）本来就到处都是，查了只会误报。
MIN_QUOTE_LENGTH = 10


@dataclass(frozen=True)
class Issue:
    """一条核验不通过的理由。`kind` 是稳定的机器可读标识。"""

    kind: str
    detail: str


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    issues: tuple[Issue, ...]

    def summary(self) -> str:
        return "；".join(issue.detail for issue in self.issues)


def normalize_for_match(text: str) -> str:
    """比对前统一去空白（含全角空格）——PDF 折行空格会让逐字子串匹配失效。"""
    return _WS.sub("", text or "")


def short_title(title: str) -> str:
    """法律简称：《中华人民共和国劳动法》→ 劳动法。"""
    return (title or "").replace("中华人民共和国", "")


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
    """从自由文本里抽条号（按出现顺序，含重复）。

    「第一百条」「第100条」「第八十七条之一」都能抽出来；「第一款」这类不抽。
    """
    numbers = []
    for match in _ARTICLE_IN_TEXT.finditer(text or ""):
        number = _to_article_number(match.group(1), match.group(2))
        if number is not None:
            numbers.append(number)
    return numbers


def _supported_text(evidence, question: str) -> str:
    """模型**被允许说到**的全部内容：条文正文 + 出处 + 效力状态 + 问题本身（§9.3 的「输入」）。"""
    parts = []
    for item in evidence:
        parts.append(item.text or "")
        parts.append(item.instrument_title or "")
        parts.append(item.provision_display or item.provision_number or "")
        parts.append(STATUS_LABELS.get(item.legal_status, item.legal_status or ""))
    parts.append(question or "")
    return normalize_for_match("".join(parts))


def _check_text(answer_text: str, question: str, items: list, evidence_flat: str) -> list[Issue]:
    """自由文本层面的四项检查（法名 / 条号 / 数值 / 引文）。"""
    issues: list[Issue] = []

    # 1) **指名引用**必须落在证据里：法名后面（同一句内）跟着条号的，才算「引用」。
    #
    #    只提法律名而没引条号不算——那通常是在**说明缺什么**（实测 22 条里唯一一条误报就是
    #    「依据不足……（《中华人民共和国道路交通安全法》未提供相关条款）」：模型在解释为什么
    #    答不了，不是在拿那条法当依据）。§9.3 第一层管的是**引用 ID**，所以只查「法名 + 条号」。
    #
    #    比对用**简称全等**，不用子串：否则「药品管理法」会被「药品管理法实施条例」掩盖
    #    （`legal_corpus/citations.py` 踩过这个前缀坑）。
    known_short = {short_title(item.instrument_title or "") for item in items}
    for match in _LAW_IN_TEXT.finditer(answer_text or ""):
        named = match.group(1).strip()
        if short_title(named) in known_short:
            continue
        rest = (answer_text or "")[match.end() :]
        sentence = _SENTENCE_SPLIT.split(rest, maxsplit=1)[0]
        if extract_article_numbers(sentence):
            issues.append(Issue("unwarranted_law", f"引用了本次证据之外的《{named}》的条号"))

    # 2) 条号必须落在证据里：证据条款本身，或证据正文里出现过的条号（模型复述条文里的交叉引用
    #    不算编造——「依照本法第五十一条规定」是条文自己写的）
    allowed = {item.provision_number for item in items}
    allowed |= set(extract_article_numbers(evidence_flat))
    seen: set[str] = set()
    for number in extract_article_numbers(answer_text):
        if number in allowed or number in seen:
            continue
        seen.add(number)
        issues.append(Issue("unwarranted_article", f"条号 {number} 不在本次证据里"))

    # 3) 数值必须来自证据或问题
    supported = _supported_text(items, question)
    for match in _NUMBER.finditer(answer_text or ""):
        token = match.group(1)
        if token not in supported:
            issues.append(Issue("unsupported_number", f"数值 {token} 不在证据或问题里"))

    # 4) 引文必须能在证据正文里找到（带省略号的引文不查——省略号本来就不是逐字引用）
    for match in _QUOTED.finditer(answer_text or ""):
        quoted = normalize_for_match(match.group(1))
        if len(quoted) < MIN_QUOTE_LENGTH or "…" in quoted or "..." in quoted:
            continue
        if quoted not in evidence_flat:
            issues.append(Issue("unsupported_quote", f"引文不在证据正文里：{match.group(1)[:40]}"))

    return issues


def verify(answer_text: str, question: str, evidence) -> VerificationResult:
    """校验一段**自由文本**是否只依据了本次证据。

    结构化输出（§9.2）之后主路径走 `verify_claims`；这里保留文本层检查，因为**主张正文本身
    仍可能写出证据外的条号**，两者是叠加的。
    """
    items = list(evidence)
    evidence_flat = normalize_for_match("".join(item.text or "" for item in items))
    issues = _check_text(answer_text, question, items, evidence_flat)
    return VerificationResult(ok=not issues, issues=tuple(issues))


def verify_claims(answer, question: str, evidence) -> VerificationResult:
    """校验**结构化主张**（§9.2）：证据编号必须存在、每条主张都必须带编号，主张正文再过文本检查。

    `answer` 是 `claims.StructuredAnswer`。**这才是 §9.3 第一层该有的形态**——引用是 ID，
    「在不在本次证据集里」是集合判断，不用猜模型把哪一句写成了引用。
    """
    items = list(evidence)
    issues: list[Issue] = []
    allowed = {str(index) for index in range(1, len(items) + 1)}

    for claim in answer.claims:
        excerpt = (claim.text or "")[:40]
        unknown = [item for item in claim.evidence_ids if item not in allowed]
        if unknown:
            issues.append(
                Issue("unwarranted_evidence", f"主张「{excerpt}」引用了不存在的证据编号 {unknown}")
            )
        if not claim.evidence_ids:
            # §9.2「模型仅输出结构化主张**和允许的证据 ID**」：没有编号的主张就是没有依据
            issues.append(Issue("unsupported_claim", f"主张「{excerpt}」没有给出证据编号"))

    evidence_flat = normalize_for_match("".join(item.text or "" for item in items))
    issues.extend(
        _check_text(
            "\n".join(claim.text or "" for claim in answer.claims), question, items, evidence_flat
        )
    )
    return VerificationResult(ok=not issues, issues=tuple(issues))
