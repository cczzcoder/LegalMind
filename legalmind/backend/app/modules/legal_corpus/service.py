"""法律版本树落库（设计 §5.1、§5.2、§7、§8.3）。

把一次解析的结果挂到法律版本树上：

```text
LegalInstrument（法域 + 名称）
  ├─ LegalVersion（版本标识、公布/生效日期、效力状态、审核状态）
  │    └─ ProvisionVersion（条的脱敏文本 + 结构路径 + 承载分块）
  └─ ProvisionIdentity（条号稳定身份，跨版本共享）
```

**条款身份只为「条」建**：实测条号全文连续，而章号/节号随上级重置，无法满足
``provision_identities`` 的唯一键（设计 §5.2）；编/章/节只作结构路径。

**本函数不自己开事务**——与 ``redaction.service.persist_redaction`` 同理，由 ``parse_artifact``
在同一个事务里调用，保证版本树、分块、审计与 Outbox 原子提交（设计 §12.1）。

**质量门禁**（设计 §7「版本关联确认」是人工环节，这里只做自动建议）：

- 正文提取完整、无回退、无多原件冲突 → ``review_status='approved'``，效力状态照实写。
- 版本标识取自文件名回退/未标注、或标题退化为文件名、或出现多原件冲突 → ``review_status='pending'``
  并写审计事件，交人工确认。

**多原件同版本**：同一 ``(法, 版本标识)`` 对应多个原件时按「效力状态 > 公布日期 >
docx 优于 pdf > 导入时间」排序，**排序更优者成为该版本的原件**并据此重建条款文本；同时置待审核并写冲突审计。
``legal_versions.legal_status`` 会做证据合并——任一原件给出确定状态即可覆盖 ``unknown``，
这样「已公布未生效」的版本不会因为另一份缺施行日期的原件而被误判为有效（设计 §8.3）。
"""

from dataclasses import dataclass
from datetime import date
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.models import (
    Chunk,
    LegalInstrument,
    LegalVersion,
    ParseRevision,
    ProvisionIdentity,
    ProvisionVersion,
    SourceArtifact,
)
from app.modules.authorization.grants import record_event
from app.modules.legal_corpus.metadata import (
    EFFECTIVE,
    REPEALED,
    STATUS_RANK,
    UNKNOWN_STATUS,
    LegalMetadata,
    extract_metadata,
    normalize_document_number,
)
from app.modules.parsing.chunking import text_sha256

# 与 models.PROVISION_TYPES 一致；第一版只为「条」建身份（设计 §5.2）
ARTICLE = "article"

# 解析准确性优先：同状态、同日期时 docx 稳定优于 pdf（设计 §8.3 的排序规则）
_MEDIA_RANK = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": 0,
    "application/pdf": 1,
}
_MEDIA_RANK_FALLBACK = 9


@dataclass(frozen=True)
class ArticleChunk:
    """承载某个「条」的一个**已入库**分块。

    ``text`` 是入库的**脱敏文本**（设计 §21.3），``structure_path`` 是该条的结构路径
    （``chunks.structure_path`` 同源）。
    """

    chunk_id: UUID
    structure_path: dict | None
    text: str


@dataclass(frozen=True)
class LinkingResult:
    """落库结果，供审计与测试断言。"""

    instrument_id: UUID
    legal_version_id: UUID
    created_version: bool
    provision_count: int
    conflict: bool
    adopted_artifact: bool
    review_status: str
    low_confidence_reasons: tuple[str, ...]


def _rank(
    legal_status: str,
    published_on: date | None,
    media_type: str,
    imported_at,
) -> tuple:
    """原件排序键，越小越优：效力状态 > 公布日期 > 文件格式（docx 优于 pdf） > 导入时间。

    **文件格式排在导入时间之前是有意的**：同一来源对同一版本同时给出 docx 与 pdf 时，
    解析准确性更高的 docx 必须稳定胜出，不能因为 pdf 后导入就翻盘。导入时间只用于
    同格式原件之间的取舍——重新导入的更正版应当胜出。
    """
    return (
        STATUS_RANK.get(legal_status, _MEDIA_RANK_FALLBACK),
        -(published_on.toordinal() if published_on else 0),
        _MEDIA_RANK.get(media_type, _MEDIA_RANK_FALLBACK),
        -(imported_at.timestamp() if imported_at else 0),
    )


async def link_legal_version(
    session: AsyncSession,
    principal: Principal,
    *,
    artifact: SourceArtifact,
    preamble_blocks: list[str],
    article_chunks: list[ArticleChunk],
    today: date,
) -> LinkingResult:
    """把 ``artifact`` 的一次解析挂到法律版本树上（调用方事务内）。

    ``preamble_blocks`` 是正文起点之前的块文本；``article_chunks`` 是本解析版本里承载「条」的
    分块（按 ordinal 升序）。``today`` 显式传入以便判定效力状态、保证测试确定。
    """
    metadata = extract_metadata(artifact.original_filename, preamble_blocks, today=today)
    instrument = await _get_or_create_instrument(session, principal, metadata)

    # 用 ON CONFLICT DO NOTHING 而不是 ORM 插入：并发重复不会抛 IntegrityError，
    # 避免被 parse_artifact 的「重复解析」异常分支误判（那条分支只认 ParseRevision 冲突）。
    inserted = await session.execute(
        pg_insert(LegalVersion)
        .values(
            id=uuid4(),
            instrument_id=instrument.id,
            artifact_id=artifact.id,
            version_label=metadata.version_label,
            promulgated_on=metadata.promulgated_on,
            effective_from=metadata.effective_from,
            legal_status=metadata.legal_status,
            review_status="approved" if metadata.confident else "pending",
            created_by=principal.user_id,
        )
        .on_conflict_do_nothing(constraint="uq_legal_version_label")
    )
    version = await session.scalar(
        select(LegalVersion).where(
            LegalVersion.instrument_id == instrument.id,
            LegalVersion.version_label == metadata.version_label,
        )
    )

    if inserted.rowcount:
        provision_count = await _sync_provisions(session, principal, version, article_chunks)
        await _repeal_superseded(session, principal, instrument, version)
        result = LinkingResult(
            instrument_id=instrument.id,
            legal_version_id=version.id,
            created_version=True,
            provision_count=provision_count,
            conflict=False,
            adopted_artifact=True,
            review_status=version.review_status,
            low_confidence_reasons=metadata.low_confidence_reasons,
        )
    else:
        result = await _link_existing(
            session, principal, instrument, version, artifact, metadata, article_chunks
        )

    record_event(
        session,
        principal,
        artifact.id,
        "document.legal_version_linked",
        {
            "document_id": str(artifact.id),
            "instrument_id": str(result.instrument_id),
            "legal_version_id": str(result.legal_version_id),
            "title": metadata.title,
            "instrument_type": metadata.instrument_type,
            "version_label": metadata.version_label,
            "promulgated_on": _iso(metadata.promulgated_on),
            "effective_from": _iso(metadata.effective_from),
            "legal_status": version.legal_status,
            "review_status": result.review_status,
            "created_version": result.created_version,
            "provision_count": result.provision_count,
            "conflict": result.conflict,
            "low_confidence_reasons": list(result.low_confidence_reasons),
        },
    )
    await session.flush()
    return result


async def parse_drafts(
    session: AsyncSession, document_id: UUID
) -> tuple[list[str], list[ArticleChunk]] | None:
    """从库里已有的解析产物重建 ``(前言块, 条款分块)``，供重新挂树（设计 §7）。

    前言 = 正文起点之前的内容，解析时整体成一个 ``structure_path`` 为空的分块；把该分块文本
    当作单块传入，与原来的多块拼接等价（元数据提取本就先去掉全部空白）。取不到前言就无法可靠地
    重新提取元数据，宁可跳过也不猜。

    **不重新解析原件**：只读 ``parse_revisions`` / ``chunks``，因此不产生新的解析版本，
    也不动原文定位与脱敏映射。撤下原件与按新规则重挂版本树都用它。
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


async def _link_existing(
    session: AsyncSession,
    principal: Principal,
    instrument: LegalInstrument,
    version: LegalVersion,
    artifact: SourceArtifact,
    metadata: LegalMetadata,
    article_chunks: list[ArticleChunk],
) -> LinkingResult:
    """该版本已存在：做证据合并、多原件排序与待审核标记。"""
    previous_artifact_id = version.artifact_id

    if previous_artifact_id == artifact.id or previous_artifact_id is None:
        # 同一原件的再次解析（解析配置变化），或该版本此前没有原件（原件被撤下后重建、
        # 或先登记版本后导入文件）：都不是「多原件冲突」，直接把条款文本指向本解析版本的分块。
        _merge_evidence(version, metadata)
        version.artifact_id = artifact.id
        provision_count = await _sync_provisions(session, principal, version, article_chunks)
        await session.flush()
        return LinkingResult(
            instrument_id=instrument.id,
            legal_version_id=version.id,
            created_version=False,
            provision_count=provision_count,
            conflict=False,
            adopted_artifact=True,
            review_status=version.review_status,
            low_confidence_reasons=metadata.low_confidence_reasons,
        )

    # 排序必须在证据合并**之前**算，否则 version 的公布日期已被本次元数据填平，日期轴失去区分度
    current = (
        await session.get(SourceArtifact, previous_artifact_id)
        if previous_artifact_id is not None
        else None
    )
    arriving_rank = _rank(
        metadata.legal_status, metadata.promulgated_on, artifact.media_type, artifact.created_at
    )
    existing_rank = (
        _rank(
            version.legal_status,
            version.promulgated_on,
            current.media_type,
            current.created_at,
        )
        if current is not None
        else None
    )
    adopted = existing_rank is None or arriving_rank < existing_rank
    previous_status = version.legal_status
    _merge_evidence(version, metadata)

    if adopted:
        version.artifact_id = artifact.id
        provision_count = await _sync_provisions(session, principal, version, article_chunks)
    else:
        provision_count = await session.scalar(_count_provisions(version.id)) or 0

    # 同一来源提供的两种格式（如官方库同时给 DOCX 与 PDF）**不是冲突**：同一来源对同一版本背书，
    # 按排序择优即可（docx 优先）。只有来源不同才是需要人工确认的冲突——官方库 DOCX 与第三方库
    # PDF 指向同一版本时，两者的授权依据不同，必须让人确认以哪份为准。
    if current is not None and current.source_id == artifact.source_id:
        await session.flush()
        return LinkingResult(
            instrument_id=instrument.id,
            legal_version_id=version.id,
            created_version=False,
            provision_count=provision_count,
            conflict=False,
            adopted_artifact=adopted,
            review_status=version.review_status,
            low_confidence_reasons=metadata.low_confidence_reasons,
        )

    # 多原件同版本（来源不同）属「冲突」：降级为待审核，并写审计供人工确认（设计 §7）
    version.review_status = "pending"
    record_event(
        session,
        principal,
        artifact.id,
        "legal_version.artifact_conflict",
        {
            "document_id": str(artifact.id),
            "legal_version_id": str(version.id),
            "instrument_id": str(instrument.id),
            "existing_artifact_id": str(previous_artifact_id) if previous_artifact_id else None,
            "arriving_artifact_id": str(artifact.id),
            "adopted_arriving": adopted,
            "existing_legal_status": previous_status,
            "arriving_legal_status": metadata.legal_status,
        },
    )
    await session.flush()
    return LinkingResult(
        instrument_id=instrument.id,
        legal_version_id=version.id,
        created_version=False,
        provision_count=provision_count,
        conflict=True,
        adopted_artifact=adopted,
        review_status=version.review_status,
        low_confidence_reasons=(*metadata.low_confidence_reasons, "multiple_artifacts"),
    )


def _article_text(chunks: list[ArticleChunk]) -> str:
    """条款文本：从条标题起算，去掉段首的编/章/节标题。

    分块段首**刻意包含**章节标题（设计 §6，分块自带章节上下文），所以不能直接把分块文本拼起来
    当条款文本——那会得到「第一章 总 则\\n第一条 …」（实测监狱法）。条标题取自
    ``structure_path["article"]``；找不到时原样返回，宁可带上标题也不凭猜测丢正文。

    注意引用锚点仍是承载该条的分块（可能同时含标题）：分块是原文定位的单位，而分块内部的精确
    偏移要经过脱敏映射才能从脱敏文本映回原文本（设计 §21.3），本版不做。
    """
    text = "\n".join(chunk.text for chunk in chunks)
    label = (chunks[0].structure_path or {}).get("article")
    if not label:
        return text
    index = text.find(label)
    return text[index:] if index > 0 else text


def _count_provisions(version_id: UUID):
    return select(func.count(ProvisionVersion.id)).where(
        ProvisionVersion.legal_version_id == version_id
    )


def _merge_evidence(version: LegalVersion, metadata: LegalMetadata) -> None:
    """补齐空字段并让确定的效力状态覆盖 ``unknown``（不覆盖已有确定值）。"""
    if version.promulgated_on is None:
        version.promulgated_on = metadata.promulgated_on
    if version.effective_from is None:
        version.effective_from = metadata.effective_from
    if version.legal_status == UNKNOWN_STATUS and metadata.legal_status != UNKNOWN_STATUS:
        version.legal_status = metadata.legal_status


async def _get_or_create_instrument(
    session: AsyncSession, principal: Principal, metadata: LegalMetadata
) -> LegalInstrument:
    """按 ``(法域, 名称)`` 取或建本体；并发重复由唯一约束 + ON CONFLICT 兜底。"""
    instrument = await session.scalar(
        select(LegalInstrument).where(
            LegalInstrument.jurisdiction == metadata.jurisdiction,
            LegalInstrument.title == metadata.title,
        )
    )
    if instrument is None:
        await session.execute(
            pg_insert(LegalInstrument)
            .values(
                id=uuid4(),
                title=metadata.title,
                jurisdiction=metadata.jurisdiction,
                issuing_body=metadata.issuing_body,
                instrument_type=metadata.instrument_type,
                document_number=metadata.document_number,
                document_number_normalized=_normalized_number(metadata.document_number),
                created_by=principal.user_id,
            )
            .on_conflict_do_nothing(constraint="uq_legal_instrument_title")
        )
        instrument = await session.scalar(
            select(LegalInstrument).where(
                LegalInstrument.jurisdiction == metadata.jurisdiction,
                LegalInstrument.title == metadata.title,
            )
        )
    elif instrument.document_number is None and metadata.document_number is not None:
        instrument.document_number = metadata.document_number
        instrument.document_number_normalized = _normalized_number(metadata.document_number)
    return instrument


def _normalized_number(value: str | None) -> str | None:
    """规范化文号（设计 §8.3）；空值保持为空，不把 None 变成空串。"""
    return normalize_document_number(value) if value else None


async def _sync_provisions(
    session: AsyncSession,
    principal: Principal,
    version: LegalVersion,
    article_chunks: list[ArticleChunk],
) -> int:
    """按当前原件的分块重建该版本的条款文本（按条款身份 upsert，多余的删除）。

    同一条可能由多个分块承载（超长条被细分），此时 ``text`` 为这些分块按序用 ``\\n`` 连接，
    ``chunk_id`` 记**首个**分块，供沿 ``chunk_spans`` 解析原文定位。
    """
    grouped: dict[str, list[ArticleChunk]] = {}
    for chunk in article_chunks:
        number = (chunk.structure_path or {}).get("article_number")
        if number:
            grouped.setdefault(number, []).append(chunk)

    kept: list[UUID] = []
    for number, chunks in grouped.items():
        identity = await _get_or_create_identity(session, principal, version.instrument_id, number)
        text = _article_text(chunks)
        provision = await session.scalar(
            select(ProvisionVersion).where(
                ProvisionVersion.legal_version_id == version.id,
                ProvisionVersion.provision_identity_id == identity.id,
            )
        )
        if provision is None:
            provision = ProvisionVersion(
                legal_version_id=version.id,
                provision_identity_id=identity.id,
                chunk_id=chunks[0].chunk_id,
                structure_path=chunks[0].structure_path,
                text=text,
                text_sha256=text_sha256(text),
                created_by=principal.user_id,
            )
            session.add(provision)
        else:
            # 重新解析或换成另一份原件：文本与承载分块随之更新，身份不变
            provision.chunk_id = chunks[0].chunk_id
            provision.structure_path = chunks[0].structure_path
            provision.text = text
            provision.text_sha256 = text_sha256(text)
        await session.flush()
        kept.append(provision.id)

    stale_stmt = select(ProvisionVersion).where(ProvisionVersion.legal_version_id == version.id)
    if kept:
        stale_stmt = stale_stmt.where(ProvisionVersion.id.notin_(kept))
    stale = await session.scalars(stale_stmt)
    for provision in stale.all():
        await session.delete(provision)
    await session.flush()
    return len(grouped)


async def _get_or_create_identity(
    session: AsyncSession,
    principal: Principal,
    instrument_id: UUID,
    number: str,
) -> ProvisionIdentity:
    identity = await session.scalar(
        select(ProvisionIdentity).where(
            ProvisionIdentity.instrument_id == instrument_id,
            ProvisionIdentity.provision_type == ARTICLE,
            ProvisionIdentity.provision_number == number,
        )
    )
    if identity is None:
        await session.execute(
            pg_insert(ProvisionIdentity)
            .values(
                id=uuid4(),
                instrument_id=instrument_id,
                provision_type=ARTICLE,
                provision_number=number,
                created_by=principal.user_id,
            )
            .on_conflict_do_nothing(constraint="uq_provision_identity")
        )
        identity = await session.scalar(
            select(ProvisionIdentity).where(
                ProvisionIdentity.instrument_id == instrument_id,
                ProvisionIdentity.provision_type == ARTICLE,
                ProvisionIdentity.provision_number == number,
            )
        )
    return identity


async def _repeal_superseded(
    session: AsyncSession,
    principal: Principal,
    instrument: LegalInstrument,
    version: LegalVersion,
) -> None:
    """新版本已生效时，把同一法律中公布更早的**有效**版本标记为被取代。

    只处理状态确定、公布日期已知且更早的版本；状态未知或日期缺失的一律不动，不猜（设计 §5.3）。
    """
    if version.legal_status != EFFECTIVE or version.promulgated_on is None:
        return
    others = await session.scalars(
        select(LegalVersion).where(
            LegalVersion.instrument_id == instrument.id,
            LegalVersion.id != version.id,
            LegalVersion.legal_status == EFFECTIVE,
        )
    )
    for other in others.all():
        if other.promulgated_on is not None and other.promulgated_on < version.promulgated_on:
            other.legal_status = REPEALED
            record_event(
                session,
                principal,
                other.id,
                "legal_version.repealed",
                {
                    "legal_version_id": str(other.id),
                    "repealed_by_version_id": str(version.id),
                    "instrument_id": str(instrument.id),
                },
            )
    await session.flush()


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None
