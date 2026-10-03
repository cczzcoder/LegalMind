"""Provision relations and applicability records (design 5.1, 5.3, 8.4).

P3 数据模型的最后两块。关系建在条款身份层：替代/改号/拆分/合并/上下位是身份之间
的事实，跨法律版本存在；回答层对具体版本的绑定由 Citation 负责（设计 5.3）。
拆分/合并通过同一 source 的多行表达，天然支持多对多。

适用性记录承载设计 5.3 所说的“复杂适用关系”——因此不对法律版本施加时间重叠约束。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

# 统一读作“source → target”；parent 的 source 为上位，defines 的 source 定义 target
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


def _created_at():
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ('" + "', '".join(values) + "')"


def upgrade():
    op.create_table(
        "provision_relations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_identity_id",
            sa.Uuid(),
            sa.ForeignKey("provision_identities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "target_identity_id",
            sa.Uuid(),
            sa.ForeignKey("provision_identities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("relation_type", sa.String(30), nullable=False),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        _created_at(),
        sa.UniqueConstraint(
            "source_identity_id",
            "target_identity_id",
            "relation_type",
            name="uq_provision_relation",
        ),
        sa.CheckConstraint(
            "source_identity_id <> target_identity_id",
            name="ck_provision_relation_distinct",
        ),
        sa.CheckConstraint(
            _in("relation_type", PROVISION_RELATION_TYPES),
            name="ck_provision_relation_type",
        ),
    )
    op.create_index(
        "ix_provision_relations_organization_id", "provision_relations", ["organization_id"]
    )
    op.create_index(
        "ix_provision_relations_source_identity_id", "provision_relations", ["source_identity_id"]
    )
    op.create_index(
        "ix_provision_relations_target_identity_id", "provision_relations", ["target_identity_id"]
    )

    op.create_table(
        "applicability_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "provision_identity_id",
            sa.Uuid(),
            sa.ForeignKey("provision_identities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # 适用范围（事项/主体/地域等）；结构化字段随需求确认后收紧
        sa.Column("scope", JSONB(), nullable=True),
        sa.Column("basis", sa.Text(), nullable=False),
        # 适用时间范围；未知即 NULL，不虚构（设计 5.3）
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "confirmed_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        _created_at(),
        sa.CheckConstraint(_in("status", APPLICABILITY_STATUSES), name="ck_applicability_status"),
    )
    op.create_index(
        "ix_applicability_records_organization_id", "applicability_records", ["organization_id"]
    )
    op.create_index(
        "ix_applicability_records_provision_identity_id",
        "applicability_records",
        ["provision_identity_id"],
    )


def downgrade():
    op.drop_table("applicability_records")
    op.drop_table("provision_relations")
