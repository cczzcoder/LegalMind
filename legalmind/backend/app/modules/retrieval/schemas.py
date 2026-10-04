"""检索输入与输出（设计 §8.1、§8.3、§13）。

本版实现 §8.3 的**精确字段与过滤**，以及**中文关键词检索**（V1.11，方案经金标准选型，
见 ``app/modules/retrieval/keyword.py``）。§8.2 的向量与图谱通路尚未实现；RRF 融合待多路齐备后
再做。因此这里也不接受 §8.1 的自然语言问题，「从问题里提取名称与条号」属于问答层（§8.2 第 2 步），
不在本版范围。
"""

from datetime import date
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.models import LEGAL_INSTRUMENT_TYPES, LEGAL_VERSION_REVIEW_STATUSES


class SearchQuery(BaseModel):
    """精确字段与过滤条件。所有字段都可选；只给过滤条件即为「按范围浏览」。"""

    # ---- 精确字段：按规范化后的值精确匹配（设计 §8.3）----
    instrument_title: str | None = Field(
        default=None, max_length=500, description="法律名称，可带《》；规范化后精确匹配"
    )
    document_number: str | None = Field(
        default=None,
        max_length=200,
        description="文号，如「主席令第77号」或「中华人民共和国主席令第七十七号」",
    )
    stable_id: str | None = Field(default=None, max_length=200, description="外部权威稳定标识")
    article_number: str | None = Field(
        default=None, max_length=100, description="条号，接受「第八十七条」或「87」"
    )
    keyword: str | None = Field(
        default=None,
        max_length=500,
        description="中文关键词；按选定方案匹配条款文本，可与精确字段和过滤条件叠加",
    )

    # ---- 过滤 ----
    jurisdiction: str | None = Field(default=None, max_length=100)
    instrument_types: list[str] | None = None
    effective_on: date | None = Field(
        default=None, description="只返回能证明在该日期有效的版本；日期未知的版本保留（不虚构）"
    )
    include_not_yet_effective: bool = Field(
        default=False,
        description="默认屏蔽「已公布未生效」；核对未来/历史版本时才放开（设计 §8.3）",
    )
    review_statuses: list[str] | None = Field(
        default=None, description="审核状态过滤；默认不过滤，结果里始终回传该状态"
    )
    limit: int = Field(default=20, ge=1, le=200)

    @field_validator("instrument_types")
    @classmethod
    def known_instrument_types(cls, value: list[str] | None) -> list[str] | None:
        return _known(value, LEGAL_INSTRUMENT_TYPES, "instrument_type")

    @field_validator("review_statuses")
    @classmethod
    def known_review_statuses(cls, value: list[str] | None) -> list[str] | None:
        return _known(value, LEGAL_VERSION_REVIEW_STATUSES, "review_status")


def _known(value: list[str] | None, allowed: tuple[str, ...], name: str) -> list[str] | None:
    if value is None:
        return None
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ValueError(f"Unknown {name}: {unknown}")
    return value


class CitationOut(BaseModel):
    """引用锚点：足以定位到具体版本与原文位置（设计 §5.2、§8.3）。

    引用必须绑到具体版本行，不能指向「最新条款」；原文位置取该条款首个分块的首个 span。
    """

    instrument_id: UUID
    legal_version_id: UUID
    provision_version_id: UUID
    provision_identity_id: UUID
    artifact_id: UUID
    chunk_id: UUID | None
    # 取不到定位时全为 None（不虚构，设计 §6）
    page_index: int | None = None
    printed_page_label: str | None = None
    char_start: int | None = None
    char_end: int | None = None


class ProvisionHit(BaseModel):
    """一条条款版本。效力状态与审核状态必须回传，供回答层判断能否据此下结论（设计 §8.3）。"""

    instrument_title: str
    instrument_type: str
    jurisdiction: str
    issuing_body: str
    document_number: str | None
    version_label: str
    legal_status: str
    review_status: str
    promulgated_on: date | None
    effective_from: date | None
    effective_to: date | None
    provision_type: str
    provision_number: str
    # 结构路径里的条标题（如「第八十七条」）；结构路径缺失时为 None
    provision_display: str | None
    text: str
    text_sha256: str
    citation: CitationOut


class SearchResponse(BaseModel):
    hits: list[ProvisionHit]
    # 命中数超过 limit 时为真；本版不做总数统计（多一次 count 查询，暂无必要）
    truncated: bool
