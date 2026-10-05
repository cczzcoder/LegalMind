"""Wiki 审核发布：状态机、发布指针与引用（设计 §10.2、§10.3）。

P1 只做到「草稿」——`wiki_revisions.status` 被 `ck_wiki_draft_only` 钉死成 `'draft'`（CODE_REVIEW
待办 m9 记的正是这条约束要在发布前移除）。这一步把它打开，并补上发布所需的东西：

- **状态机**：``draft`` → ``submitted`` → ``published`` / ``rejected``；``rejected`` 可以改引用后
  重新提交（``submitted``），但**改正文要新开修订**（§10.2「提交后锁定该修订」）。
- **发布指针** ``wiki_pages.published_revision``：审核通过才写，读者看的是它指向的修订（§10.2）。
- **审核痕迹** ``reviewed_by`` / ``reviewed_at`` / ``review_note``：谁在什么时候以什么理由批的或
  驳的。审计事件里也有，但审核信息是页面内容的一部分（§10.1），要能直接读出来。
- **引用** ``wiki_revision_citations``：指向具体的**条款版本**而不是「某条法律的最新版」——
  设计 §5.3 要求引用绑版本，否则原文更新后旧结论会看起来仍然有据。发布前逐条检查（§10.2
  「引用检查不通过不得发布」），读取时也用它执行 §10.3 的权限继承。
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_STATUSES = ("draft", "submitted", "published", "rejected")


def upgrade():
    op.drop_constraint("ck_wiki_draft_only", "wiki_revisions", type_="check")
    op.create_check_constraint(
        "ck_wiki_revision_status",
        "wiki_revisions",
        "status IN ('" + "', '".join(_STATUSES) + "')",
    )

    op.add_column("wiki_pages", sa.Column("published_revision", sa.Integer(), nullable=True))
    # 发布指针只能指向本页已存在的修订
    op.create_check_constraint(
        "ck_wiki_page_published_within_head",
        "wiki_pages",
        "published_revision IS NULL OR (published_revision >= 1 AND published_revision <= head_revision)",
    )

    op.add_column("wiki_revisions", sa.Column("reviewed_by", sa.Uuid(), nullable=True))
    op.add_column(
        "wiki_revisions", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("wiki_revisions", sa.Column("review_note", sa.Text(), nullable=True))
    op.create_foreign_key(
        "wiki_revisions_reviewed_by_fkey",
        "wiki_revisions",
        "users",
        ["reviewed_by"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "wiki_revision_citations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "revision_id",
            sa.Uuid(),
            sa.ForeignKey("wiki_revisions.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "provision_version_id",
            sa.Uuid(),
            sa.ForeignKey("provision_versions.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("revision_id", "provision_version_id", name="uq_wiki_citation"),
    )


def downgrade():
    op.drop_table("wiki_revision_citations")
    op.drop_constraint("wiki_revisions_reviewed_by_fkey", "wiki_revisions", type_="foreignkey")
    op.drop_column("wiki_revisions", "review_note")
    op.drop_column("wiki_revisions", "reviewed_at")
    op.drop_column("wiki_revisions", "reviewed_by")
    op.drop_constraint("ck_wiki_page_published_within_head", "wiki_pages", type_="check")
    op.drop_column("wiki_pages", "published_revision")
    op.drop_constraint("ck_wiki_revision_status", "wiki_revisions", type_="check")
    # 回滚时可能有非 draft 的修订；先归位再收紧约束，否则约束建不上
    op.execute("UPDATE wiki_revisions SET status = 'draft' WHERE status <> 'draft'")
    op.create_check_constraint("ck_wiki_draft_only", "wiki_revisions", "status = 'draft'")
