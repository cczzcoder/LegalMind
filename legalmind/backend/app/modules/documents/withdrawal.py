"""撤下原件（设计 §15.3）。

用于来源被认定无权使用、或登记错误必须移除原件时。设计 §15.3 要求「删除流程必须覆盖**原件、
解析产物**、索引、缓存和衍生 Wiki」；本模块覆盖原件与解析产物（索引、缓存、衍生 Wiki 尚未实现）。

**必须按批次撤下**（``document_ids`` 是集合，不是一份一份调）：同一来源的多份原件常常互为
「同一版本的其他来源」，逐份执行会互相把对方当成存活原件去重建版本树，产生中间态和无谓的
``pending``。整批一起排除才是对的语义——实测宪法（2004 修正 PDF 与 2018 修正 PDF 同属一个本体，
逐份撤下时两者会互相「重建」对方）。

**删除顺序受外键 RESTRICT 约束**（迁移 0006，不隐式级联）：条款版本 → 脱敏映射 → 分块定位 →
分块 → 解析版本 → 原件。任何一步失败整体回滚。

**法律版本树不能简单跟着删**：一个版本的原件被撤下时，同一版本可能还有别的合法来源原件
（实测商标法：DOCX 来自官方库、PDF 来自第三方库，两者是同一版本）。因此流程是
「先解除引用 → 从存活原件重建 → 仍无原件的版本才删除」。

**原件字节在数据库事务提交之后才删**（与导入的「先写文件、后登记，失败则补偿删除」镜像）。
文件删除失败会抛错并提示 `object_key`，审计里也记了该键，便于运维手工清理——**残留字节比丢数据
更糟，所以宁可报错也不静默放过**。
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage import LocalFileStorage
from app.core.security import Principal
from app.models import (
    AuditEvent,
    Chunk,
    ChunkSpan,
    Job,
    LegalInstrument,
    LegalVersion,
    ParseRevision,
    ProvisionIdentity,
    ProvisionVersion,
    RedactionEntity,
    SourceArtifact,
)
from app.modules.authorization.grants import record_event
from app.modules.legal_corpus.metadata import (
    UNKNOWN_STATUS,
    extract_metadata,
    normalize_document_number,
)
from app.modules.legal_corpus.service import ArticleChunk, link_legal_version


@dataclass(frozen=True)
class WithdrawPlan:
    """单份原件的撤下内容，供 dry-run 与审计使用。"""

    document_id: UUID
    filename: str
    sha256: str
    object_key: str
    parse_revisions: int
    chunks: int
    chunk_spans: int
    redaction_entities: int


@dataclass(frozen=True)
class WithdrawReport:
    """整批撤下的结果。

    ``remove_versions`` / ``remove_instruments`` 是**最终**结果（已计入整批排除），dry-run 预演
    与真正执行给出的是同一份判断。
    """

    documents: tuple[WithdrawPlan, ...]
    detach_versions: tuple[str, ...]
    remove_versions: tuple[str, ...]
    remove_instruments: tuple[str, ...]
    relink_from: tuple[str, ...]
    relinked: int = 0

    @property
    def document_ids(self) -> tuple[UUID, ...]:
        return tuple(plan.document_id for plan in self.documents)


@dataclass(frozen=True)
class _Survivor:
    """仍存在的原件及其从库里重建的解析草稿。"""

    artifact: SourceArtifact
    preamble: list[str]
    articles: list[ArticleChunk]
    version_label: str


class _DryRun(Exception):
    """dry-run 哨兵：在事务里抛出以回滚，从而只观察不修改。"""

    def __init__(self, report: WithdrawReport):
        self.report = report


async def withdraw_documents(
    session: AsyncSession,
    principal: Principal,
    storage: LocalFileStorage,
    document_ids: list[UUID],
    *,
    reason: str,
    dry_run: bool = False,
    today: date | None = None,
) -> WithdrawReport:
    """整批撤下原件及其解析产物，并清理会变成孤儿的法律版本与条款。

    ``reason`` 必填：审计要能说明为什么撤下（§20.3 来源撤回须留痕）。
    ``dry_run=True`` 时只返回计划、不改动任何数据。
    """
    if not reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
    if not document_ids:
        raise HTTPException(status_code=422, detail="No documents given")

    wanted = list(dict.fromkeys(document_ids))
    moment = today or datetime.now(UTC).date()
    try:
        async with session.begin():
            artifacts = list(
                await session.scalars(
                    select(SourceArtifact).where(SourceArtifact.id.in_(wanted)).with_for_update()
                )
            )
            found = {artifact.id for artifact in artifacts}
            missing = [str(item) for item in wanted if item not in found]
            if missing:
                raise HTTPException(status_code=404, detail=f"Document not found: {missing}")

            plans: list[WithdrawPlan] = []
            revision_ids: list[UUID] = []
            chunk_ids: list[UUID] = []
            for artifact in artifacts:
                artifact_revisions = list(
                    await session.scalars(
                        select(ParseRevision.id).where(ParseRevision.artifact_id == artifact.id)
                    )
                )
                artifact_chunks = list(
                    await session.scalars(
                        select(Chunk.id).where(Chunk.parse_revision_id.in_(artifact_revisions))
                    )
                )
                revision_ids += artifact_revisions
                chunk_ids += artifact_chunks
                plans.append(
                    WithdrawPlan(
                        document_id=artifact.id,
                        filename=artifact.original_filename,
                        sha256=artifact.sha256,
                        object_key=artifact.object_key,
                        parse_revisions=len(artifact_revisions),
                        chunks=len(artifact_chunks),
                        chunk_spans=await _count(
                            session, ChunkSpan, ChunkSpan.chunk_id.in_(artifact_chunks)
                        ),
                        redaction_entities=await _count(
                            session,
                            RedactionEntity,
                            RedactionEntity.parse_revision_id.in_(artifact_revisions),
                        ),
                    )
                )

            versions = list(
                await session.scalars(
                    select(LegalVersion).where(LegalVersion.artifact_id.in_(wanted))
                )
            )
            instrument_ids = {version.instrument_id for version in versions}
            survivors = await _survivors(session, instrument_ids, exclude=set(wanted), today=moment)
            # 存活原件各自代表哪个版本标识——决定被解除引用的版本还能不能重建
            survivor_labels = {
                (instrument_id, survivor.version_label)
                for instrument_id, entries in survivors.items()
                for survivor in entries
            }

            report = WithdrawReport(
                documents=tuple(plans),
                detach_versions=tuple(sorted(version.version_label for version in versions)),
                remove_versions=tuple(
                    sorted(
                        version.version_label
                        for version in versions
                        if (version.instrument_id, version.version_label) not in survivor_labels
                    )
                ),
                remove_instruments=tuple(
                    sorted(await _instruments_to_remove(session, instrument_ids, survivors))
                ),
                relink_from=tuple(
                    sorted(
                        survivor.artifact.original_filename
                        for entries in survivors.values()
                        for survivor in entries
                    )
                ),
            )
            if dry_run:
                raise _DryRun(report)

            # 1) 条款版本引用分块，必须先于分块删除（RESTRICT）
            if versions:
                await session.execute(
                    delete(ProvisionVersion).where(
                        ProvisionVersion.legal_version_id.in_([v.id for v in versions])
                    )
                )
                for version in versions:
                    version.artifact_id = None
                    # 日期与效力状态可能是从**被撤下**的原件合并来的（证据合并的结果），
                    # 撤下后必须清掉，由存活原件重新提取——否则会留下无证据支撑的效力状态
                    version.promulgated_on = None
                    version.effective_from = None
                    version.legal_status = UNKNOWN_STATUS
            # 2) 解析产物：脱敏映射（含明文，最敏感）→ 定位 → 分块 → 解析版本
            await session.execute(
                delete(RedactionEntity).where(RedactionEntity.parse_revision_id.in_(revision_ids))
            )
            await session.execute(delete(ChunkSpan).where(ChunkSpan.chunk_id.in_(chunk_ids)))
            await session.execute(delete(Chunk).where(Chunk.parse_revision_id.in_(revision_ids)))
            await session.execute(delete(ParseRevision).where(ParseRevision.id.in_(revision_ids)))
            # 3) 原件
            for artifact in artifacts:
                await session.delete(artifact)
            # 3.1) 尚未开始的解析任务：任务指向的原件已不存在，留着只会以 document_missing 永久失败
            #      进人工队列（实测 6 份原件留下 6 条噪音）。只取消 pending/retry_wait——设计 §12.2
            #      规定取消不强制终止在跑任务，正在执行的会自行以 document_missing 收场。
            cancelled = await session.execute(
                update(Job)
                .where(
                    Job.status.in_(("pending", "retry_wait")),
                    Job.payload["document_id"].astext.in_([str(item) for item in wanted]),
                )
                .values(status="cancelled")
            )
            await session.flush()

            # 4) 从存活原件重建，再清理仍无原件的版本与空本体
            relinked = 0
            for instrument_id, entries in survivors.items():
                for survivor in entries:
                    await link_legal_version(
                        session,
                        principal,
                        artifact=survivor.artifact,
                        preamble_blocks=survivor.preamble,
                        article_chunks=survivor.articles,
                        today=moment,
                    )
                    await _refresh_instrument(
                        session, instrument_id, survivor.artifact, survivor.preamble, moment
                    )
                    relinked += 1

            removed_versions = 0
            for version in versions:
                await session.refresh(version)
                if version.artifact_id is not None:
                    continue
                await session.delete(version)
                removed_versions += 1
            await session.flush()

            removed_instruments = 0
            removed_identities = 0
            for instrument_id in instrument_ids:
                # 版本删掉后，只为那些版本存在的条款身份就成了孤立行（设计 §5.3 不允许孤立引用）
                orphans = list(
                    await session.scalars(
                        select(ProvisionIdentity).where(
                            ProvisionIdentity.instrument_id == instrument_id,
                            ~exists().where(
                                ProvisionVersion.provision_identity_id == ProvisionIdentity.id
                            ),
                        )
                    )
                )
                for identity in orphans:
                    await session.delete(identity)
                    removed_identities += 1
                if orphans:
                    await session.flush()
                remaining = await session.scalar(
                    select(func.count(LegalVersion.id)).where(
                        LegalVersion.instrument_id == instrument_id
                    )
                )
                if remaining:
                    continue
                instrument = await session.get(LegalInstrument, instrument_id)
                if instrument is not None:
                    await session.delete(instrument)
                removed_instruments += 1

            # 每份原件各写一条审计，逐份可追溯；批次信息一并带上
            for plan in plans:
                record_event(
                    session,
                    principal,
                    plan.document_id,
                    "document.withdrawn",
                    {
                        "document_id": str(plan.document_id),
                        "filename": plan.filename,
                        "sha256": plan.sha256,
                        # 记下对象键：文件删除失败时据此手工清理
                        "object_key": plan.object_key,
                        "reason": reason.strip(),
                        "batch_size": len(plans),
                        "parse_revisions": plan.parse_revisions,
                        "chunks": plan.chunks,
                        "chunk_spans": plan.chunk_spans,
                        "redaction_entities": plan.redaction_entities,
                        "detached_versions": list(report.detach_versions),
                        "removed_versions": removed_versions,
                        "removed_instruments": removed_instruments,
                        "removed_provision_identities": removed_identities,
                        "cancelled_jobs": cancelled.rowcount or 0,
                        "relinked_from": list(report.relink_from),
                    },
                )
            await session.flush()
    except _DryRun as probe:
        return probe.report

    failed: list[str] = []
    for plan in report.documents:
        try:
            storage.delete(plan.object_key)
        except FileNotFoundError:
            # 文件本就不在（例如已被清理）：不算失败
            pass
        except OSError:
            failed.append(plan.object_key)
    if failed:
        raise HTTPException(
            status_code=500,
            detail=f"数据库已撤下，但以下原件文件删除失败，请手工清理：{failed}",
        )
    return WithdrawReport(
        documents=report.documents,
        detach_versions=report.detach_versions,
        remove_versions=report.remove_versions,
        remove_instruments=report.remove_instruments,
        relink_from=report.relink_from,
        relinked=relinked,
    )


async def _count(session: AsyncSession, model, condition) -> int:
    return await session.scalar(select(func.count()).select_from(model).where(condition)) or 0


async def _survivors(
    session: AsyncSession, instrument_ids: set[UUID], *, exclude: set[UUID], today: date
) -> dict[UUID, list[_Survivor]]:
    """找出受影响本体下**仍然存在**的原件，并重建它们的解析草稿，供重建版本树用。

    本体与原件之间没有直接关联表（只有 ``legal_versions.artifact_id`` 指向**当前**原件），
    因此用审计事件 ``document.legal_version_linked`` 回溯曾挂到该本体的原件——这正是当初记这条
    审计的用途。不能反过来用 ``legal_versions.artifact_id`` 查本体：多原件冲突中落选的那份不是
    任何版本的原件，恰恰是需要找出来的候选。``exclude`` 是**整批**待撤下的原件。
    """
    if not instrument_ids:
        return {}
    wanted = {str(item) for item in instrument_ids}
    rows = list(
        await session.scalars(
            select(AuditEvent.payload).where(AuditEvent.action == "document.legal_version_linked")
        )
    )
    pairs: list[tuple[UUID, UUID]] = []
    for payload in rows:
        instrument = payload.get("instrument_id")
        document = payload.get("document_id")
        if instrument not in wanted or not document:
            continue
        document_id = UUID(document)
        if document_id not in exclude:
            pairs.append((UUID(instrument), document_id))

    survivors: dict[UUID, list[_Survivor]] = {}
    for instrument_id, document_id in dict.fromkeys(pairs):
        artifact = await session.get(SourceArtifact, document_id)
        if artifact is None:
            continue
        drafts = await _parse_drafts(session, document_id)
        if drafts is None:
            continue
        preamble, articles = drafts
        survivors.setdefault(instrument_id, []).append(
            _Survivor(
                artifact=artifact,
                preamble=preamble,
                articles=articles,
                version_label=extract_metadata(
                    artifact.original_filename, preamble, today=today
                ).version_label,
            )
        )
    return survivors


async def _instruments_to_remove(
    session: AsyncSession,
    instrument_ids: set[UUID],
    survivors: dict[UUID, list[_Survivor]],
) -> list[str]:
    names: list[str] = []
    for instrument_id in instrument_ids:
        if survivors.get(instrument_id):
            continue
        instrument = await session.get(LegalInstrument, instrument_id)
        if instrument is not None:
            names.append(instrument.title)
    return names


async def _parse_drafts(
    session: AsyncSession, document_id: UUID
) -> tuple[list[str], list[ArticleChunk]] | None:
    """从库里已有的解析产物重建 ``(前言块, 条款分块)``，供重新挂树。

    前言 = 正文起点之前的内容，解析时整体成一个 ``structure_path`` 为空的分块；把该分块文本
    当作单块传入，与原来的多块拼接等价（元数据提取本就先去掉全部空白）。取不到前言就无法可靠地
    重新提取元数据，宁可跳过也不猜。
    """
    revision = await session.scalar(
        select(ParseRevision)
        .where(ParseRevision.artifact_id == document_id)
        .order_by(ParseRevision.created_at.desc())
        .limit(1)
    )
    if revision is None:
        return None
    chunks = list(
        await session.scalars(
            select(Chunk).where(Chunk.parse_revision_id == revision.id).order_by(Chunk.ordinal)
        )
    )
    preamble = [chunk.text for chunk in chunks if not chunk.structure_path]
    if not preamble:
        return None
    articles = [
        ArticleChunk(chunk_id=chunk.id, structure_path=chunk.structure_path, text=chunk.text)
        for chunk in chunks
        if chunk.structure_path
    ]
    return preamble, articles


async def _refresh_instrument(
    session: AsyncSession,
    instrument_id: UUID,
    artifact: SourceArtifact,
    preamble: list[str],
    today: date,
) -> None:
    """按新的原件重算本体的元数据字段。

    ``link_legal_version`` 只会补齐空字段，但本体的文号等可能是从**被撤下**的那份原件合并来的
    （实测商标法：文号取自 PDF 的主席令），因此这里显式覆盖为存活原件提取到的值（可能是 None）。
    """
    instrument = await session.get(LegalInstrument, instrument_id)
    if instrument is None:
        return
    metadata = extract_metadata(artifact.original_filename, preamble, today=today)
    instrument.document_number = metadata.document_number
    instrument.document_number_normalized = (
        normalize_document_number(metadata.document_number) if metadata.document_number else None
    )
    instrument.issuing_body = metadata.issuing_body
    instrument.instrument_type = metadata.instrument_type
