"""页面失效检测所需的两处记录（设计 §10.2）。

§10.2 写的是「**来源更新先标记待复核，不自动删除历史说明**」。要能判断「来源更新了」，
得先记住「审核当时看到的是什么」：

- ``wiki_revision_citations.provision_text_sha256``：**发布时**把引用条款的正文哈希快照下来。
  之后同一版本被重新解析、或换了更优原件后重建过条款文本，哈希就会不一致——「我当时审的是
  这段文字」这句话要能验证，不能只靠版本号。
- ``wiki_pages.review_due_at`` / ``review_due_reason``：待复核标记。**只标记、不动正文**——
  历史说明按 §10.2 保留，由人决定怎么改。重新发布会清掉标记（重新发布本身就是复核过）。

标记是**派生事实**，不是人工状态：`app.cli flag-stale-pages` 可重复执行，算出来的结果一致。
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "wiki_revision_citations",
        sa.Column("provision_text_sha256", sa.String(64), nullable=True),
    )
    op.add_column(
        "wiki_pages", sa.Column("review_due_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("wiki_pages", sa.Column("review_due_reason", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("wiki_pages", "review_due_reason")
    op.drop_column("wiki_pages", "review_due_at")
    op.drop_column("wiki_revision_citations", "provision_text_sha256")
