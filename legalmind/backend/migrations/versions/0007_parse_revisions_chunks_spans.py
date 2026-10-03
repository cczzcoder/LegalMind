"""Parse revisions, chunks and source spans (design 5.1, 6, 7).

原文可定位的地基：解析版本 → 分块 → 定位。解析文本更新时新建解析版本，
不静默移动既有引用（设计 6）。本迁移只建表，不含解析器实现。

设计 6 的定位 JSON 是序列化形态；表里做规范化，artifact_id / artifact_sha256 /
parse_revision_id 沿 chunk -> parse_revision 推导，不重复存储。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

PARSE_QUALITY_STATUSES = ("pending", "ok", "needs_review", "rejected")


def _created_at():
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def upgrade():
    op.create_table(
        "parse_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            sa.Uuid(),
            sa.ForeignKey("source_artifacts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("parser", sa.String(100), nullable=False),
        sa.Column("parser_version", sa.String(50), nullable=False),
        sa.Column("config_version", sa.String(50), nullable=False),
        sa.Column("text_sha256", sa.String(64), nullable=False),
        sa.Column("quality_status", sa.String(20), nullable=False),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        _created_at(),
        # 同一原件 + 同一解析配置只允许一个解析版本；解析器版本变化时新建行
        sa.UniqueConstraint(
            "artifact_id",
            "parser",
            "parser_version",
            "config_version",
            name="uq_parse_revision_config",
        ),
        sa.CheckConstraint(
            "quality_status IN ('" + "', '".join(PARSE_QUALITY_STATUSES) + "')",
            name="ck_parse_revision_quality_status",
        ),
    )
    op.create_index("ix_parse_revisions_organization_id", "parse_revisions", ["organization_id"])
    op.create_index("ix_parse_revisions_artifact_id", "parse_revisions", ["artifact_id"])

    op.create_table(
        "chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "parse_revision_id",
            sa.Uuid(),
            sa.ForeignKey("parse_revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        # 结构范围（章/节/条路径）；法律结构映射待法律版本切片定型
        sa.Column("structure_path", JSONB(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.String(64), nullable=False),
        _created_at(),
        sa.UniqueConstraint("parse_revision_id", "ordinal", name="uq_chunk_ordinal"),
        sa.CheckConstraint("ordinal >= 0", name="ck_chunk_ordinal_nonnegative"),
    )
    op.create_index("ix_chunks_organization_id", "chunks", ["organization_id"])
    op.create_index("ix_chunks_parse_revision_id", "chunks", ["parse_revision_id"])

    op.create_table(
        "chunk_spans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "chunk_id",
            sa.Uuid(),
            sa.ForeignKey("chunks.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        # 原件零基页序号；无页概念的格式（如 HTML）为空
        sa.Column("page_index", sa.Integer(), nullable=True),
        # 印刷页码独立保存，不与文件页序号混用
        sa.Column("printed_page_label", sa.String(50), nullable=True),
        sa.Column("block_id", sa.String(100), nullable=True),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("coordinate_system", sa.String(30), nullable=True),
        sa.Column("bbox", JSONB(), nullable=True),
        sa.Column("text_sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint("chunk_id", "ordinal", name="uq_chunk_span_ordinal"),
        sa.CheckConstraint("ordinal >= 0", name="ck_chunk_span_ordinal_nonnegative"),
        sa.CheckConstraint("char_start >= 0", name="ck_chunk_span_char_start_nonnegative"),
        sa.CheckConstraint("char_end > char_start", name="ck_chunk_span_char_range"),
        # 坐标系与 bbox 必须同时有或同时无
        sa.CheckConstraint(
            "(coordinate_system IS NULL) = (bbox IS NULL)",
            name="ck_chunk_span_bbox_pair",
        ),
    )
    op.create_index("ix_chunk_spans_chunk_id", "chunk_spans", ["chunk_id"])


def downgrade():
    op.drop_table("chunk_spans")
    op.drop_table("chunks")
    op.drop_table("parse_revisions")
