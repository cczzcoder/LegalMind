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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WikiPage(Base):
    __tablename__ = "wiki_pages"
    __table_args__ = (
        CheckConstraint(
            "head_revision >= 1",
            name="ck_wiki_page_head_positive",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    organization_id: Mapped[UUID] = mapped_column(index=True)
    title: Mapped[str] = mapped_column(String(200))
    head_revision: Mapped[int] = mapped_column(Integer)

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
        CheckConstraint(
            "status = 'draft'",
            name="ck_wiki_draft_only",
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
    author_id: Mapped[UUID]

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    organization_id: Mapped[UUID] = mapped_column(index=True)
    actor_id: Mapped[UUID]
    action: Mapped[str] = mapped_column(String(100))
    resource_id: Mapped[UUID]
    payload: Mapped[dict] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    organization_id: Mapped[UUID] = mapped_column(index=True)
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
