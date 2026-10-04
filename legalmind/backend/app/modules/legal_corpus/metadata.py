"""法律元数据提取（设计 §5.1、§5.2、§7、§8.3）。

从「前言」——解析产物中正文起点（``StructureOutline.body_start``）之前的块——与文件名提取
法律名称、资料类型、制定机关、文号、版本标识、公布/生效日期与效力状态，供法律版本落库使用。

**原则**

- **正文优先、文件名回退**：正文的公布信息是权威来源；文件名只作最后兜底，且一旦回退即标记
  为低置信度（见下）。
- **未知即未知，不虚构**（设计 §5.3）：日期取不到存 ``None``，效力状态取不到记 ``unknown``。
- **PDF 折行会切断日期与关键词**：实测「2026年」与「6月26日」分属两块，因此所有匹配都在
  **去除空白后的前言**上进行。
- 提取结果附带 ``low_confidence_reasons``，供落库时做**质量门禁**：正文完整且无冲突才自动
  确认（``review_status='approved'``），否则置为待审核（``review_status='pending'``）。

16 份真实样本上的实测覆盖：14 份可从正文公布信息得到版本标识与公布日期，2 份只能回退
（``矿产资源法实施条例`` 前言无公布信息 → 文件名日期；``人工智能科技伦理审查与服务办法``
只有印发日期、无通过/修正/修订子句 → 正文末个日期 + 低置信度标记）。
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import PurePosixPath

from app.modules.legal_corpus.structure import chinese_number_to_int

# 资料类型取值与 models.LEGAL_INSTRUMENT_TYPES 一致
CONSTITUTION = "constitution"
LAW = "law"
ADMINISTRATIVE_REGULATION = "administrative_regulation"
JUDICIAL_INTERPRETATION = "judicial_interpretation"
LOCAL_REGULATION = "local_regulation"
DEPARTMENT_RULE = "department_rule"
OTHER = "other"

# 效力状态取值与 models.LEGAL_STATUSES 一致
EFFECTIVE = "effective"
NOT_YET_EFFECTIVE = "not_yet_effective"
REPEALED = "repealed"
UNKNOWN_STATUS = "unknown"

DEFAULT_JURISDICTION = "中国"
UNKNOWN_TEXT = "未知"
UNLABELLED_VERSION = "未标注版本"

# 版本标识缺省值的低置信度标记
REASON_VERSION_FROM_FILENAME = "version_label_from_filename"
REASON_VERSION_UNLABELLED = "version_label_unlabelled"
REASON_VERSION_WEAK_DATE = "version_label_from_bare_date"
REASON_TITLE_FROM_FILENAME = "title_from_filename"

_WS = re.compile(r"[\s\u3000]+")
# 文号里的全角数字（公报文本会用），统一成半角后再比较
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
# 「第七十七号」→「第77号」；只认纯中文数字，避免误改其他内容
_ORDER_NUMBER = re.compile(r"第([零一二三四五六七八九十百千]+)号")
_DATE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_EFFECTIVE_FROM = re.compile(r"自(\d{4})年(\d{1,2})月(\d{1,2})日起施行")
_DOCUMENT_NUMBER = re.compile(
    r"(中华人民共和国主席令|国务院令)第([零一二三四五六七八九十百千〇\d]+)号"
)
_BOOK_TITLE = re.compile(r"《([^《》〈〉]{2,80})》")
# 前言首块可能是印发通知（「工业和信息化部等十部门关于印发《…》的通知」），据此取制定机关
_ISSUER_PREFIX = re.compile(r"^(.{2,40}?)(?=关于|印发|发布|制定)")
# 文件名尾部的日期后缀，如 中华人民共和国劳动法_20181229.docx
_FILENAME_DATE_SUFFIX = re.compile(r"_(\d{4})(\d{2})(\d{2})$")
# 文件名尾部的版本括注（（2026年修订）/（2018年修正文本）/（2004年修正）/（2026年））。
# 刻意不含「试行」——「（试行）」是名称的一部分，不能剥离。
_FILENAME_VERSION_PAREN = re.compile(
    r"[（(][^（()）]{0,30}(?:年|修正|修订|文本|草案)[^（()）]{0,30}[)）]$"
)

# 名称后缀 → 资料类型。**必须按后缀长度降序匹配**：单字的「法」会抢先命中「办法」「条例」
# 之类的双字后缀，把部门规章误判为法律。
_SUFFIX_TYPES = (
    ("法典", LAW),
    ("条例", ADMINISTRATIVE_REGULATION),
    ("办法", DEPARTMENT_RULE),
    ("规定", DEPARTMENT_RULE),
    ("细则", DEPARTMENT_RULE),
    ("规则", DEPARTMENT_RULE),
    ("通则", DEPARTMENT_RULE),
    ("解释", JUDICIAL_INTERPRETATION),
    ("法", LAW),
)

# 效力状态的优先级（数字越小越优先）：当前有效 > 已公布未生效 > 已废止/被取代 > 无法判定。
# 多原件同版本时按此排序给出建议（设计 §7 的版本关联确认仍由人工完成）。
STATUS_RANK = {EFFECTIVE: 0, NOT_YET_EFFECTIVE: 1, REPEALED: 2, UNKNOWN_STATUS: 3}


@dataclass(frozen=True)
class LegalMetadata:
    """从正文前言与文件名提取出的法律元数据。"""

    title: str
    instrument_type: str
    jurisdiction: str
    issuing_body: str
    document_number: str | None
    version_label: str
    promulgated_on: date | None
    effective_from: date | None
    legal_status: str
    low_confidence_reasons: tuple[str, ...] = field(default=())

    @property
    def confident(self) -> bool:
        """正文提取完整且无回退、无弱证据时才算高置信度（质量门禁，见模块 docstring）。"""
        return not self.low_confidence_reasons


def normalize_title(text: str) -> str:
    """规范化名称：去空白与全角空格、去书名号与版本括注。"""
    cleaned = _WS.sub("", text).strip()
    cleaned = cleaned.strip("《》〈〉")
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = _FILENAME_VERSION_PAREN.sub("", cleaned)
    return cleaned


def normalize_document_number(text: str) -> str:
    """规范化文号，供精确匹配（设计 §8.3）。

    去全部空白、全角数字转半角，并把「第X号」里的中文数字转成阿拉伯数字，使
    「中华人民共和国主席令第七十七号」与「…第77号」落到同一个值上。只做这两种等价改写，
    不改动文号本身的结构——写错一个字的文号不该被"猜"到。
    """
    cleaned = _WS.sub("", text).translate(_FULLWIDTH_DIGITS)
    return _ORDER_NUMBER.sub(_arabic_order_number, cleaned)


def _arabic_order_number(match: re.Match) -> str:
    value = chinese_number_to_int(match.group(1))
    return f"第{value}号" if value is not None else match.group(0)


def filename_title(filename: str) -> str:
    """从文件名推断名称：去扩展名、去 ``_YYYYMMDD`` 尾缀、去版本括注。"""
    stem = PurePosixPath(filename.replace("\\", "/")).stem
    stem = _FILENAME_DATE_SUFFIX.sub("", stem)
    return normalize_title(stem)


def filename_date(filename: str) -> date | None:
    """文件名里的 ``_YYYYMMDD`` 尾缀；取不到返回 ``None``（不虚构）。"""
    match = _FILENAME_DATE_SUFFIX.search(PurePosixPath(filename.replace("\\", "/")).stem)
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _to_date(year: str, month: str, day: str) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _is_law_title(text: str) -> bool:
    """是否像法律名称：无句读、不太长、以法律名称后缀结尾（可带括注）。"""
    if not text or len(text) > 80 or any(char in text for char in "，。；：、！？"):
        return False
    if "关于" in text:
        return False
    base = re.sub(r"[（(][^（()）]*[)）]$", "", text)
    return any(
        base.endswith(suffix)
        for suffix in ("法典", "法", "条例", "办法", "规定", "细则", "规则", "通则", "解释")
    )


def _extract_title(preamble_blocks: Sequence[str], filename: str) -> tuple[str, bool]:
    """返回 ``(名称, 是否退化为文件名)``。正文候选与文件名一致时视为正文确认。"""
    from_filename = filename_title(filename)
    candidates: list[str] = []
    for block in preamble_blocks:
        stripped = _WS.sub("", block)
        for book in _BOOK_TITLE.findall(stripped):
            if _is_law_title(book):
                candidates.append(normalize_title(book))
        if _is_law_title(stripped):
            candidates.append(normalize_title(stripped))

    for candidate in candidates:
        if from_filename and candidate == from_filename:
            return candidate, False
    if candidates:
        return candidates[0], True
    if from_filename:
        return from_filename, True
    return UNKNOWN_TEXT, True


def _extract_issuing_body(stripped: str, preamble_blocks: Sequence[str]) -> str:
    if "全国人民代表大会常务委员会" in stripped:
        return "全国人民代表大会常务委员会"
    if "全国人民代表大会" in stripped:
        return "全国人民代表大会"
    if "国务院令" in stripped:
        return "国务院"
    for block in preamble_blocks:
        match = _ISSUER_PREFIX.match(_WS.sub("", block))
        if match:
            return match.group(1)
    return UNKNOWN_TEXT


def _extract_document_number(stripped: str) -> str | None:
    matches = list(_DOCUMENT_NUMBER.finditer(stripped))
    if not matches:
        return None
    # 取最后一个：一份文本的沿革会列出历次公布令，最近一次在末尾
    return matches[-1].group(0)


def _last_amendment_clause(stripped: str) -> tuple[date, str] | None:
    """最后一个「日期 + 通过/修正/修订」子句，返回 ``(日期, 动作)``。

    在去空白文本上匹配，避免 PDF 折行把日期与关键词切开。关键词优先级为
    修订 > 修正 > 通过：一次修正的子句里常同时出现「通过…的《修正案》修正」，取「修正」才对。
    """
    last: tuple[date, str] | None = None
    for match in _DATE.finditer(stripped):
        window = stripped[match.end() : match.end() + 60]
        kind = next((word for word in ("修订", "修正", "通过") if word in window), None)
        if kind is None:
            continue
        value = _to_date(match.group(1), match.group(2), match.group(3))
        if value is not None:
            last = (value, kind)
    return last


def _extract_version(stripped: str, filename: str) -> tuple[str, date | None, list[str]]:
    """返回 ``(版本标识, 公布日期, 低置信度原因)``。"""
    clause = _last_amendment_clause(stripped)
    if clause is not None:
        value, kind = clause
        label = f"{value.year}年" if kind == "通过" else f"{value.year}年{kind}"
        return label, value, []

    # 正文没有沿革子句：退一步用前言里最后一个日期（如印发日期），但标记为弱证据
    dates = [d for d in (_to_date(*m.groups()) for m in _DATE.finditer(stripped)) if d is not None]
    if dates:
        value = dates[-1]
        return f"{value.year}年", value, [REASON_VERSION_WEAK_DATE]

    # 再退一步用文件名日期；仍取不到则记未标注版本
    fallback = filename_date(filename)
    if fallback is not None:
        return fallback.isoformat(), fallback, [REASON_VERSION_FROM_FILENAME]
    return UNLABELLED_VERSION, None, [REASON_VERSION_UNLABELLED]


def _infer_instrument_type(title: str, issuing_body: str) -> str:
    # 尾部的「（试行）」「（暂行）」等括注不参与后缀匹配
    base = re.sub(r"[（(][^（()）]*[)）]$", "", title)
    if "宪法" in base:
        return CONSTITUTION
    for suffix, kind in _SUFFIX_TYPES:
        if not base.endswith(suffix):
            continue
        if kind is DEPARTMENT_RULE and issuing_body in (
            "国务院",
            "全国人民代表大会",
            "全国人民代表大会常务委员会",
        ):
            # 国务院以「条例」以外形式发布的规范性文件仍属行政法规
            return ADMINISTRATIVE_REGULATION
        return kind
    if issuing_body == "国务院":
        return ADMINISTRATIVE_REGULATION
    return OTHER


def _legal_status(effective_from: date | None, today: date) -> str:
    if effective_from is None:
        return UNKNOWN_STATUS
    return NOT_YET_EFFECTIVE if effective_from > today else EFFECTIVE


def extract_metadata(
    filename: str,
    preamble_blocks: Sequence[str],
    *,
    today: date,
) -> LegalMetadata:
    """从文件名与正文前言提取法律元数据。

    ``preamble_blocks`` 是正文起点之前的块文本（按顺序）；``today`` 用于判定效力状态，
    显式传入以便测试确定。
    """
    stripped = _WS.sub("", "".join(preamble_blocks))
    title, title_from_filename = _extract_title(preamble_blocks, filename)
    version_label, promulgated_on, reasons = _extract_version(stripped, filename)
    effective_from = None
    match = _EFFECTIVE_FROM.search(stripped)
    if match is not None:
        effective_from = _to_date(match.group(1), match.group(2), match.group(3))
    issuing_body = _extract_issuing_body(stripped, preamble_blocks)

    if title_from_filename:
        reasons = [*reasons, REASON_TITLE_FROM_FILENAME]

    return LegalMetadata(
        title=title,
        instrument_type=_infer_instrument_type(title, issuing_body),
        jurisdiction=DEFAULT_JURISDICTION,
        issuing_body=issuing_body,
        document_number=_extract_document_number(stripped),
        version_label=version_label,
        promulgated_on=promulgated_on,
        effective_from=effective_from,
        legal_status=_legal_status(effective_from, today),
        low_confidence_reasons=tuple(reasons),
    )
