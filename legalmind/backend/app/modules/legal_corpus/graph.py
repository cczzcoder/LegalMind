"""条款引用关系的落库（设计 §8.4）。

把 ``citations.extract`` 抽到的引用解析成 ``provision_relations`` 的 ``cite`` 边。
**目标条款必须在库里存在**——找不到就丢弃并计数，不猜、不建指向不存在条款的边
（``provision_relations`` 两个方向都是外键，指向不存在的行会直接失败）。

**为什么引用可以直接入库**：§8.4 要求「不得由模型自由生成关系边」，指的是不能让模型凭语义联想
造边。引用是**正则从正文抽出来的文本事实**（条文自己写着「本法第八十七条」），每条边都带
``evidence`` 片段可人工复核，所以不属于「模型生成」。其余关系类型——替代 / 改号 / 拆分 / 合并 /
上下位 / 定义与被定义——没有这种来源，仍需人工确认，本模块不碰。

**审计按法律逐部记录**（而不是整批一条）：出问题时能定位到是哪部法律的哪次抽取写进去的。
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.models import (
    LegalInstrument,
    ProvisionIdentity,
    ProvisionRelation,
    ProvisionVersion,
)
from app.modules.authorization.grants import record_event
from app.modules.legal_corpus import citations

CITE = "cite"


@dataclass(frozen=True)
class LinkReport:
    """一次引用抽取的结果。``unresolved`` 高说明语料里引用了大量尚未入库的法律。"""

    provisions: int
    extracted: int
    linked: int
    unresolved: int
    unresolved_samples: tuple[str, ...]


async def link_citations(
    session: AsyncSession, principal: Principal, *, dry_run: bool = False
) -> LinkReport:
    """把全部条款文本里的引用抽成 ``cite`` 边；``dry_run`` 只统计不写。"""
    async with session.begin():
        instruments = {
            instrument.id: instrument.title
            for instrument in await session.scalars(select(LegalInstrument))
        }
        by_title = {title: identifier for identifier, title in instruments.items()}
        aliases = citations.aliases_of(instruments.values())
        identities = {
            (identity.instrument_id, identity.provision_number): identity.id
            for identity in await session.scalars(select(ProvisionIdentity))
        }
        rows = (
            await session.execute(
                select(
                    ProvisionIdentity.instrument_id,
                    ProvisionIdentity.provision_number,
                    ProvisionVersion.text,
                ).join(
                    ProvisionVersion,
                    ProvisionVersion.provision_identity_id == ProvisionIdentity.id,
                )
            )
        ).all()

        edges: dict[tuple[UUID, UUID], str] = {}
        extracted = 0
        unresolved: list[str] = []
        per_instrument: dict[UUID, int] = {}

        for instrument_id, number, text in rows:
            source = identities.get((instrument_id, number))
            if source is None:
                continue
            for citation in citations.extract(
                text, article_number=number, titles=instruments.values()
            ):
                extracted += 1
                if citation.alias is None:
                    target = identities.get((instrument_id, citation.target))
                else:
                    title = aliases.get(citation.alias)
                    other = by_title.get(title) if title else None
                    target = identities.get((other, citation.target)) if other else None
                if target is None or target == source:
                    unresolved.append(
                        f"{instruments.get(instrument_id, '?')} 第{number}条 → "
                        f"{citation.alias or '本法'}第{citation.target}条"
                    )
                    continue
                edges.setdefault((source, target), citation.evidence)
                per_instrument[instrument_id] = per_instrument.get(instrument_id, 0) + 1

        if dry_run:
            return LinkReport(
                provisions=len(rows),
                extracted=extracted,
                linked=len(edges),
                unresolved=len(unresolved),
                unresolved_samples=tuple(unresolved[:10]),
            )

        if edges:
            await session.execute(
                pg_insert(ProvisionRelation)
                .values(
                    [
                        {
                            "source_identity_id": source,
                            "target_identity_id": target,
                            "relation_type": CITE,
                            "created_by": principal.user_id,
                        }
                        for source, target in edges
                    ]
                )
                # 重复抽取不该抛 IntegrityError——那会被上层误判成别的问题
                .on_conflict_do_nothing(
                    index_elements=["source_identity_id", "target_identity_id", "relation_type"]
                )
            )
        for instrument_id, count in per_instrument.items():
            record_event(
                session,
                principal,
                instrument_id,
                "legal_corpus.citations_linked",
                {
                    "instrument_id": str(instrument_id),
                    "edges": count,
                    "unresolved_total": len(unresolved),
                },
            )

    return LinkReport(
        provisions=len(rows),
        extracted=extracted,
        linked=len(edges),
        unresolved=len(unresolved),
        unresolved_samples=tuple(unresolved[:10]),
    )
