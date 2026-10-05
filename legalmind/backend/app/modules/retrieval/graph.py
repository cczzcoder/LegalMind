"""图谱扩展检索（设计 §8.4 的概念验证：PostgreSQL 边表 + 一跳扩展）。

从**种子条款**出发，沿 ``provision_relations`` 的 ``cite`` 边取一跳邻居，并**映射回种子所在的那个
法律版本**——这样扩展出来的候选与种子同版本，证据版本明确（§8.4「绑定具体证据版本」）。

**只走出边、只走一跳**：

- 只走**出边**（本条引用谁）才是「上下文补齐」——补的是本条依赖的那一条。反过来把「谁引用了我」
  全拉进来，一个被反复引用的条款会把几十条无关条文拖进结果。
- 只走**一跳**：再往外对本条的理解没有增量，却会放大噪声。§8.4 说概念验证用 ``WITH RECURSIVE``；
  一跳就是一条 join，语义等价且更省，真需要多跳时再改递归（触发条件同 §18）。

**授权照旧在数据库里复核**（§8.4、§11.2）：扩展候选同样 join 原件套 ``document_scope``，
图存储不承载授权判断。
"""

from uuid import UUID

from sqlalchemy import Select, and_, select

from app.core.security import Principal
from app.models import (
    LegalInstrument,
    LegalVersion,
    ProvisionIdentity,
    ProvisionRelation,
    ProvisionVersion,
    SourceArtifact,
)
from app.modules.authorization.service import AuthorizationService

# 与 graph.link_citations 写入的类型一致
CITE = "cite"


def statement(
    *,
    seed_version_ids: list[UUID],
    principal: Principal,
    limit: int,
    filters: list | None = None,
) -> Select:
    """种子条款版本的**一跳引用邻居**（与种子同一法律版本），已套授权与过滤条件。"""
    seeds = (
        select(
            ProvisionVersion.provision_identity_id.label("identity_id"),
            ProvisionVersion.legal_version_id.label("legal_version_id"),
        )
        .where(ProvisionVersion.id.in_(seed_version_ids))
        .distinct()
        .subquery()
    )
    statement = (
        select(ProvisionVersion, ProvisionIdentity, LegalVersion, LegalInstrument, SourceArtifact)
        .select_from(ProvisionRelation)
        .join(seeds, ProvisionRelation.source_identity_id == seeds.c.identity_id)
        .join(ProvisionIdentity, ProvisionRelation.target_identity_id == ProvisionIdentity.id)
        .join(
            ProvisionVersion,
            and_(
                ProvisionVersion.provision_identity_id == ProvisionIdentity.id,
                # 同一法律版本——跨版本会带出别版本的文本，证据版本就不明确了
                ProvisionVersion.legal_version_id == seeds.c.legal_version_id,
            ),
        )
        .join(LegalVersion, ProvisionVersion.legal_version_id == LegalVersion.id)
        .join(LegalInstrument, LegalVersion.instrument_id == LegalInstrument.id)
        .join(SourceArtifact, LegalVersion.artifact_id == SourceArtifact.id)
        .where(ProvisionRelation.relation_type == CITE)
        .where(AuthorizationService.document_scope(principal))
        .limit(limit)
    )
    if filters:
        statement = statement.where(*filters)
    return statement
