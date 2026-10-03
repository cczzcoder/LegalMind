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
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.adapters.storage import get_storage
from app.core.config import get_settings
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
    roles = frozenset(
        await session.scalars(select(UserRole.role).where(UserRole.user_id == user.id))
    )
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

    # 与 API 入口使用同一上限，避免把超大文件整个读进内存（设计 14.2）
    max_bytes = get_settings().max_upload_bytes
    imported = skipped = failed = 0
    async with SessionFactory() as session:
        try:
            async with session.begin():
                principal = await cli_principal(session, username, DOCUMENT_WRITE)
            for path in files:
                # 逐个文件独立事务，单个失败不影响其他文件；重复文件跳过而不是重复登记
                size = path.stat().st_size
                if size > max_bytes:
                    failed += 1
                    print(f"FAIL  {path.name}: {size} bytes exceeds MAX_UPLOAD_BYTES={max_bytes}")
                    continue
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


def _pg_env_and_args(database_url: str) -> tuple[dict, list[str]]:
    """将 DATABASE_URL 拆成 pg_dump / pg_restore 的连接参数和环境变量。"""
    url = database_url.replace("+asyncpg", "")
    parsed = urlparse(url)
    env = os.environ.copy()
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    args: list[str] = []
    if parsed.hostname:
        args += ["-h", parsed.hostname]
    if parsed.port:
        args += ["-p", str(parsed.port)]
    if parsed.username:
        args += ["-U", parsed.username]
    return env, args


def do_backup(dest: Path, label: str | None, settings=None) -> None:
    if settings is None:
        from app.core.config import get_settings  # 延迟导入，避免在测试中触发 DB 验证

        settings = get_settings()
    if label is None:
        label = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    backup_dir = dest / label
    if backup_dir.exists():
        sys.exit(f"备份目录已存在: {backup_dir}")
    backup_dir.mkdir(parents=True)

    env, conn_args = _pg_env_and_args(settings.database_url)
    db_name = urlparse(settings.database_url.replace("+asyncpg", "")).path.lstrip("/")
    dump_path = backup_dir / "db.dump"

    print(f"导出数据库 → {dump_path} …")
    result = subprocess.run(
        ["pg_dump", "--format=custom", "-f", str(dump_path)] + conn_args + [db_name],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        shutil.rmtree(backup_dir, ignore_errors=True)
        sys.exit(f"pg_dump 失败:\n{result.stderr}")

    storage_root = Path(settings.storage_root).resolve()
    objects_src = storage_root / "objects"
    objects_dst = backup_dir / "objects"

    entries: list[dict] = []
    if objects_src.exists():
        for src_file in sorted(objects_src.rglob("*")):
            if not src_file.is_file():
                continue
            content = src_file.read_bytes()
            sha256 = hashlib.sha256(content).hexdigest()
            dst = objects_dst / src_file.relative_to(objects_src)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst)
            entries.append(
                {"object_key": src_file.name, "sha256": sha256, "size_bytes": len(content)}
            )

    (backup_dir / "artifacts_manifest.json").write_text(
        json.dumps({"format_version": "1", "entries": entries}, indent=2), encoding="utf-8"
    )
    (backup_dir / "backup_meta.json").write_text(
        json.dumps(
            {
                "created_at": datetime.now(UTC).isoformat(),
                "label": label,
                "artifact_count": len(entries),
                "db_size_bytes": dump_path.stat().st_size,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"备份完成: {backup_dir}\n  DB dump: {dump_path.stat().st_size:,} 字节  原件: {len(entries)} 个"
    )


def do_restore(
    src: Path,
    database_url: str | None,
    storage_root_override: str | None,
    dry_run: bool,
    settings=None,
) -> None:
    if settings is None:
        from app.core.config import get_settings

        settings = get_settings()
    db_url = database_url or settings.database_url
    storage_root = Path(storage_root_override or settings.storage_root).resolve()

    for name in ("backup_meta.json", "artifacts_manifest.json", "db.dump"):
        if not (src / name).exists():
            sys.exit(f"备份不完整，缺失: {name}")

    meta = json.loads((src / "backup_meta.json").read_text(encoding="utf-8"))
    manifest = json.loads((src / "artifacts_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format_version") != "1":
        sys.exit(f"不支持的清单版本: {manifest.get('format_version')!r}")

    entries: list[dict] = manifest.get("entries", [])
    print(f"备份时间: {meta.get('created_at')}  原件数: {len(entries)}")

    # 全部校验通过才继续，不做部分恢复
    print("校验原件哈希 …")
    bad: list[str] = []
    objects_src = src / "objects"
    for entry in entries:
        key = entry["object_key"]
        backup_file = objects_src / key[:2] / key
        if not backup_file.exists():
            bad.append(f"  {key}: 备份文件缺失")
            continue
        actual = hashlib.sha256(backup_file.read_bytes()).hexdigest()
        if actual != entry["sha256"]:
            bad.append(f"  {key}: sha256 不符 (期望 {entry['sha256'][:12]}… 实际 {actual[:12]}…)")
    if bad:
        sys.exit("原件校验失败，中止恢复:\n" + "\n".join(bad))

    # 目标存储的冲突检查必须在覆盖数据库之前完成，
    # 否则冲突中止会留下“库已恢复、原件未恢复”的不一致状态（设计 15.2）
    conflicts: list[str] = []
    for entry in entries:
        key = entry["object_key"]
        existing = storage_root / "objects" / key[:2] / key
        if (
            existing.exists()
            and hashlib.sha256(existing.read_bytes()).hexdigest() != entry["sha256"]
        ):
            conflicts.append(key)
    if conflicts:
        preview = "、".join(conflicts[:5])
        sys.exit(
            f"恢复冲突（{len(conflicts)} 个），未修改数据库: {preview} 已存在且内容不同，"
            "请先清空存储目录再恢复。"
        )

    print(f"全部 {len(entries)} 个原件校验通过。")
    if dry_run:
        print("Dry run：未写入任何数据。")
        return

    env, conn_args = _pg_env_and_args(db_url)
    db_name = urlparse(db_url.replace("+asyncpg", "")).path.lstrip("/")
    print(f"恢复数据库 {db_name!r} …")
    result = subprocess.run(
        ["pg_restore", "--clean", "--if-exists", "-d", db_name]
        + conn_args
        + [str(src / "db.dump")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    # pg_restore 仅当 exit > 1 时才是真实错误（exit 1 可能只是警告）
    if result.returncode > 1:
        sys.exit(f"pg_restore 失败 (exit {result.returncode}):\n{result.stderr}")
    if result.stderr:
        print(f"pg_restore 警告:\n{result.stderr.strip()}")

    copied = skipped = 0
    for entry in entries:
        key = entry["object_key"]
        src_file = objects_src / key[:2] / key
        dst_file = storage_root / "objects" / key[:2] / key
        if dst_file.exists():
            # 内容冲突已在恢复数据库前排除，此处必定一致
            skipped += 1
            continue
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)
        copied += 1

    print(
        f"恢复完成。\n"
        f"  原件写入: {copied} 个，已存在跳过: {skipped} 个\n"
        f"  建议验证: SELECT acquired_at FROM source_artifacts LIMIT 5;"
    )


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

    bak = commands.add_parser("backup", help="备份数据库和原始文件到本地目录")
    bak.add_argument("--dest", required=True, type=Path, help="备份存放的父目录")
    bak.add_argument("--label", default=None, help="本次备份的标签（默认为 UTC 时间戳）")

    res = commands.add_parser("restore", help="从备份目录恢复数据库和原始文件")
    res.add_argument("--src", required=True, type=Path, help="备份目录（含 backup_meta.json）")
    res.add_argument("--db-url", default=None, help="覆盖 DATABASE_URL")
    res.add_argument("--storage-root", default=None, help="覆盖 STORAGE_ROOT")
    res.add_argument("--dry-run", action="store_true", help="仅校验，不写入任何数据")

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

    if args.command == "backup":
        do_backup(args.dest, args.label)
        return

    if args.command == "restore":
        if not args.src.is_dir():
            sys.exit(f"备份目录不存在: {args.src}")
        do_restore(args.src, args.db_url, args.storage_root, args.dry_run)
        return

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
