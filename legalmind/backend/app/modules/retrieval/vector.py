"""向量检索通路（设计 8.2、8.3、14.1）。

条款版本的向量存在 ``provision_embeddings``，**按模型分开存**（同一条款可并存多个模型的向量，
评测时直接横向比）。查询向量由本地模型现算（``app/adapters/embedding.py``）。

**默认精确最近邻**：设计 §8.3 要求「pgvector 默认执行精确最近邻搜索，近似索引按数据量和评测
结果再启用」。当前 2403 条条款，顺序扫描够用；不定长 ``vector`` 列也建不了 ANN 索引，所以没建。

**``max_distance`` 是必需的**：向量检索**总会**返回最近的 k 条——没有阈值就永远不会「查无结果」，
必须零命中的查询会稳定误报。阈值用余弦距离（``1 - 余弦相似度``）。
"""

from sqlalchemy import Select, select

from app.core.security import Principal
from app.models import (
    LegalInstrument,
    LegalVersion,
    ProvisionEmbedding,
    ProvisionIdentity,
    ProvisionVersion,
    SourceArtifact,
)
from app.modules.authorization.service import AuthorizationService

# 余弦距离上限默认值。归一化向量下 0.5 约等于余弦相似度 0.5。
# 这个值是从两份金标准一起扫出来的（见 doc/技术决策与踩坑记录.md §1.5）：
# 0.5 时问句式召回打满且误报 0，词面召回 0.974；0.6 只会多漏 1 条误报、召回不变，所以取 0.5。
DEFAULT_MAX_DISTANCE = 0.5


def statement(
    *,
    model: str,
    query_vector: list[float],
    principal: Principal,
    limit: int,
    max_distance: float | None = DEFAULT_MAX_DISTANCE,
) -> Select:
    """向量通路的查询：返回与精确/关键词通路**相同的实体列**，便于复用同一套 `_hit`。

    授权仍在数据库里复核（设计 §11.2），与另外两条通路一致。
    """
    distance = ProvisionEmbedding.embedding.cosine_distance(query_vector)
    statement = (
        select(ProvisionVersion, ProvisionIdentity, LegalVersion, LegalInstrument, SourceArtifact)
        .select_from(ProvisionEmbedding)
        .join(ProvisionVersion, ProvisionEmbedding.provision_version_id == ProvisionVersion.id)
        .join(ProvisionIdentity, ProvisionVersion.provision_identity_id == ProvisionIdentity.id)
        .join(LegalVersion, ProvisionVersion.legal_version_id == LegalVersion.id)
        .join(LegalInstrument, LegalVersion.instrument_id == LegalInstrument.id)
        .join(SourceArtifact, LegalVersion.artifact_id == SourceArtifact.id)
        .where(ProvisionEmbedding.model == model)
        .where(AuthorizationService.document_scope(principal))
        .order_by(distance)
        .limit(limit)
    )
    if max_distance is not None:
        statement = statement.where(distance <= max_distance)
    return statement
