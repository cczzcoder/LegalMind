from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

AccessScope = Literal["organization", "restricted"]


class CreatePage(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=200_000)
    # restricted：只有被授权的用户可访问，创建者自动获得授权
    access_scope: AccessScope = "organization"

    @field_validator("title", "body")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value must not be blank.")
        return value


class CreateRevision(BaseModel):
    expected_revision: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=200_000)

    @field_validator("body")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Body must not be blank.")
        return value


class PageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    head_revision: int
    # 为 None 表示还没有审核发布的版本，读者看不到内容（设计 §10.2）
    published_revision: int | None
    # 待复核标记：引用的依据被取代或正文变了（§10.2「来源更新先标记待复核」）
    review_due_at: datetime | None
    review_due_reason: str | None
    access_scope: AccessScope
    created_at: datetime


class SetAccessScope(BaseModel):
    access_scope: AccessScope


class SetCitations(BaseModel):
    """覆盖式登记引用；引用的是**具体条款版本**（设计 §5.3）。"""

    provision_version_ids: list[UUID] = Field(max_length=500)


class ReviewDecision(BaseModel):
    """审核结论。``note`` 是驳回时给作者的理由，也是事后审计要看的东西。"""

    note: str | None = Field(default=None, max_length=2000)


class GrantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    granted_by: UUID
    created_at: datetime


class CitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provision_version_id: UUID
    created_at: datetime


class RevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    page_id: UUID
    number: int
    body: str
    status: str
    author_id: UUID
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    review_note: str | None
    created_at: datetime
