"""问答运行记录（设计 §9.1、§9.4、§9.3 第三层、§21.2）。

三件事逼出这张表：

- **§9.1 的执行状态需要显式承载**。设计原话是「状态定义、转移条件、每步授权检查和核验门禁
  **由本系统显式声明**」。在此之前这些状态只存在于 `answer_question` 的分支里——跑完就没了，
  看不出一次运行停在哪个状态、为什么停。
- **§9.4 要求「问答记录保存证据和配置快照」**：复核时要能还原「当时是怎么跑的」。
- **§9.3 第三层的人工复核需要一个队列**：`NEEDS_REVIEW` 的运行得留得下来，否则「转人工」只是
  一句提示。

**只记元数据、证据引用与配置快照，不记问题与回答正文**——正文只留 sha256。这是两条约束合起来
的结果：§9.4 要留证据与配置，而 §21 要求「客户个性化问答采用临时检索，**客户数据不落库**」。
证据存的是**条款版本 ID 与效力状态**，不是条文正文（那在公共表里）。

`answer_runs` 是**用户私有数据**（§21.2），所以带 `organization_id`——与公共法律数据表不同。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

#: 与 `app.models.ANSWER_RUN_STATES` 一致。**迁移里写死一份**是故意的：迁移是历史事实，
#: 不该跟着应用常量变——以后加状态要新写迁移。
_STATES = (
    "CREATED",
    "CLARIFYING",
    "RETRIEVING",
    "RERANKING",
    "ASSEMBLING_EVIDENCE",
    "GENERATING",
    "VERIFYING",
    "ANSWERED",
    "PARTIAL",
    "NEEDS_REVIEW",
    "INSUFFICIENT_EVIDENCE",
    "FAILED",
)


def upgrade():
    op.create_table(
        "answer_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("previous_state", sa.String(30), nullable=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("question_sha256", sa.String(64), nullable=False),
        sa.Column("answer_sha256", sa.String(64), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("blocked_by", sa.String(30), nullable=True),
        sa.Column("published", sa.Boolean(), nullable=False),
        sa.Column("seconds", sa.Float(), nullable=True),
        sa.Column("review_required", sa.Boolean(), nullable=False),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("state IN ('" + "', '".join(_STATES) + "')", name="ck_answer_run_state"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_answer_runs_organization_id", "answer_runs", ["organization_id"])
    op.create_index("ix_answer_runs_created_at", "answer_runs", ["created_at"])
    # 待审队列按（状态, 时间）取
    op.create_index("ix_answer_run_state_created", "answer_runs", ["state", "created_at"])


def downgrade():
    op.drop_index("ix_answer_run_state_created", table_name="answer_runs")
    op.drop_index("ix_answer_runs_created_at", table_name="answer_runs")
    op.drop_index("ix_answer_runs_organization_id", table_name="answer_runs")
    op.drop_table("answer_runs")
