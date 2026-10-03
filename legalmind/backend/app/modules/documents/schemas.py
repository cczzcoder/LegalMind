from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

Sensitivity = Literal["public", "internal", "confidential"]
AccessScope = Literal["organization", "restricted"]


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_id: UUID
    original_filename: str
    media_type: str
    size_bytes: int
    sha256: str
    sensitivity: Sensitivity
    access_scope: AccessScope
    acquired_at: datetime | None
    created_by: UUID
    created_at: datetime


class ImportResult(BaseModel):
    document: DocumentOut
    job_id: UUID


class SetAccessScope(BaseModel):
    access_scope: AccessScope


class SetDocumentSource(BaseModel):
    """更正原件的来源归属（FR-01）；目标来源必须已登记授权说明（设计 §20.3）。"""

    source_id: UUID


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    job_type: str
    status: str
    attempt_count: int
    max_attempts: int
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
