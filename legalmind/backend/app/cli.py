"""命令行创建用户（系统不开放注册，第一个管理员由此创建）。

用法：
    python -m app.cli create-user --org 示例机构 --username admin --role system_admin
    python -m app.cli reset-mfa --username admin
    python -m app.cli create-source --as kadmin --name 来源 --type official --trust high --license-note 说明
    python -m app.cli import-documents --as editor1 --source <id> --dir 目录 --sensitivity public
密码从终端交互输入，不经命令行参数传递，避免进入 shell 历史。
"""

import argparse
import asyncio
import getpass
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.adapters.storage import get_storage
from app.core.database import SessionFactory, engine
from app.core.security import Principal
from app.models import (
    ROLE_NAMES,
    SENSITIVITY_LEVELS,
    SOURCE_TYPES,
    TRUST_LEVELS,
    Organization,
    User,
    UserRole,
)
from app.modules.authorization.service import (
    DOCUMENT_WRITE,
    SOURCE_MANAGE,
    AuthorizationService,
)
from app.modules.documents import service as documents_service
from app.modules.identity import mfa, service
from app.modules.identity.schemas import CreateUser
from app.modules.sources import service as sources_service
from app.modules.sources.schemas import CreateSource


async def create_user(org_name: str, username: str, roles: list[str], password: str) -> None:
    async with SessionFactory() as session:
        async with session.begin():
            organization = await session.scalar(
                select(Organization).where(Organization.name == org_name)
            )
            if organization is None:
                organization = Organization(name=org_name)
                session.add(organization)
                print(f"Created organization: {org_name}")
        organization_id = organization.id

        user = await service.create_user(
            session,
            organization_id,
            None,
            CreateUser(username=username, password=password, roles=roles),
        )
    await engine.dispose()
    print(f"Created user {user.username} ({user.id}) with roles {user.roles}")


async def cli_principal(session, username: str, permission: str):
    """CLI 以指定用户的身份和角色执行，不绕过权限检查。"""
    user = await session.scalar(select(User).where(User.username == username))
    if user is None or not user.is_active:
        sys.exit(f"Active user not found: {username}")
    roles = frozenset(await session.scalars(select(UserRole.role).where(UserRole.user_id == user.id)))
    principal = Principal(organization_id=user.organization_id, user_id=user.id, roles=roles)
    if not AuthorizationService.can(principal, permission):
        sys.exit(f"User {username} lacks permission {permission}")
    return principal


async def create_source(username: str, data: CreateSource) -> None:
    async with SessionFactory() as session:
        try:
            async with session.begin():
                principal = await cli_principal(session, username, SOURCE_MANAGE)
            source = await sources_service.create_source(session, principal, data)
        except HTTPException as error:
            sys.exit(f"{error.status_code}: {error.detail}")
        finally:
            await engine.dispose()
    print(f"Created source {source.name} ({source.id})")


async def import_documents(
    username: str,
    source_id: UUID,
    directory: Path,
    sensitivity: str,
    access_scope: str,
) -> None:
    files = sorted(path for path in directory.iterdir() if path.is_file())
    if not files:
        sys.exit(f"No files in {directory}")

    imported = skipped = failed = 0
    async with SessionFactory() as session:
        try:
            async with session.begin():
                principal = await cli_principal(session, username, DOCUMENT_WRITE)
            for path in files:
                # 逐个文件独立事务，单个失败不影响其他文件；重复文件跳过而不是重复登记
                try:
                    artifact, job = await documents_service.import_document(
                        session,
                        get_storage(),
                        principal,
                        source_id=source_id,
                        filename=path.name,
                        content=path.read_bytes(),
                        sensitivity=sensitivity,
                        access_scope=access_scope,
                    )
                except HTTPException as error:
                    if error.status_code == 409:
                        skipped += 1
                        print(f"SKIP  {path.name}: {error.detail}")
                    elif error.status_code == 404:
                        sys.exit(f"Source not found: {source_id}")
                    else:
                        failed += 1
                        print(f"FAIL  {path.name}: {error.status_code} {error.detail}")
                    continue
                imported += 1
                print(f"OK    {path.name} -> document {artifact.id}, job {job.id}")
        finally:
            await engine.dispose()

    print(f"Imported {imported}, skipped {skipped}, failed {failed}.")
    if failed:
        sys.exit(1)


async def reset_mfa(username: str) -> None:
    async with SessionFactory() as session:
        try:
            user = await mfa.reset(session, username)
        except LookupError:
            sys.exit(f"User not found: {username}")
        finally:
            await engine.dispose()
    print(f"MFA reset for {user.username}; all sessions revoked. Re-enroll at next login.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-user")
    create.add_argument("--org", required=True, help="组织名称，不存在时创建")
    create.add_argument("--username", required=True)
    create.add_argument("--role", action="append", required=True, choices=ROLE_NAMES)

    # 认证器与恢复码都丢失时由运维在服务器上执行，操作写入审计
    reset = commands.add_parser("reset-mfa")
    reset.add_argument("--username", required=True)

    # 以 --as 指定的用户身份执行，需要该用户角色具备相应权限，操作写入审计
    source = commands.add_parser("create-source")
    source.add_argument("--as", dest="actor", required=True, help="执行操作的用户名")
    source.add_argument("--name", required=True)
    source.add_argument("--type", required=True, choices=SOURCE_TYPES)
    source.add_argument("--trust", required=True, choices=TRUST_LEVELS)
    source.add_argument("--license-note", required=True, help="资料获取及使用授权说明")
    source.add_argument("--url")
    source.add_argument("--publisher")
    source.add_argument(
        "--last-checked-at",
        type=datetime.fromisoformat,
        help="来源最后核查时间（ISO 8601，带时区）；未核查时不填",
    )

    imports = commands.add_parser("import-documents")
    imports.add_argument("--as", dest="actor", required=True, help="执行操作的用户名")
    imports.add_argument("--source", required=True, type=UUID)
    imports.add_argument("--dir", required=True, type=Path)
    imports.add_argument("--sensitivity", required=True, choices=SENSITIVITY_LEVELS)
    imports.add_argument(
        "--access-scope",
        default="organization",
        choices=("organization", "restricted"),
    )

    args = parser.parse_args()

    if args.command == "reset-mfa":
        asyncio.run(reset_mfa(args.username))
        return

    if args.command == "create-source":
        try:
            data = CreateSource(
                name=args.name,
                source_type=args.type,
                trust_level=args.trust,
                license_note=args.license_note,
                url=args.url,
                publisher=args.publisher,
                last_checked_at=args.last_checked_at,
            )
        except ValidationError as error:
            sys.exit(str(error))
        asyncio.run(create_source(args.actor, data))
        return

    if args.command == "import-documents":
        if not args.dir.is_dir():
            sys.exit(f"Not a directory: {args.dir}")
        asyncio.run(
            import_documents(
                args.actor,
                args.source,
                args.dir,
                args.sensitivity,
                args.access_scope,
            )
        )
        return

    password = getpass.getpass("Password (min 12 chars): ")
    if password != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match.")

    try:
        asyncio.run(create_user(args.org, args.username, args.role, password))
    except ValidationError as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
