"""Source registry, original file records, job table; grants extended to documents."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def created_at():
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def upgrade():
    op.create_table(
        "sources",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("source_type", sa.String(20), nullable=False),
        sa.Column("trust_level", sa.String(20), nullable=False),
        sa.Column("url", sa.String(2000), nullable=True),
        sa.Column("publisher", sa.String(200), nullable=True),
        sa.Column("license_note", sa.Text(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        created_at(),
        sa.UniqueConstraint("organization_id", "name", name="uq_source_name"),
        sa.CheckConstraint(
            "source_type IN ('official', 'republished', 'internal')",
            name="ck_source_type",
        ),
        sa.CheckConstraint(
            "trust_level IN ('high', 'medium', 'low')",
            name="ck_source_trust_level",
        ),
    )
    op.create_index("ix_sources_organization_id", "sources", ["organization_id"])

    op.create_table(
        "source_artifacts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column(
            "source_id",
            sa.Uuid(),
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("object_key", sa.String(200), nullable=False, unique=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("sensitivity", sa.String(20), nullable=False),
        sa.Column("access_scope", sa.String(20), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        created_at(),
        sa.UniqueConstraint("organization_id", "sha256", name="uq_source_artifact_sha256"),
        sa.CheckConstraint(
            "sensitivity IN ('public', 'internal', 'confidential')",
            name="ck_artifact_sensitivity",
        ),
        sa.CheckConstraint(
            "access_scope IN ('organization', 'restricted')",
            name="ck_artifact_access_scope",
        ),
        sa.CheckConstraint(
            "sensitivity <> 'confidential' OR access_scope = 'restricted'",
            name="ck_artifact_confidential_restricted",
        ),
    )
    op.create_index(
        "ix_source_artifacts_organization_id",
        "source_artifacts",
        ["organization_id"],
    )
    op.create_index("ix_source_artifacts_source_id", "source_artifacts", ["source_id"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(50), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("progress", JSONB(), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        created_at(),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("organization_id", "idempotency_key", name="uq_job_idempotency"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled')",
            name="ck_job_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_job_attempts_nonnegative"),
    )
    op.create_index("ix_jobs_organization_id", "jobs", ["organization_id"])

    op.drop_constraint("ck_access_grant_resource_type", "access_grants", type_="check")
    op.create_check_constraint(
        "ck_access_grant_resource_type",
        "access_grants",
        "resource_type IN ('wiki_page', 'document')",
    )


def downgrade():
    # 已有文档授权时降级会因约束失败而中止，不静默删除授权记录
    op.drop_constraint("ck_access_grant_resource_type", "access_grants", type_="check")
    op.create_check_constraint(
        "ck_access_grant_resource_type",
        "access_grants",
        "resource_type IN ('wiki_page')",
    )
    op.drop_table("jobs")
    op.drop_table("source_artifacts")
    op.drop_table("sources")
