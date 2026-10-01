import secrets
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader

from app.core.config import get_settings

api_key_header = APIKeyHeader(
    name="X-Dev-Key",
    auto_error=False,
)


@dataclass(frozen=True)
class Principal:
    organization_id: UUID
    user_id: UUID


async def get_principal(
    key: Annotated[str | None, Depends(api_key_header)],
) -> Principal:
    settings = get_settings()
    expected = settings.dev_api_key.get_secret_value()

    if key is None or not secrets.compare_digest(
        key.encode("utf-8"),
        expected.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid development credentials",
        )

    return Principal(
        organization_id=settings.dev_org_id,
        user_id=settings.dev_user_id,
    )
