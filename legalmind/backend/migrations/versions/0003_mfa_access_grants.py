"""Administrator TOTP MFA and page-level access grants."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("mfa_secret_encrypted", sa.Text(), nullable=True))
    op.add_column(
        "users",
        sa.Column("mfa_enabled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("users", sa.Column("mfa_last_timecode", sa.BigInteger(), nullable=True))
    op.add_column(
        "auth_sessions",
        sa.Column("mfa_verified_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "mfa_recovery_codes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_mfa_recovery_codes_user_id",
        "mfa_recovery_codes",
        ["user_id"],
    )

    # 既有页面保持组织内可见，与迁移前行为一致
    op.add_column(
        "wiki_pages",
        sa.Column(
            "access_scope",
            sa.String(20),
            server_default="organization",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_wiki_page_access_scope",
        "wiki_pages",
        "access_scope IN ('organization', 'restricted')",
    )

    op.create_table(
        "access_grants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("resource_type", sa.String(30), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("granted_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "resource_type",
            "resource_id",
            "user_id",
            name="uq_access_grant",
        ),
        sa.CheckConstraint(
            "resource_type IN ('wiki_page')",
            name="ck_access_grant_resource_type",
        ),
    )
    op.create_index(
        "ix_access_grants_organization_id",
        "access_grants",
        ["organization_id"],
    )
    op.create_index(
        "ix_access_grants_user_id",
        "access_grants",
        ["user_id"],
    )


def downgrade():
    op.drop_table("access_grants")
    op.drop_constraint("ck_wiki_page_access_scope", "wiki_pages", type_="check")
    op.drop_column("wiki_pages", "access_scope")
    op.drop_table("mfa_recovery_codes")
    op.drop_column("auth_sessions", "mfa_verified_at")
    op.drop_column("users", "mfa_last_timecode")
    op.drop_column("users", "mfa_enabled_at")
    op.drop_column("users", "mfa_secret_encrypted")
