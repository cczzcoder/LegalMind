"""ORM 模型。按域拆成子模块，**对外仍走 `app.models` 这一个入口**。"""

# 数据约束策略（设计 5.3「用明确的数据约束防止重复版本和孤立引用」）：
# "人"的引用列（created_by / author_id / granted_by / actor_id）一律建外键，ondelete 统一
# RESTRICT——不隐式级联删除，物理删除交由设计 15.3 的显式删除流程。
# 组织字段只出现在用户私有数据表上（设计 21.2）；公共法律数据表全局共享，不设组织字段。
# 例外：resource_id 是多态引用（access_grants.resource_id、audit_events.resource_id），
# 无法建外键，由应用层保证一致性。

# ⚠️ 子模块之间**不互相 import**，全靠本文件汇总；SQLAlchemy 的 `relationship("类名")`
# 是字符串、运行期才经 registry 解析，所以**导入顺序不影响结果**（下面按字母序排）。
# 唯一要注意的是：本文件必须把**全部**模型导入进来，否则 `Base.metadata` 会缺表
# （`alembic` 会以为要删表）。
from app.models.answering import (
    ANSWER_RUN_STATES,
    ANSWER_RUN_TERMINAL_STATES,
    ANSWER_RUN_TRANSITIONS,
    AnswerRun,
)
from app.models.audit import (
    AuditEvent,
    OutboxEvent,
)
from app.models.base import Base, _in
from app.models.corpus import (
    APPLICABILITY_STATUSES,
    LEGAL_INSTRUMENT_TYPES,
    LEGAL_STATUSES,
    LEGAL_VERSION_REVIEW_STATUSES,
    PARSE_QUALITY_STATUSES,
    PROVISION_RELATION_TYPES,
    PROVISION_TYPES,
    ApplicabilityRecord,
    Chunk,
    ChunkSpan,
    LegalInstrument,
    LegalVersion,
    ParseRevision,
    ProvisionEmbedding,
    ProvisionIdentity,
    ProvisionRelation,
    ProvisionVersion,
)
from app.models.identity import (
    ROLE_NAMES,
    AccessGrant,
    AuthSession,
    LoginAttempt,
    MfaRecoveryCode,
    Organization,
    User,
    UserRole,
)
from app.models.jobs import (
    JOB_STATUSES,
    Job,
)
from app.models.redaction import (
    REDACTION_ENTITY_TYPES,
    RedactionEntity,
)
from app.models.sources import (
    SENSITIVITY_LEVELS,
    SOURCE_TYPES,
    TRUST_LEVELS,
    Source,
    SourceArtifact,
)
from app.models.wiki import (
    WIKI_REVISION_STATUSES,
    WikiPage,
    WikiRevision,
    WikiRevisionCitation,
)

__all__ = [
    "ANSWER_RUN_STATES",
    "ANSWER_RUN_TERMINAL_STATES",
    "ANSWER_RUN_TRANSITIONS",
    "APPLICABILITY_STATUSES",
    "JOB_STATUSES",
    "LEGAL_INSTRUMENT_TYPES",
    "LEGAL_STATUSES",
    "LEGAL_VERSION_REVIEW_STATUSES",
    "PARSE_QUALITY_STATUSES",
    "PROVISION_RELATION_TYPES",
    "PROVISION_TYPES",
    "REDACTION_ENTITY_TYPES",
    "ROLE_NAMES",
    "SENSITIVITY_LEVELS",
    "SOURCE_TYPES",
    "TRUST_LEVELS",
    "WIKI_REVISION_STATUSES",
    "AccessGrant",
    "AnswerRun",
    "ApplicabilityRecord",
    "AuditEvent",
    "AuthSession",
    "Base",
    "Chunk",
    "ChunkSpan",
    "Job",
    "LegalInstrument",
    "LegalVersion",
    "LoginAttempt",
    "MfaRecoveryCode",
    "Organization",
    "OutboxEvent",
    "ParseRevision",
    "ProvisionEmbedding",
    "ProvisionIdentity",
    "ProvisionRelation",
    "ProvisionVersion",
    "RedactionEntity",
    "Source",
    "SourceArtifact",
    "User",
    "UserRole",
    "WikiPage",
    "WikiRevision",
    "WikiRevisionCitation",
    "_in",
]
