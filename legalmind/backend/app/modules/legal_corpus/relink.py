"""按当前择优规则重新挂法律版本树（设计 §7、§8.3）。

多原件的择优规则会变（V1.11 把「格式」提到「导入时间」之前，让 docx 稳定优先），但规则变了
**不会自动重选已经落库的主原件**——``legal_versions.artifact_id`` 停在旧规则选出的那一份。
本模块按**当前**规则重挂一遍：从解析产物重建前言与条款分块，对每份原件重跑
``link_legal_version``，由排序键决定谁当主原件（更优者胜出，落选的不动）。

**不重新解析原件**：只读 ``parse_revisions`` / ``chunks``，不产生新的解析版本，也不动原文定位
与脱敏映射。**不删任何数据**：只改 ``legal_versions.artifact_id`` 与据此重建的条款版本。
取不到解析产物的原件**跳过并如实报告**，不猜。

候选原件从审计事件 ``document.legal_version_linked`` 回溯——本体与原件之间没有直接关联表
（``legal_versions.artifact_id`` 只指向**当前**主原件，落选的那份不在其中）。
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.models import AuditEvent, LegalInstrument, LegalVersion, SourceArtifact
from app.modules.legal_corpus.service import ArticleChunk, link_legal_version, parse_drafts

# 流水线挂版本时写入的审计动作；据此回溯「哪些原件挂过哪个本体」
LINK_ACTION = "document.legal_version_linked"


class _DryRun(Exception):
    """预演：算完报告就回滚事务，不改动数据。"""

    def __init__(self, report: "RelinkReport") -> None:
        self.report = report


@dataclass(frozen=True)
class RelinkChange:
    """某个版本的主原件发生了变化。"""

    instrument_title: str
    version_label: str
    previous_artifact: str | None
    adopted_artifact: str


@dataclass(frozen=True)
class RelinkReport:
    instruments: int
    candidates: int
    relinked: int
    skipped: tuple[str, ...]
    changed: tuple[RelinkChange, ...]
    # 正常情况下为空：版本标识由同一份解析产物推出，与既有一致
    created_versions: tuple[str, ...]


async def relink_versions(
    session: AsyncSession,
    principal: Principal,
    *,
    instrument_ids: set[UUID] | None = None,
    dry_run: bool = False,
    today: date | None = None,
) -> RelinkReport:
    """按当前排序键重挂版本树；``instrument_ids`` 为空表示全部本体。"""
    moment = today or datetime.now(UTC).date()
    try:
        async with session.begin():
            drafts: dict[UUID, tuple[list[str], list[ArticleChunk]]] = {}
            grouped: dict[UUID, list[SourceArtifact]] = {}
            skipped: list[str] = []
            candidates = 0
            for instrument_id, artifact in await _candidates(session, instrument_ids):
                candidates += 1
                rebuilt = await parse_drafts(session, artifact.id)
                if rebuilt is None:
                    # 取不到解析产物就没法可靠地重挂，跳过并如实报告，不猜
                    skipped.append(artifact.original_filename)
                    continue
                drafts[artifact.id] = rebuilt
                grouped.setdefault(instrument_id, []).append(artifact)

            before_ids = set(await session.scalars(select(LegalVersion.id)))
            before = {
                instrument_id: await _primary_map(session, instrument_id)
                for instrument_id in grouped
            }

            relinked = 0
            for instrument_id, artifacts in grouped.items():
                # 顺序不影响结果（排序是全序，逐对比较必然收敛到最优），按导入时间定序便于复现
                for artifact in sorted(artifacts, key=lambda item: item.created_at):
                    preamble, articles = drafts[artifact.id]
                    await link_legal_version(
                        session,
                        principal,
                        artifact=artifact,
                        preamble_blocks=preamble,
                        article_chunks=articles,
                        today=moment,
                    )
                    relinked += 1
            await session.flush()

            changed: list[RelinkChange] = []
            for instrument_id in grouped:
                after = await _primary_map(session, instrument_id)
                instrument = await session.get(LegalInstrument, instrument_id)
                title = instrument.title if instrument is not None else "?"
                for label, artifact_id in sorted(after.items()):
                    previous = before[instrument_id].get(label)
                    if previous == artifact_id:
                        continue
                    changed.append(
                        RelinkChange(
                            instrument_title=title,
                            version_label=label,
                            previous_artifact=await _filename(session, previous),
                            adopted_artifact=await _filename(session, artifact_id) or "?",
                        )
                    )

            created = await _labels(
                session, set(await session.scalars(select(LegalVersion.id))) - before_ids
            )
            report = RelinkReport(
                instruments=len(grouped),
                candidates=candidates,
                relinked=relinked,
                skipped=tuple(sorted(skipped)),
                changed=tuple(changed),
                created_versions=created,
            )
            if dry_run:
                raise _DryRun(report)
    except _DryRun as probe:
        return probe.report
    return report


async def _candidates(
    session: AsyncSession, instrument_ids: set[UUID] | None
) -> list[tuple[UUID, SourceArtifact]]:
    """回溯「(本体, 原件)」候选：审计事件里记过挂接的原件，本体与原件都还在。"""
    wanted = {str(item) for item in instrument_ids} if instrument_ids else None
    payloads = list(
        await session.scalars(select(AuditEvent.payload).where(AuditEvent.action == LINK_ACTION))
    )
    pairs: list[tuple[UUID, UUID]] = []
    for payload in payloads:
        instrument = payload.get("instrument_id")
        document = payload.get("document_id")
        if not instrument or not document:
            continue
        if wanted is not None and instrument not in wanted:
            continue
        pairs.append((UUID(instrument), UUID(document)))
    if not pairs:
        return []

    alive = set(
        await session.scalars(
            select(LegalInstrument.id).where(LegalInstrument.id.in_({item for item, _ in pairs}))
        )
    )
    candidates: list[tuple[UUID, SourceArtifact]] = []
    seen: set[tuple[UUID, UUID]] = set()
    for instrument_id, document_id in pairs:
        if instrument_id not in alive or (instrument_id, document_id) in seen:
            continue
        seen.add((instrument_id, document_id))
        artifact = await session.get(SourceArtifact, document_id)
        if artifact is not None:
            candidates.append((instrument_id, artifact))
    return candidates


async def _primary_map(session: AsyncSession, instrument_id: UUID) -> dict[str, UUID | None]:
    """该本体下「版本标识 → 主原件 ID」。"""
    versions = await session.scalars(
        select(LegalVersion).where(LegalVersion.instrument_id == instrument_id)
    )
    return {version.version_label: version.artifact_id for version in versions}


async def _filename(session: AsyncSession, artifact_id: UUID | None) -> str | None:
    if artifact_id is None:
        return None
    artifact = await session.get(SourceArtifact, artifact_id)
    return artifact.original_filename if artifact is not None else None


async def _labels(session: AsyncSession, version_ids: set[UUID]) -> tuple[str, ...]:
    if not version_ids:
        return ()
    labels = await session.scalars(
        select(LegalVersion.version_label).where(LegalVersion.id.in_(version_ids))
    )
    return tuple(sorted(labels))
