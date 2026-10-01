"""Initial Wiki, audit and outbox tables."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "wiki_pages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("head_revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "head_revision >= 1",
            name="ck_wiki_page_head_positive",
        ),
    )

    op.create_index(
        "ix_wiki_pages_organization_id",
        "wiki_pages",
        ["organization_id"],
    )

    op.create_table(
        "wiki_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "page_id",
            sa.Uuid(),
            sa.ForeignKey("wiki_pages.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "page_id",
            "number",
            name="uq_wiki_revision_number",
        ),
        sa.CheckConstraint(
            "number > 0",
            name="ck_wiki_revision_positive",
        ),
        sa.CheckConstraint(
            "status = 'draft'",
            name="ck_wiki_draft_only",
        ),
    )

    op.create_index(
        "ix_wiki_revisions_page_id",
        "wiki_revisions",
        ["page_id"],
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_index(
        "ix_audit_events_organization_id",
        "audit_events",
        ["organization_id"],
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "delivered_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_outbox_events_organization_id",
        "outbox_events",
        ["organization_id"],
    )


def downgrade():
    op.drop_table("outbox_events")
    op.drop_table("audit_events")
    op.drop_table("wiki_revisions")
    op.drop_table("wiki_pages")
