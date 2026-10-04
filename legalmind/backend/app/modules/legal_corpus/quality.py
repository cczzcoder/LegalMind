"""入库质量门禁（设计 §7、§8.3、§17.1、§20.3）。

把散落在流水线里的判定（来源登记、元数据完整性、解析与条款质量）汇总成一次**只读**校验，
对每个原件给出三态结论：

- ``failed``：违反硬要求，不得进入正式证据范围——来源未登记、授权说明为空（§20.3
  「未登记授权说明的来源不得入库」）、解析产物为空、原件没有挂到任何法律版本。
- ``degraded``：已落库但置信度不足或质量有缺口——流水线已置 ``review_status='pending'``
  （版本标识回退、标题退化为文件名、多原件冲突），或该版本没有条款身份。
- ``passed``：高置信度自动落库（``review_status='approved'``）且条款齐全。

「高置信度自动落库、低置信度降级待审」由流水线完成（``service.link_legal_version``）；
本模块只**校验**其结果，不改变任何数据。判定做成纯函数便于单元测试，
取数与报告见 ``scripts/quality_gate.py``。
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuditEvent,
    Chunk,
    LegalVersion,
    ParseRevision,
    ProvisionVersion,
    Source,
    SourceArtifact,
)
from app.modules.legal_corpus.metadata import UNLABELLED_VERSION

PASSED = "passed"
DEGRADED = "degraded"
FAILED = "failed"

# 与 models.LEGAL_VERSION_REVIEW_STATUSES 一致
APPROVED = "approved"

# 流水线挂版本时写入的审计动作（service.link_legal_version）
LINK_ACTION = "document.legal_version_linked"

# 原因码：稳定标识，供脚本、报告与测试引用
REASON_SOURCE_MISSING = "source_missing"
REASON_LICENSE_MISSING = "license_note_missing"
REASON_PARSE_MISSING = "parse_missing"
REASON_PARSE_EMPTY = "parse_empty"
REASON_NOT_LANDED = "not_landed"
REASON_REVIEW_PENDING = "review_pending"
REASON_VERSION_UNLABELLED = "version_unlabelled"
REASON_NO_PROVISIONS = "no_provisions"


@dataclass(frozen=True)
class ArtifactFacts:
    """一个原件在门禁下的只读事实快照（由 :func:`collect_facts` 从库里取出）。"""

    filename: str
    media_type: str
    source_name: str | None
    license_note: str | None
    # 该原件解析版本的分块总数；完全没有解析版本时为 None
    chunk_count: int | None
    # 是否已挂到法律版本（含被更优原件取代的非主原件）
    landed: bool
    # 所挂版本的字段；未挂版本时为 None
    version_label: str | None
    review_status: str | None
    provision_count: int


@dataclass(frozen=True)
class Verdict:
    status: str
    reasons: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.status != FAILED


def assess(facts: ArtifactFacts) -> Verdict:
    """按「硬要求 → 降级项」的顺序判定：硬要求不过即 ``failed``，不再看降级项。"""
    failed: list[str] = []
    if not facts.source_name:
        failed.append(REASON_SOURCE_MISSING)
    elif not (facts.license_note or "").strip():
        # §20.3：没有授权说明的来源不得入库；空说明与没有说明等价
        failed.append(REASON_LICENSE_MISSING)
    if facts.chunk_count is None:
        failed.append(REASON_PARSE_MISSING)
    elif facts.chunk_count == 0:
        failed.append(REASON_PARSE_EMPTY)
    if not facts.landed:
        failed.append(REASON_NOT_LANDED)
    if failed:
        return Verdict(status=FAILED, reasons=tuple(failed))

    degraded: list[str] = []
    if facts.review_status != APPROVED:
        degraded.append(REASON_REVIEW_PENDING)
    if facts.version_label == UNLABELLED_VERSION:
        degraded.append(REASON_VERSION_UNLABELLED)
    if facts.provision_count <= 0:
        degraded.append(REASON_NO_PROVISIONS)
    if degraded:
        return Verdict(status=DEGRADED, reasons=tuple(degraded))
    return Verdict(status=PASSED, reasons=())


async def collect_facts(
    session: AsyncSession, *, source_name: str | None = None
) -> list[ArtifactFacts]:
    """取全库（或指定来源）原件的门禁事实；只读，不写任何数据。"""
    statement = (
        select(SourceArtifact, Source)
        # 外连接：来源缺失要作为门禁失败暴露，而不是让原件从报告里消失
        .outerjoin(Source, SourceArtifact.source_id == Source.id)
        .order_by(SourceArtifact.original_filename)
    )
    if source_name is not None:
        statement = statement.where(Source.name == source_name)
    rows = (await session.execute(statement)).all()

    chunk_counts = {
        artifact_id: total
        for artifact_id, total in (
            await session.execute(
                select(ParseRevision.artifact_id, func.count(Chunk.id))
                .outerjoin(Chunk, Chunk.parse_revision_id == ParseRevision.id)
                .group_by(ParseRevision.artifact_id)
            )
        ).all()
    }
    provision_counts = {
        version_id: total
        for version_id, total in (
            await session.execute(
                select(ProvisionVersion.legal_version_id, func.count(ProvisionVersion.id)).group_by(
                    ProvisionVersion.legal_version_id
                )
            )
        ).all()
    }

    versions = {version.id: version for version in await session.scalars(select(LegalVersion))}
    # 主原件：版本行直接指向它
    landed: dict[UUID, LegalVersion] = {
        version.artifact_id: version
        for version in versions.values()
        if version.artifact_id is not None
    }
    # 被更优原件取代的非主原件：版本行指向别人，但审计事件记了它挂到哪个版本
    for artifact_id, version_id in await _link_events(session):
        version = versions.get(version_id)
        if version is not None and artifact_id not in landed:
            landed[artifact_id] = version

    return [
        ArtifactFacts(
            filename=artifact.original_filename,
            media_type=artifact.media_type,
            source_name=source.name if source is not None else None,
            license_note=source.license_note if source is not None else None,
            chunk_count=chunk_counts.get(artifact.id),
            landed=artifact.id in landed,
            version_label=landed[artifact.id].version_label if artifact.id in landed else None,
            review_status=landed[artifact.id].review_status if artifact.id in landed else None,
            provision_count=(
                provision_counts.get(landed[artifact.id].id, 0) if artifact.id in landed else 0
            ),
        )
        for artifact, source in rows
    ]


async def _link_events(session: AsyncSession) -> list[tuple[UUID, UUID]]:
    """从审计事件里取「原件 → 法律版本」的挂接记录（按时间升序，后写覆盖前写）。"""
    events = (
        await session.execute(
            select(AuditEvent.resource_id, AuditEvent.payload)
            .where(AuditEvent.action == LINK_ACTION)
            .order_by(AuditEvent.created_at)
        )
    ).all()
    pairs: list[tuple[UUID, UUID]] = []
    for resource_id, payload in events:
        raw = payload.get("legal_version_id")
        if not raw:
            continue
        try:
            pairs.append((resource_id, UUID(str(raw))))
        except ValueError:
            continue
    return pairs
