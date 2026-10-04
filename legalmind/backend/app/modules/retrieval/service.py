"""精确字段、中文关键词与向量兜底检索（设计 §8.2、§8.3）。

**三条通路**：精确字段（名称 / 文号 / 稳定 ID / 规范化条号）、关键词（``keyword``）、
自然语言（``semantic``，级联）。

**``semantic`` 走级联，不是盲目融合**：先用关键词（含查询改写）在原文里找，命中为空才用本地嵌入
模型走向量。依据是实测（``doc/技术决策与踩坑记录.md`` §1.6）——关键词命中时准确率 1.000，向量
通路永远返回 top-k、必然夹带无关条文；RRF 融合等于把噪声加到关键词上（准确率 1.000 → 0.035），
而级联在三份金标准上都取到两边的强项。**图谱通路尚未实现。**

**授权在数据库里复核**（设计 §11.2、§8.3）：查询直接 join 到原件并套用
``AuthorizationService.document_scope``，不信任调用方提交的范围。受限原件（``restricted``）
没有授权记录就不出现在结果里，与「不存在」表现一致。

**默认屏蔽「已公布未生效」**（设计 §8.3）：把尚未生效的法律当成现行依据是最危险的错误。调用方
显式传 ``include_not_yet_effective`` 才放开（核对未来或历史版本时）。

**审核状态不过滤，但始终回传**：``pending`` 是数据质量标记，不是法律效力事实。默认按它过滤会把
确实存在、只是还没人工确认的法律整条藏起来（实测商标法与宪法正处在这个状态）。因此结果一定带
``legal_status`` 与 ``review_status``，由回答层决定能不能据此下结论（设计 §20.5）。

**日期未知不当作已知**：``effective_on`` 只排除**能证明**不在效期内的版本（施行日期晚于该日、
或失效日期不晚于该日）；施行日期未知的版本保留，但在结果里如实显示 ``unknown``。
"""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import Integer, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import embedding
from app.core.config import get_settings
from app.core.security import Principal
from app.models import (
    Chunk,
    ChunkSpan,
    LegalInstrument,
    LegalVersion,
    ProvisionIdentity,
    ProvisionVersion,
    SourceArtifact,
)
from app.modules.authorization.service import AuthorizationService
from app.modules.legal_corpus.metadata import (
    NOT_YET_EFFECTIVE,
    normalize_document_number,
    normalize_title,
)
from app.modules.legal_corpus.structure import normalize_article_number
from app.modules.retrieval import keyword, rewrite, vector
from app.modules.retrieval.schemas import (
    CitationOut,
    ProvisionHit,
    SearchQuery,
    SearchResponse,
)

# 条号是文本（「87」「101之1」），直接排序会得到 "10" < "2"。取开头数字做数值排序。
_ARTICLE_SORT = func.regexp_replace(ProvisionIdentity.provision_number, "[^0-9].*$", "", "g").cast(
    Integer
)


async def search_provisions(
    session: AsyncSession,
    principal: Principal,
    query: SearchQuery,
) -> SearchResponse:
    """按精确字段、关键词或自然语言检索条款版本（设计 §8.2、§8.3）。

    ``semantic`` 走**级联**：先用关键词（含查询改写）在原文里找，命中为空才用本地嵌入模型走向量。
    依据是实测（`doc/技术决策与踩坑记录.md` §1.6）：关键词命中时准确率 1.000，而向量通路永远返回
    top-k、必然夹带无关条文；**盲目 RRF 融合等于把这份噪声加到关键词上**（词面集准确率
    1.000 → 0.035），级联则在三份金标准上都取到两边的强项。
    """
    if query.semantic and query.keyword:
        raise HTTPException(status_code=422, detail="Give either keyword or semantic, not both")
    if query.article_number and normalize_article_number(query.article_number) is None:
        # 条号解析不出来就直接返回空结果，而不是退化成"不过滤"——静默放宽条件最危险。
        # 也不能拿哨兵值去比较：PostgreSQL 不接受字符串里的 NUL 字节。
        return SearchResponse(hits=[], truncated=False)

    filters = _filters(query)
    if query.semantic:
        async with session.begin():
            rows, truncated, spans = await _keyword_arm(session, principal, query, filters)
        path = "keyword"
        if not rows:
            # 模型调用放在事务外（设计 §12.1）；首次会下载/加载模型（约 2 GB 常驻）
            query_vector = embedding.encode(get_settings().embedding_model, [query.semantic])[0]
            async with session.begin():
                rows, truncated, spans = await _vector_arm(
                    session, principal, query, filters, query_vector
                )
            path = "vector"
    else:
        async with session.begin():
            rows, truncated, spans = await _single(session, principal, query, filters)
        path = "keyword" if query.keyword else "exact"

    hits = [
        _hit(provision, identity, version, instrument, artifact, spans.get(provision.id))
        for provision, identity, version, instrument, artifact in rows
    ]
    return SearchResponse(hits=hits, truncated=truncated, path=path)


def _filters(query: SearchQuery) -> list:
    """精确字段与过滤条件——三条通路（精确 / 关键词 / 向量）**共用同一套**，保证口径一致。"""
    conditions: list = []
    if query.instrument_title:
        conditions.append(LegalInstrument.title == normalize_title(query.instrument_title))
    if query.document_number:
        conditions.append(
            LegalInstrument.document_number_normalized
            == normalize_document_number(query.document_number)
        )
    if query.stable_id:
        conditions.append(LegalInstrument.stable_id == query.stable_id)
    if query.jurisdiction:
        conditions.append(LegalInstrument.jurisdiction == query.jurisdiction)
    if query.instrument_types:
        conditions.append(LegalInstrument.instrument_type.in_(query.instrument_types))
    if query.article_number:
        # 可解析性已在 search_provisions 里检查过
        conditions.append(
            ProvisionIdentity.provision_number == normalize_article_number(query.article_number)
        )
    if not query.include_not_yet_effective:
        conditions.append(LegalVersion.legal_status != NOT_YET_EFFECTIVE)
    if query.effective_on is not None:
        conditions += [
            or_(
                LegalVersion.effective_from.is_(None),
                LegalVersion.effective_from <= query.effective_on,
            ),
            or_(
                LegalVersion.effective_to.is_(None),
                LegalVersion.effective_to > query.effective_on,
            ),
        ]
    if query.review_statuses:
        conditions.append(LegalVersion.review_status.in_(query.review_statuses))
    return conditions


def _base_statement(principal: Principal, filters: list):
    return (
        select(ProvisionVersion, ProvisionIdentity, LegalVersion, LegalInstrument, SourceArtifact)
        .select_from(ProvisionVersion)
        .join(ProvisionIdentity, ProvisionVersion.provision_identity_id == ProvisionIdentity.id)
        .join(LegalVersion, ProvisionVersion.legal_version_id == LegalVersion.id)
        .join(LegalInstrument, LegalVersion.instrument_id == LegalInstrument.id)
        # 内连接：没有原件的版本（原件被撤下后尚未重建）不参与检索
        .join(SourceArtifact, LegalVersion.artifact_id == SourceArtifact.id)
        .where(AuthorizationService.document_scope(principal))
        .where(*filters)
    )


async def _single(session, principal, query, filters):
    statement = _base_statement(principal, filters)
    order_by: list = [LegalInstrument.title, LegalVersion.version_label, _ARTICLE_SORT]
    if query.keyword:
        # 关键词通路：方案由金标准选型（见 keyword.SELECTED），自带相关性排序，
        # 放在稳定排序之前——命中的条文越短越聚焦。
        strategy = keyword.selected()
        statement = statement.where(strategy.match(query.keyword))
        order_by = [*strategy.order(query.keyword), *order_by]
    return await _run(session, statement.order_by(*order_by), query.limit)


async def _keyword_arm(session, principal, query, filters):
    """级联第一段：关键词（含查询改写），各变体结果**取并集**、按条款去重。

    并集只可能提高召回——改写不替代原查询（见 ``rewrite.py``）。
    """
    strategy = keyword.selected()
    merged: dict = {}
    for variant in rewrite.rewrite(query.semantic).variants():
        statement = (
            _base_statement(principal, filters)
            .where(strategy.match(variant))
            .order_by(*strategy.order(variant), LegalInstrument.title, _ARTICLE_SORT)
            .limit(query.limit + 1)
        )
        for row in (await session.execute(statement)).all():
            merged.setdefault(row[0].id, row)
        if len(merged) > query.limit:
            break
    rows = list(merged.values())[: query.limit + 1]
    truncated = len(rows) > query.limit
    return await _with_spans(session, rows[: query.limit], truncated)


async def _vector_arm(session, principal, query, filters, query_vector):
    statement = vector.statement(
        model=get_settings().embedding_model,
        query_vector=query_vector,
        principal=principal,
        limit=query.limit + 1,
        filters=filters,
    )
    return await _run(session, statement, query.limit)


async def _run(session, statement, limit):
    rows = list((await session.execute(statement)).all())
    truncated = len(rows) > limit
    return await _with_spans(session, rows[:limit], truncated)


async def _with_spans(session, rows, truncated):
    spans = await _article_spans(session, [row[0] for row in rows])
    return rows, truncated, spans


async def _article_spans(
    session: AsyncSession, provisions: list[ProvisionVersion]
) -> dict[UUID, ChunkSpan]:
    """为每条条款找到承载**「条」本身**的那个 span。

    分块的文本是若干块用 ``\\n`` 连接，``chunk_spans`` 也是按块逐个记录的，所以「条标题在分块文本里
    落在第几块」就对应第几个 span。直接取首个 span 会落到段首的编/章/节标题上——实测监狱法第一条的
    首个 span 是「第一章 总 则」。

    跨边界口径：``chunk.text`` 是脱敏文本、span 偏移指向原文本（设计 §21.3）。这里用换行计数定位，
    而识别器不匹配换行符，块边界不受脱敏影响，因此两套坐标在这一步是对齐的。
    """
    chunk_ids = {provision.chunk_id for provision in provisions if provision.chunk_id is not None}
    if not chunk_ids:
        return {}
    chunks = {
        chunk.id: chunk
        for chunk in await session.scalars(select(Chunk).where(Chunk.id.in_(chunk_ids)))
    }
    grouped: dict[UUID, list[ChunkSpan]] = {}
    for span in await session.scalars(
        select(ChunkSpan)
        .where(ChunkSpan.chunk_id.in_(chunk_ids))
        .order_by(ChunkSpan.chunk_id, ChunkSpan.ordinal)
    ):
        grouped.setdefault(span.chunk_id, []).append(span)

    found: dict[UUID, ChunkSpan] = {}
    for provision in provisions:
        chunk = chunks.get(provision.chunk_id)
        candidates = grouped.get(provision.chunk_id, [])
        if chunk is None or not candidates:
            continue
        index = 0
        label = (provision.structure_path or {}).get("article")
        if label:
            offset = chunk.text.find(label)
            if offset > 0:
                index = chunk.text.count("\n", 0, offset)
        # ordinal 从 0 连续编号，正常情况下 index 就是它；对不上时退回首个 span
        found[provision.id] = next(
            (span for span in candidates if span.ordinal == index), candidates[0]
        )
    return found


def _hit(
    provision: ProvisionVersion,
    identity: ProvisionIdentity,
    version: LegalVersion,
    instrument: LegalInstrument,
    artifact: SourceArtifact,
    span: ChunkSpan | None,
) -> ProvisionHit:
    structure_path = provision.structure_path or {}
    return ProvisionHit(
        instrument_title=instrument.title,
        instrument_type=instrument.instrument_type,
        jurisdiction=instrument.jurisdiction,
        issuing_body=instrument.issuing_body,
        document_number=instrument.document_number,
        version_label=version.version_label,
        legal_status=version.legal_status,
        review_status=version.review_status,
        promulgated_on=version.promulgated_on,
        effective_from=version.effective_from,
        effective_to=version.effective_to,
        provision_type=identity.provision_type,
        provision_number=identity.provision_number,
        provision_display=structure_path.get("article"),
        text=provision.text,
        text_sha256=provision.text_sha256,
        citation=CitationOut(
            instrument_id=instrument.id,
            legal_version_id=version.id,
            provision_version_id=provision.id,
            provision_identity_id=identity.id,
            artifact_id=artifact.id,
            chunk_id=provision.chunk_id,
            page_index=span.page_index if span else None,
            printed_page_label=span.printed_page_label if span else None,
            char_start=span.char_start if span else None,
            char_end=span.char_end if span else None,
        ),
    )
