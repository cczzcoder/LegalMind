"""Index on audit_events for MFA failure lookups.

identity/mfa.py 的 check_failures 按 actor_id + action + created_at 过滤，
审计表只增不减，缺索引会随数据量增长退化为全表扫描。
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_audit_events_actor_action_created"


def upgrade():
    op.create_index(
        INDEX_NAME,
        "audit_events",
        ["actor_id", "action", "created_at"],
    )


def downgrade():
    op.drop_index(INDEX_NAME, table_name="audit_events")
