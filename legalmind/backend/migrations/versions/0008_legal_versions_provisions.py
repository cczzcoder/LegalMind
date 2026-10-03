"""Legal instrument / version / provision identity / provision version (design 5.1, 5.2).

P3 第二片：法律版本树。LegalInstrument 是本体，LegalVersion 是其某一版本，
ProvisionIdentity 是条款的稳定身份，ProvisionVersion 是条款在某个版本下的文本。

按设计 5.3：效力日期用 Date 且未知即 NULL（不虚构）；不施加“不允许时间重叠”约束；
引用绑定到具体的 ProvisionVersion，而非“最新条款”。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

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
        "legal_instruments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("jurisdiction", sa.String(100), nullable=False),
        sa.Column("issuing_body", sa.String(200), nullable=False),
        sa.Column("instrument_type", sa.String(50), nullable=False),
        sa.Column("document_number", sa.String(200), nullable=True),
        sa.Column("stable_id", sa.String(200), nullable=True),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        _created_at(),
        sa.UniqueConstraint("organization_id", "stable_id", name="uq_legal_instrument_stable_id"),
        sa.CheckConstraint(
            _in("instrument_type", LEGAL_INSTRUMENT_TYPES), name="ck_legal_instrument_type"
        ),
    )
    op.create_index(
        "ix_legal_instruments_organization_id", "legal_instruments", ["organization_id"]
    )

    op.create_table(
        "legal_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "instrument_id",
            sa.Uuid(),
            sa.ForeignKey("legal_instruments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            sa.Uuid(),
            sa.ForeignKey("source_artifacts.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("version_label", sa.String(100), nullable=False),
        # 未知日期即 NULL，不虚构（设计 5.3）
        sa.Column("promulgated_on", sa.Date(), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("review_status", sa.String(30), nullable=False),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        _created_at(),
        sa.UniqueConstraint("instrument_id", "version_label", name="uq_legal_version_label"),
        sa.CheckConstraint(
            _in("review_status", LEGAL_VERSION_REVIEW_STATUSES),
            name="ck_legal_version_review_status",
        ),
    )
    op.create_index("ix_legal_versions_organization_id", "legal_versions", ["organization_id"])
    op.create_index("ix_legal_versions_instrument_id", "legal_versions", ["instrument_id"])
    op.create_index("ix_legal_versions_artifact_id", "legal_versions", ["artifact_id"])

    op.create_table(
        "provision_identities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "instrument_id",
            sa.Uuid(),
            sa.ForeignKey("legal_instruments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provision_type", sa.String(30), nullable=False),
        sa.Column("provision_number", sa.String(100), nullable=False),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        _created_at(),
        sa.UniqueConstraint(
            "instrument_id",
            "provision_type",
            "provision_number",
            name="uq_provision_identity",
        ),
        sa.CheckConstraint(
            _in("provision_type", PROVISION_TYPES), name="ck_provision_identity_type"
        ),
    )
    op.create_index(
        "ix_provision_identities_organization_id", "provision_identities", ["organization_id"]
    )
    op.create_index(
        "ix_provision_identities_instrument_id", "provision_identities", ["instrument_id"]
    )

    op.create_table(
        "provision_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "legal_version_id",
            sa.Uuid(),
            sa.ForeignKey("legal_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "provision_identity_id",
            sa.Uuid(),
            sa.ForeignKey("provision_identities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "chunk_id",
            sa.Uuid(),
            sa.ForeignKey("chunks.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("structure_path", JSONB(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        _created_at(),
        sa.UniqueConstraint(
            "legal_version_id",
            "provision_identity_id",
            name="uq_provision_version",
        ),
    )
    op.create_index(
        "ix_provision_versions_organization_id", "provision_versions", ["organization_id"]
    )
    op.create_index(
        "ix_provision_versions_legal_version_id", "provision_versions", ["legal_version_id"]
    )
    op.create_index(
        "ix_provision_versions_provision_identity_id",
        "provision_versions",
        ["provision_identity_id"],
    )
    op.create_index("ix_provision_versions_chunk_id", "provision_versions", ["chunk_id"])


def downgrade():
    op.drop_table("provision_versions")
    op.drop_table("provision_identities")
    op.drop_table("legal_versions")
    op.drop_table("legal_instruments")
