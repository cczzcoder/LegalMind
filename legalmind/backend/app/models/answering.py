"""问答运行记录与状态机（设计 §9.1、§9.4、§21.2）。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, _in

#: 问答运行状态（设计 §9.1）。**终态**之后不再转移。
ANSWER_RUN_STATES = (
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


#: 终态：一次运行到这些状态就结束了。
ANSWER_RUN_TERMINAL_STATES = (
    "ANSWERED",
    "PARTIAL",
    "NEEDS_REVIEW",
    "INSUFFICIENT_EVIDENCE",
    "FAILED",
)


#: **显式声明**的转移表（设计 §9.1「状态定义、转移条件、每步授权检查和核验门禁由本系统显式
#: 声明」）。不在这里的转移就是非法的——落库前逐条校验，避免出现「怎么走到这个状态的」。
#:
#: ⚠️ `CLARIFYING` 与 `RERANKING` 目前是**保留状态**：反问（§9.1 的澄清分支）与重排序都还没
#: 接进链路，但转移表先把位置留出来，接入时不必改表。
ANSWER_RUN_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "CREATED": ("CLARIFYING", "RETRIEVING"),
    "CLARIFYING": ("RETRIEVING", "NEEDS_REVIEW", "FAILED"),
    # `RETRIEVING → ASSEMBLING_EVIDENCE` 是**故意跳过后面的 RERANKING**：重排序有适配层与
    # 评测开关，但实测救不了融合、且用弱模型重排会把排序搞坏，**没进默认**（§17.2、V1.13）。
    "RETRIEVING": (
        "RERANKING",
        "ASSEMBLING_EVIDENCE",
        "INSUFFICIENT_EVIDENCE",
        "NEEDS_REVIEW",
        "FAILED",
    ),
    "RERANKING": ("ASSEMBLING_EVIDENCE", "FAILED"),
    "ASSEMBLING_EVIDENCE": ("GENERATING", "NEEDS_REVIEW", "FAILED"),
    # 输出不合约定（§9.2）时不是「失败」而是「有草稿但需人工」——所以 GENERATING 能直接进待审
    "GENERATING": ("VERIFYING", "NEEDS_REVIEW", "FAILED"),
    "VERIFYING": ("ANSWERED", "PARTIAL", "NEEDS_REVIEW", "FAILED"),
}


class AnswerRun(Base):
    """一次问答运行的记录（设计 §9.1、§9.4、§21）。

    **只记元数据、证据引用与配置快照，不记问题与回答正文**——正文只留 sha256。这是两条约束
    合起来的结果：§9.4 要求「问答记录保存证据和配置快照」，§21 要求「客户个性化问答采用临时
    检索，客户数据不落库」。证据存的是**条款版本 ID 与效力状态**，不是条文正文（那在公共表里）。
    """

    __tablename__ = "answer_runs"
    __table_args__ = (
        CheckConstraint(_in("state", ANSWER_RUN_STATES), name="ck_answer_run_state"),
        # 待审队列按这个索引取（§9.3 第三层）
        Index("ix_answer_run_state_created", "state", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    state: Mapped[str] = mapped_column(String(30))
    previous_state: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # 问答历史属**用户私有数据**（设计 21.2），所以带组织字段——与公共法律数据表不同
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    requested_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))

    # 正文不落库：只留哈希，用于同一问题的重复投递识别与追溯
    question_sha256: Mapped[str] = mapped_column(String(64))
    answer_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 证据引用：[{"provision_version_id": ..., "legal_status": ..., "cited": bool}]
    evidence: Mapped[list] = mapped_column(JSONB, default=list)
    # 配置快照：模型名、检索参数、提示词版本等——复核时要能还原「当时是怎么跑的」
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    # 哪道门禁拦下的（scope / verification / format）；没被拦为 NULL
    blocked_by: Mapped[str | None] = mapped_column(String(30), nullable=True)
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    seconds: Mapped[float | None] = mapped_column(nullable=True)

    # 人工复核（设计 §9.3 第三层）。`NEEDS_REVIEW` 的运行进待审队列。
    review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewed_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
