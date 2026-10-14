"""Wiki 页与修订（设计 §10）。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# 修订状态机（设计 §10.2）：提交后锁定，审核通过才发布；驳回后可以改引用再提交，
# 但改正文要新开修订。
WIKI_REVISION_STATUSES = ("draft", "submitted", "published", "rejected")


class WikiPage(Base):
    __tablename__ = "wiki_pages"
    __table_args__ = (
        CheckConstraint(
            "head_revision >= 1",
            name="ck_wiki_page_head_positive",
        ),
        CheckConstraint(
            "access_scope IN ('organization', 'restricted')",
            name="ck_wiki_page_access_scope",
        ),
        # 发布指针只能指向本页已存在的修订（设计 §10.2）
        CheckConstraint(
            "published_revision IS NULL"
            " OR (published_revision >= 1 AND published_revision <= head_revision)",
            name="ck_wiki_page_published_within_head",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200))
    head_revision: Mapped[int] = mapped_column(Integer)
    # 审核通过后写入；为 NULL 表示还没有正式发布的版本，读者看不到内容（设计 §10.2）
    published_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 待复核标记：引用的依据被取代或正文变了（§10.2「来源更新先标记待复核」）。
    # 只标记、不动正文——历史说明保留，由人决定怎么改。
    review_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_due_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # organization：同组织有角色权限者可访问；restricted：还需 AccessGrant
    access_scope: Mapped[str] = mapped_column(
        String(20),
        default="organization",
        server_default="organization",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class WikiRevision(Base):
    __tablename__ = "wiki_revisions"
    __table_args__ = (
        UniqueConstraint(
            "page_id",
            "number",
            name="uq_wiki_revision_number",
        ),
        CheckConstraint(
            "number > 0",
            name="ck_wiki_revision_positive",
        ),
        # 状态取值来自 WIKI_REVISION_STATUSES；这里不能调 _in（它定义在本文件后面，
        # 类体求值时还看不到），所以直接拼串——与旁边 ck_wiki_page_access_scope 同一写法
        CheckConstraint(
            "status IN ('" + "', '".join(WIKI_REVISION_STATUSES) + "')",
            name="ck_wiki_revision_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    page_id: Mapped[UUID] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="RESTRICT"),
        index=True,
    )
    number: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(30),
        default="draft",
    )
    author_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    # 审核痕迹（设计 §10.1 的「审核信息」）：谁、何时、以什么理由批的或驳的
    reviewed_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class WikiRevisionCitation(Base):
    """修订引用的**具体条款版本**（设计 §5.3、§10.2）。

    指向 ``provision_versions`` 而不是「某条法律的最新版」——原文更新后旧结论必须看起来就旧，
    否则「有据可查」是假的。发布前逐条检查，读取时用它执行 §10.3 的权限继承。
    """

    __tablename__ = "wiki_revision_citations"
    __table_args__ = (
        UniqueConstraint("revision_id", "provision_version_id", name="uq_wiki_citation"),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("wiki_revisions.id", ondelete="RESTRICT"),
        index=True,
    )
    provision_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("provision_versions.id", ondelete="RESTRICT"),
        index=True,
    )
    # **发布时**引用条款的正文哈希（§10.2）。之后同一版本被重新解析、或换了更优原件后重建过
    # 条款文本，哈希就对不上——「审核当时看到的是这段文字」要能验证，不能只靠版本号。
    provision_text_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
