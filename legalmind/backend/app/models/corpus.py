"""法律本体、版本、条款与关系（设计 §5、§8.4）。"""

from datetime import date, datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, _in

PARSE_QUALITY_STATUSES = ("pending", "ok", "needs_review", "rejected")


class ParseRevision(Base):
    """解析版本（设计 5.1、7）。不可变：重新解析生成新版本，不覆盖旧版本。"""

    __tablename__ = "parse_revisions"
    __table_args__ = (
        # 同一原件 + 同一解析配置只允许一个解析版本；解析器版本变化时新建行
        UniqueConstraint(
            "artifact_id",
            "parser",
            "parser_version",
            "config_version",
            name="uq_parse_revision_config",
        ),
        CheckConstraint(
            _in("quality_status", PARSE_QUALITY_STATUSES),
            name="ck_parse_revision_quality_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    artifact_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_artifacts.id", ondelete="RESTRICT"),
        index=True,
    )
    parser: Mapped[str] = mapped_column(String(100))
    parser_version: Mapped[str] = mapped_column(String(50))
    # 解析处理配置版本，与 documents/service.PARSE_CONFIG_VERSION 对应
    config_version: Mapped[str] = mapped_column(String(50))
    # 规范化文本的 SHA-256；char_start/char_end 相对该文本
    text_sha256: Mapped[str] = mapped_column(String(64))
    quality_status: Mapped[str] = mapped_column(String(20), default="pending")
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class Chunk(Base):
    """解析文本分块（设计 5.1）。"""

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("parse_revision_id", "ordinal", name="uq_chunk_ordinal"),
        CheckConstraint("ordinal >= 0", name="ck_chunk_ordinal_nonnegative"),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    parse_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("parse_revisions.id", ondelete="RESTRICT"),
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    # 结构范围（章/节/条路径）；法律结构映射待法律版本切片定型
    # none_as_null：JSONB 默认把 Python None 存成 JSON null（不是 SQL NULL），会让
    # "IS NULL" 判断失效；这里显式存 SQL NULL，语义才是"未知"
    structure_path: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    text_sha256: Mapped[str] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class ChunkSpan(Base):
    """分块在原件中的定位（设计 6）。跨页分块对应多个 span。

    设计 6 的定位 JSON 是序列化形态；此处做规范化，artifact_id / artifact_sha256 /
    parse_revision_id 沿 chunk -> parse_revision 推导，不重复存储。
    """

    __tablename__ = "chunk_spans"
    __table_args__ = (
        UniqueConstraint("chunk_id", "ordinal", name="uq_chunk_span_ordinal"),
        CheckConstraint("ordinal >= 0", name="ck_chunk_span_ordinal_nonnegative"),
        CheckConstraint("char_start >= 0", name="ck_chunk_span_char_start_nonnegative"),
        CheckConstraint("char_end > char_start", name="ck_chunk_span_char_range"),
        # 坐标系与 bbox 必须同时有或同时无
        CheckConstraint(
            "(coordinate_system IS NULL) = (bbox IS NULL)",
            name="ck_chunk_span_bbox_pair",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    chunk_id: Mapped[UUID] = mapped_column(
        ForeignKey("chunks.id", ondelete="RESTRICT"),
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    # 原件零基页序号；无页概念的格式（如 HTML）为空
    page_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 印刷页码独立保存，不与文件页序号混用
    printed_page_label: Mapped[str | None] = mapped_column(String(50), nullable=True)
    block_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 相对解析版本规范化文本的 Unicode 码点偏移
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    coordinate_system: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # none_as_null：与 ck_chunk_span_bbox_pair 约束一致，无 bbox 时存 SQL NULL 而非 JSON null
    bbox: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    text_sha256: Mapped[str] = mapped_column(String(64))


# 资料类型；暂定，随来源清单与数据分级确认后调整（需求 2.2）
LEGAL_INSTRUMENT_TYPES = (
    "constitution",
    "law",
    "administrative_regulation",
    "judicial_interpretation",
    "local_regulation",
    "department_rule",
    "other",
)


# 条款层级：编/章/节/条/款/项/目
PROVISION_TYPES = ("part", "chapter", "section", "article", "paragraph", "item", "subitem")


LEGAL_VERSION_REVIEW_STATUSES = ("pending", "approved", "rejected")


# 版本效力状态（设计 5.1、8.3）。unknown 表示无法判定，不虚构（设计 5.3）；
# 检索默认屏蔽 not_yet_effective（设计 8.3）。
LEGAL_STATUSES = ("effective", "not_yet_effective", "repealed", "unknown")


class LegalInstrument(Base):
    """法律法规本体（设计 5.1）。稳定身份跨版本不变。"""

    __tablename__ = "legal_instruments"
    __table_args__ = (
        # 公共法律数据全局共享（设计 21.2）；稳定 ID 全库唯一
        UniqueConstraint("stable_id", name="uq_legal_instrument_stable_id"),
        # 可推导的业务身份：法域 + 名称（规范化后）。stable_id 依赖外部权威库，多为 NULL，
        # 而 PostgreSQL 的唯一约束允许多个 NULL，故不能靠它去重（设计 8.3 的规范化字段）。
        UniqueConstraint("jurisdiction", "title", name="uq_legal_instrument_title"),
        CheckConstraint(
            _in("instrument_type", LEGAL_INSTRUMENT_TYPES),
            name="ck_legal_instrument_type",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    # 规范化后的名称（去《》、去空白）；身份键的一部分
    title: Mapped[str] = mapped_column(String(500))
    # 法域
    jurisdiction: Mapped[str] = mapped_column(String(100))
    # 制定机关
    issuing_body: Mapped[str] = mapped_column(String(200))
    instrument_type: Mapped[str] = mapped_column(String(50))
    # 文号，原样保存供展示；精确匹配用下面的规范化字段（设计 8.3）
    document_number: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 规范化文号（去空白、全角转半角、「第X号」中文数字转阿拉伯数字），精确匹配用
    document_number_normalized: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
        index=True,
    )
    # 外部权威稳定标识
    stable_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class LegalVersion(Base):
    """法律版本（设计 5.1、5.2）。

    效力日期用 Date 并按法律日期处理；未知即 NULL，不虚构（设计 5.3）。
    不施加“不允许时间重叠”约束——复杂适用关系由 ApplicabilityRecord 表达（设计 5.3）。
    """

    __tablename__ = "legal_versions"
    __table_args__ = (
        UniqueConstraint("instrument_id", "version_label", name="uq_legal_version_label"),
        CheckConstraint(
            _in("review_status", LEGAL_VERSION_REVIEW_STATUSES),
            name="ck_legal_version_review_status",
        ),
        CheckConstraint(_in("legal_status", LEGAL_STATUSES), name="ck_legal_version_legal_status"),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    instrument_id: Mapped[UUID] = mapped_column(
        ForeignKey("legal_instruments.id", ondelete="RESTRICT"),
        index=True,
    )
    # 该版本对应的原件；允许先登记版本、后导入文件
    artifact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("source_artifacts.id", ondelete="RESTRICT"),
        index=True,
        nullable=True,
    )
    version_label: Mapped[str] = mapped_column(String(100))
    # 公布日期；未知为 NULL
    promulgated_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 生效日期；未知为 NULL
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 失效日期；未知为 NULL
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 效力状态：effective 当前有效 / not_yet_effective 已公布未生效 / repealed 已废止或被取代 /
    # unknown 无法判定。由公布信息中的施行日期与同日法律版本比较推导（legal_corpus/metadata）。
    legal_status: Mapped[str] = mapped_column(
        String(30), default="unknown", server_default="unknown"
    )
    review_status: Mapped[str] = mapped_column(String(30), default="pending")
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class ProvisionIdentity(Base):
    """条款稳定身份（设计 5.1、5.2）。身份跨法律版本不变，版本承载文本。"""

    __tablename__ = "provision_identities"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "provision_type",
            "provision_number",
            name="uq_provision_identity",
        ),
        CheckConstraint(_in("provision_type", PROVISION_TYPES), name="ck_provision_identity_type"),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    instrument_id: Mapped[UUID] = mapped_column(
        ForeignKey("legal_instruments.id", ondelete="RESTRICT"),
        index=True,
    )
    provision_type: Mapped[str] = mapped_column(String(30))
    # 条号，原样保存（如“第一条”）；规范化条号待设计 8.3 定型后再加
    provision_number: Mapped[str] = mapped_column(String(100))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class ProvisionVersion(Base):
    """条款在某个法律版本下的文本与结构位置（设计 5.1、5.2）。

    引用绑定到本表的具体行，而不是“最新条款”（设计 5.3）。
    """

    __tablename__ = "provision_versions"
    __table_args__ = (
        # 同一法律版本下，一个条款身份只有一份文本
        UniqueConstraint(
            "legal_version_id",
            "provision_identity_id",
            name="uq_provision_version",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    legal_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("legal_versions.id", ondelete="RESTRICT"),
        index=True,
    )
    provision_identity_id: Mapped[UUID] = mapped_column(
        ForeignKey("provision_identities.id", ondelete="RESTRICT"),
        index=True,
    )
    # 承载本条款文本的分块；用于沿 chunk_spans 解析原文定位
    chunk_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("chunks.id", ondelete="RESTRICT"),
        index=True,
        nullable=True,
    )
    # 结构路径（编/章/节/条…）；与 chunks.structure_path 同源，待法律结构定型
    structure_path: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    text_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class ProvisionEmbedding(Base):
    """条款版本的向量（设计 8.2、8.3、14.1）。

    按 ``(条款版本, 模型)`` 唯一，因此同一个条款可以并存**多个模型**的向量——评测时直接横向比，
    不必反复重算。**维度故意不写死**（不定长 ``vector`` 列 + ``dimensions`` 列自述）：
    不同模型维度不同（BGE-M3 是 1024、text2vec-base 是 768），代价是建不了 ANN 索引、
    只能精确最近邻——这正是设计 §8.3 要求的默认行为。
    """

    __tablename__ = "provision_embeddings"
    __table_args__ = (
        UniqueConstraint("provision_version_id", "model", name="uq_provision_embedding_model"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # 不单建索引：唯一键 (provision_version_id, model) 的前导列就是它
    provision_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("provision_versions.id", ondelete="RESTRICT")
    )
    # 模型标识（仓库名 + 版本，如 BAAI/bge-m3）
    model: Mapped[str] = mapped_column(String(200), index=True)
    dimensions: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(Vector())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


# 条款关系类型（设计 5.1、8.4）。统一读作“source → target”：
# supersede 替代、renumber 改号、split 拆分、merge 合并、cite 引用、
# parent 上下位（source 为上位）、defines 定义与被定义（source 定义 target）
PROVISION_RELATION_TYPES = (
    "supersede",
    "renumber",
    "split",
    "merge",
    "cite",
    "parent",
    "defines",
)


APPLICABILITY_STATUSES = ("pending", "confirmed", "rejected")


class ProvisionRelation(Base):
    """条款之间的关系（设计 5.1、8.4）。

    关系建在条款身份层：替代/改号/拆分/合并/上下位是身份之间的事实，跨法律版本存在，
    图谱扩展检索（设计 8.4）需要跨版本可用。回答层对具体版本的绑定由 Citation 负责。
    拆分或合并通过同一 source 的多行表达，天然支持多对多（设计 5.3）。
    """

    __tablename__ = "provision_relations"
    __table_args__ = (
        UniqueConstraint(
            "source_identity_id",
            "target_identity_id",
            "relation_type",
            name="uq_provision_relation",
        ),
        # 关系必须指向两个不同的条款
        CheckConstraint(
            "source_identity_id <> target_identity_id",
            name="ck_provision_relation_distinct",
        ),
        CheckConstraint(
            _in("relation_type", PROVISION_RELATION_TYPES), name="ck_provision_relation_type"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    source_identity_id: Mapped[UUID] = mapped_column(
        ForeignKey("provision_identities.id", ondelete="RESTRICT"),
        index=True,
    )
    target_identity_id: Mapped[UUID] = mapped_column(
        ForeignKey("provision_identities.id", ondelete="RESTRICT"),
        index=True,
    )
    relation_type: Mapped[str] = mapped_column(String(30))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class ApplicabilityRecord(Base):
    """条款的适用性记录（设计 5.1、5.3）。

    设计 5.3 要求“不对所有法律版本简单施加不允许时间重叠”，复杂适用关系在此单独表达。
    适用时间范围未知即 NULL，不虚构（设计 5.3）。
    """

    __tablename__ = "applicability_records"
    __table_args__ = (
        CheckConstraint(_in("status", APPLICABILITY_STATUSES), name="ck_applicability_status"),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    provision_identity_id: Mapped[UUID] = mapped_column(
        ForeignKey("provision_identities.id", ondelete="RESTRICT"),
        index=True,
    )
    # 适用范围（事项/主体/地域等）；结构化字段随需求确认后收紧
    scope: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 依据说明（依据哪条规定、哪份文件）
    basis: Mapped[str] = mapped_column(Text)
    # 适用时间范围；未知为 NULL
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # 正式结论须经人工确认（设计第 20 节）
    confirmed_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
