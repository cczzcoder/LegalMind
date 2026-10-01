"""命令行创建用户（系统不开放注册，第一个管理员由此创建）。

用法：
    python -m app.cli create-user --org 示例机构 --username admin --role system_admin
密码从终端交互输入，不经命令行参数传递，避免进入 shell 历史。
"""

import argparse
import asyncio
import getpass
import sys

from pydantic import ValidationError
from sqlalchemy import select

from app.core.database import SessionFactory, engine
from app.models import ROLE_NAMES, Organization
from app.modules.identity import service
from app.modules.identity.schemas import CreateUser


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


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-user")
    create.add_argument("--org", required=True, help="组织名称，不存在时创建")
    create.add_argument("--username", required=True)
    create.add_argument("--role", action="append", required=True, choices=ROLE_NAMES)

    args = parser.parse_args()

    password = getpass.getpass("Password (min 12 chars): ")
    if password != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match.")

    try:
        asyncio.run(create_user(args.org, args.username, args.role, password))
    except ValidationError as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
