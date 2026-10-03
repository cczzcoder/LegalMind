from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

SourceType = Literal["official", "republished", "internal"]
TrustLevel = Literal["high", "medium", "low"]


class CreateSource(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source_type: SourceType
    trust_level: TrustLevel
    url: str | None = Field(default=None, max_length=2000)
    publisher: str | None = Field(default=None, max_length=200)
    # 授权说明必填：没有说明就不知道能否存储、加工和展示（需求 2.2）
    license_note: str = Field(min_length=1, max_length=5000)
    # 未核查时留空，表示未知
    last_checked_at: datetime | None = None

    @field_validator("name", "license_note")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value must not be blank.")
        return value.strip()

    @field_validator("url")
    @classmethod
    def http_url_only(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("https://", "http://")):
            raise ValueError("URL must start with http:// or https://")
        return value

    @field_validator("last_checked_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("last_checked_at must include a timezone")
        return value


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    source_type: SourceType
    trust_level: TrustLevel
    url: str | None
    publisher: str | None
    license_note: str
    last_checked_at: datetime | None
    created_at: datetime
