from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CreatePage(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=200_000)

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
    created_at: datetime


class RevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    page_id: UUID
    number: int
    body: str
    status: str
    author_id: UUID
    created_at: datetime
