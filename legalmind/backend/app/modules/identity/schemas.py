from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.models import ROLE_NAMES


def validate_roles(roles: list[str]) -> list[str]:
    unknown = set(roles) - set(ROLE_NAMES)
    if unknown:
        raise ValueError(f"Unknown roles: {sorted(unknown)}")
    return sorted(set(roles))


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=128)


class CreateUser(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    # 上限防止超长输入拖慢 Argon2
    password: str = Field(min_length=12, max_length=128)
    roles: list[str] = Field(min_length=1)

    _check_roles = field_validator("roles")(validate_roles)


class SetRoles(BaseModel):
    roles: list[str]

    _check_roles = field_validator("roles")(validate_roles)


class UserOut(BaseModel):
    id: UUID
    organization_id: UUID
    username: str
    is_active: bool
    roles: list[str]


class CurrentUser(UserOut):
    csrf_token: str
