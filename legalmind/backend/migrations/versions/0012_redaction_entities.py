"""Redaction mapping table (design 21.3).

脱敏映射表：原文本不落库，由脱敏文本 + 本表可逆重建。
本表是系统内最敏感的数据，访问权限严于业务数据，读写均写入审计。
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

# 与 redaction/detector.py 的 ENTITY_TYPES 一致
REDACTION_ENTITY_TYPES = ("id_number", "case_number", "phone", "person")


def upgrade():
    op.create_table(
        "redaction_entities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "parse_revision_id",
            sa.Uuid(),
            sa.ForeignKey("parse_revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(30), nullable=False),
        sa.Column("plaintext", sa.Text(), nullable=False),
        sa.Column("placeholder", sa.String(50), nullable=False),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "parse_revision_id",
            "entity_type",
            "plaintext",
            name="uq_redaction_entity_value",
        ),
        sa.UniqueConstraint(
            "parse_revision_id",
            "placeholder",
            name="uq_redaction_entity_placeholder",
        ),
        sa.CheckConstraint(
            "entity_type IN ('" + "', '".join(REDACTION_ENTITY_TYPES) + "')",
            name="ck_redaction_entity_type",
        ),
    )
    op.create_index(
        "ix_redaction_entities_parse_revision_id",
        "redaction_entities",
        ["parse_revision_id"],
    )


def downgrade():
    op.drop_table("redaction_entities")
