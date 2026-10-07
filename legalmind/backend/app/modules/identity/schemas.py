from typing import Literal
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
    mfa_enabled: bool


class CurrentUser(UserOut):
    csrf_token: str
    #: 当前角色对应的**权限集合**（并集）。前端用它决定菜单与按钮是否渲染；
    #: **强制校验仍在后端**（`require_permission`），这不是授权依据。
    permissions: list[str] = []
    # ok：可使用业务接口；enroll：需先绑定 TOTP；verify：需输入验证码
    mfa_status: Literal["ok", "enroll", "verify"]


class MfaCode(BaseModel):
    # TOTP 为 6 位数字；恢复码形如 xxxxx-xxxxx
    code: str = Field(min_length=6, max_length=20)


class MfaEnrollment(BaseModel):
    secret: str
    otpauth_uri: str


class RecoveryCodes(BaseModel):
    recovery_codes: list[str]
