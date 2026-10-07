"""命令行创建用户（系统不开放注册，第一个管理员由此创建）。

用法：
    python -m app.cli create-user --org 示例机构 --username admin --role system_admin
    python -m app.cli reset-mfa --username admin
    python -m app.cli create-source --as kadmin --name 来源 --type official --trust high --license-note 说明
    python -m app.cli update-source --as kadmin --source <id> [--url ...] [--license-note ...]
    python -m app.cli import-documents --as editor1 --source <id> --dir 目录 --sensitivity public
    python -m app.cli set-document-source --as kadmin --document <id> --source <id>   # 更正来源归属
    python -m app.cli withdraw-document --as kadmin --document <id> [--document <id>...] --reason 原因 [--dry-run]
    python -m app.cli withdraw-document --as kadmin --from-source <来源 id> --reason 原因 [--dry-run]
    python -m app.cli relink-versions --as kadmin [--instrument <本体 id>...] [--dry-run]
    python -m app.cli extract-citations --as editor1 [--dry-run]   # 条文引用抽成 cite 边
    python -m app.cli flag-stale-pages --as reviewer1 [--dry-run]  # 标记依据已失效的已发布页面
    python -m app.cli ask --as reader1 --question "单位欠缴社保费会被怎么处理？"  # 本地模型的问答
    python -m app.cli run-worker [--once]        # 按需启动后台 worker（解析等）
密码从终端交互输入，不经命令行参数传递，避免进入 shell 历史。
"""

import argparse
import asyncio
import getpass
import hashlib
import json
import logging
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

from app.adapters import cache as answer_cache
from app.adapters.storage import get_storage
from app.core.config import get_settings
from app.core.database import SessionFactory, engine
from app.core.security import Principal
from app.models import (
    ROLE_NAMES,
    SENSITIVITY_LEVELS,
    SOURCE_TYPES,
    TRUST_LEVELS,
    AnswerRun,
    Job,
    Organization,
    User,
    UserRole,
)
from app.modules.answering import runs as answering_runs
from app.modules.answering import service as answering_service
from app.modules.authorization.service import (
    DOCUMENT_READ,
    DOCUMENT_WRITE,
    REVIEW_DECIDE,
    SOURCE_MANAGE,
    AuthorizationService,
)
from app.modules.documents import service as documents_service
from app.modules.documents import withdrawal as documents_withdrawal
from app.modules.identity import mfa, service
from app.modules.identity.schemas import CreateUser
from app.modules.legal_corpus import graph as legal_corpus_graph
from app.modules.legal_corpus import relink as legal_corpus_relink
from app.modules.sources import service as sources_service
from app.modules.sources.schemas import CreateSource, UpdateSource
from app.modules.wiki import staleness as wiki_staleness
from app.workers import runner as worker_runner


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


async def update_source(username: str, source_id: UUID, data: UpdateSource) -> None:
    """更正来源登记信息；需 source.manage，变更写审计（设计 §20.3）。"""
    async with SessionFactory() as session:
        try:
            async with session.begin():
                principal = await cli_principal(session, username, SOURCE_MANAGE)
            source = await sources_service.update_source(session, principal, source_id, data)
        except HTTPException as error:
            sys.exit(f"{error.status_code}: {error.detail}")
        finally:
            await engine.dispose()
    print(f"Updated source {source.name} ({source.id})")


async def withdraw_documents_command(
    username: str,
    document_ids: list[UUID],
    source_id: UUID | None,
    reason: str,
    dry_run: bool,
) -> None:
    """整批撤下原件及其解析产物（设计 §15.3）；需 source.manage，操作写审计。"""
    async with SessionFactory() as session:
        try:
            async with session.begin():
                principal = await cli_principal(session, username, SOURCE_MANAGE)
            if source_id is not None:
                document_ids = await documents_service.list_artifact_ids(session, source_id)
                if not document_ids:
                    sys.exit(f"No documents under source {source_id}")
            report = await documents_withdrawal.withdraw_documents(
                session,
                principal,
                get_storage(),
                document_ids,
                reason=reason,
                dry_run=dry_run,
            )
        except HTTPException as error:
            sys.exit(f"{error.status_code}: {error.detail}")
        finally:
            await engine.dispose()

    verb = "将撤下" if dry_run else "已撤下"
    print(f"{verb} {len(report.documents)} 份原件：")
    for plan in report.documents:
        print(
            f"  - {plan.filename}：解析版本 {plan.parse_revisions}，分块 {plan.chunks}，"
            f"定位 {plan.chunk_spans}，脱敏映射 {plan.redaction_entities}"
        )
    print(f"  解除引用的版本：{list(report.detach_versions) or '无'}")
    print(f"  仍无原件而删除的版本：{list(report.remove_versions) or '无'}")
    print(f"  随之删除的法律本体：{list(report.remove_instruments) or '无'}")
    print(f"  用这些原件重建版本树：{list(report.relink_from) or '无'}")
    if dry_run:
        print("  这是预演，未改动任何数据；确认后去掉 --dry-run 再执行。")


async def ask_command(
    username: str, question: str, limit: int, max_new_tokens: int, model: str | None
) -> None:
    """最小可用的证据约束问答（设计 §9；P6 的第一刀）；需 document.read。

    **只用本地生成模型**——§9.5 的决策锁定本地部署、默认关闭任何外部 API 接口。
    检索一条都没命中时不调用模型，直接返回「依据不足」。
    """
    async with SessionFactory() as session:
        try:
            async with session.begin():
                principal = await cli_principal(session, username, DOCUMENT_READ)
            answer = await answering_service.answer_question(
                session,
                principal,
                question,
                limit=limit,
                max_new_tokens=max_new_tokens,
                model=model,
            )
        except HTTPException as error:
            sys.exit(f"{error.status_code}: {error.detail}")
        finally:
            await engine.dispose()

    print(f"问题：{answer.question}")
    print(
        f"检索通路：{answer.path}　依据 {len(answer.citations)} 条"
        f"　生成时间：{answer.generated_at}　耗时 {answer.seconds:.1f}s"
    )
    # 门禁拦下的那几种情况里，`answer.answer` **就是**这条提示本身，别再打一遍
    for notice in (answer.status_notice, answer.scope_notice, answer.evidence_notice):
        if notice and notice not in answer.answer:
            print()
            print(f"⚠️  {notice}")
    if answer.verification is not None and not answer.verification.ok:
        # §9.3 第一层没过：answer 里已经是「不当正式答案发布」的说明，草稿单独打出来给人工判读
        print()
        print("⚠️  核验未通过（设计 §9.3 第一层）")
    print()
    print(answer.answer)
    if answer.draft:
        print()
        print("—— 以下为模型草稿，未通过核验，仅供参考 ——")
        print(answer.draft)
    # §20.2：每个正式输出都要带「不构成法律意见」声明与知识范围说明
    print()
    print(f"※ {answer.disclaimer}")
    if not answer.published:
        reason = {"scope": "问题涉及本人情形", "verification": "引用核验未通过"}.get(
            answer.blocked_by, answer.blocked_by
        )
        print(f"※ 本结论未通过门禁（{reason}），不作为正式答案，请人工判读。")
    if answer.citations:
        print()
        print("依据：")
        for index, citation in enumerate(answer.citations, start=1):
            print(
                f"  [{index}] {citation.instrument_title} {citation.provision_display}"
                f"（效力状态：{citation.legal_status}，版本 {citation.provision_version_id}）"
            )


async def answer_runs_command(username: str, limit: int, pending_only: bool) -> None:
    """列出问答运行记录（设计 §9.1、§9.4）；`--pending` 只看待人工复核的（§9.3 第三层）。

    **只显示元数据、证据引用与配置快照**——运行记录里本来就没有问题与回答正文（§21
    「客户数据不落库」，正文只留 sha256）。
    """
    async with SessionFactory() as session:
        try:
            async with session.begin():
                principal = await cli_principal(session, username, REVIEW_DECIDE)
            if pending_only:
                rows = await answering_runs.pending_reviews(session, principal, limit=limit)
            else:
                statement = (
                    select(AnswerRun)
                    .where(AnswerRun.organization_id == principal.organization_id)
                    .order_by(AnswerRun.created_at.desc())
                    .limit(limit)
                )
                rows = list(await session.scalars(statement))
        except HTTPException as error:
            sys.exit(f"{error.status_code}: {error.detail}")
        finally:
            await engine.dispose()

    title = "待人工复核" if pending_only else "最近"
    print(f"{title}问答运行 {len(rows)} 条：")
    for row in rows:
        flag = (
            "待审"
            if row.review_required and row.reviewed_at is None
            else "已复核"
            if row.reviewed_at
            else "—"
        )
        print(
            f"  {row.id}  {row.state:<22} {flag:<4} 发布={row.published} "
            f"门禁={row.blocked_by or '—'} 证据={len(row.evidence)} 条 "
            f"{row.created_at:%Y-%m-%d %H:%M}"
        )
    if pending_only and rows:
        print()
        print("复核：python -m app.cli review-run --as <用户名> --run <id> --note '...'")


async def review_run_command(username: str, run_id: str, note: str | None) -> None:
    """标记一条待审运行已人工复核（设计 §9.3 第三层）；需 review.decide。

    ⚠️ 复核**不改变运行状态**——状态机记的是「当时怎么走的」，复核是之后发生的另一件事，
    结论写在 `review_note` 里（并写审计）。
    """
    async with SessionFactory() as session:
        try:
            async with session.begin():
                principal = await cli_principal(session, username, REVIEW_DECIDE)
                run = await answering_runs.mark_reviewed(
                    session, principal, UUID(run_id), note=note
                )
        except HTTPException as error:
            sys.exit(f"{error.status_code}: {error.detail}")
        except (LookupError, ValueError) as error:
            sys.exit(str(error))
        finally:
            await engine.dispose()

    print(f"已复核：{run.id}（状态 {run.state}，门禁 {run.blocked_by or '—'}）")
    print(f"复核人：{run.reviewed_by}　时间：{run.reviewed_at}")
    if run.review_note:
        print(f"备注：{run.review_note}")


async def ask_async_command(
    username: str, question: str, limit: int, max_new_tokens: int, model: str | None
) -> None:
    """异步问答（设计 §9.4）：**提交后立刻返回**，由 worker 在后台执行。

    与同步 `ask` 的两点不同：① 需要短期缓存（答案要经它交付，问题也要经它传给 worker）；
    ② **问题先脱敏再进缓存**——它必须离开进程，所以不能带原始 PII。运行记录里仍然只有元数据与
    哈希（§21「客户数据不落库」）。
    """
    settings = get_settings()
    cache = answer_cache.from_settings(settings)
    try:
        async with SessionFactory() as session:
            try:
                async with session.begin():
                    principal = await cli_principal(session, username, DOCUMENT_READ)
                    run, job = await answering_runs.submit_question(
                        session,
                        principal,
                        question,
                        cache=cache,
                        limit=limit,
                        max_new_tokens=max_new_tokens,
                        model=model,
                    )
            except HTTPException as error:
                sys.exit(f"{error.status_code}: {error.detail}")
            except answering_runs.CacheUnavailable as error:
                sys.exit(str(error))
    finally:
        await cache.close()
        await engine.dispose()

    print(f"已提交问答运行：{run.id}（任务 {job.id}，状态 {run.state}）")
    print(f"缓存 TTL：{settings.answer_cache_ttl_seconds} 秒——**到期即焚**，之后内容不可再取")
    print()
    print("启动 worker：python -m app.cli run-worker --once")
    print(f"查看结果：  python -m app.cli answer-run --as {username} --run {run.id}")


async def answer_run_command(username: str, run_id: str) -> None:
    """查看一次问答运行（设计 §9.1、§9.4）；需 review.decide。

    ⚠️ **正文只在短期缓存里**：运行记录按 §21「客户数据不落库」只存元数据与哈希。缓存过期、
    或未配置缓存时，这里会**如实显示「内容不可用」**，不会拿别的东西冒充。
    """
    settings = get_settings()
    cache = answer_cache.from_settings(settings)
    try:
        async with SessionFactory() as session:
            try:
                async with session.begin():
                    principal = await cli_principal(session, username, REVIEW_DECIDE)
                run = await session.get(AnswerRun, UUID(run_id))
                if run is None or run.organization_id != principal.organization_id:
                    sys.exit("运行记录不存在")
                # 任务状态一并取出（见下面打印处的说明）
                job = await session.scalar(
                    select(Job)
                    .where(Job.payload["run_id"].astext == str(run.id))
                    .order_by(Job.created_at.desc())
                    .limit(1)
                )
                question = await answering_runs.read_question(cache, run.id)
                answer = await answering_runs.read_answer(cache, run.id)
            except HTTPException as error:
                sys.exit(f"{error.status_code}: {error.detail}")
    finally:
        await cache.close()
        await engine.dispose()

    print(f"运行：{run.id}")
    print(f"状态：{run.state}（上一步 {run.previous_state or '—'}）　门禁：{run.blocked_by or '—'}")
    print(f"发布：{run.published}　待审：{run.review_required and run.reviewed_at is None}")
    print(
        f"证据：{len(run.evidence)} 条　耗时：{run.seconds if run.seconds is None else f'{run.seconds:.1f}s'}"
    )
    # **任务状态要一并显示**：运行记录与任务是两套状态——运行记录说「这次问答走到哪」，任务表说
    # 「试了几次、成没成」。永久失败时运行记录会停在最后一次尝试的状态（不会自动置 FAILED，
    # 否则重试会撞上终态），所以**任务状态才是判断「卡住了」的关键**。
    if job is not None:
        attempts = f"{job.attempt_count}/{job.max_attempts}"
        extra = f"　错误：{job.error_code}" if job.error_code else ""
        print(f"任务：{job.status}（尝试 {attempts}）{extra}")
    else:
        print("任务：—（同步问答没有任务）")
    print(f"配置：{run.config}")
    if run.reviewed_at:
        print(f"复核：{run.reviewed_by} @ {run.reviewed_at}　{run.review_note or ''}")
    print()
    if answer is None:
        print("内容不可用：短期缓存已过期、未配置缓存，或这次运行没产出内容。")
        print("（运行记录里不存问题与回答正文——设计 §21「客户数据不落库」，只留 sha256。）")
        return
    print("—— 问题（脱敏后）——")
    print(question or "（问题已过期）")
    print()
    print("—— 回答（脱敏后）——")
    print(answer)


async def flag_stale_pages_command(username: str, dry_run: bool) -> None:
    """标记依据已失效的已发布 Wiki 页面（设计 §10.2）；需 review.decide，操作写审计。

    只看**发布指针指向的**那版修订。两条信号：引用的法律版本被取代（`legal_status` 变
    `repealed`）、或引用条款正文与发布时的快照不一致。**只标记，不动正文**——历史说明按 §10.2
    保留，由人决定怎么改。
    """
    async with SessionFactory() as session:
        try:
            async with session.begin():
                principal = await cli_principal(session, username, REVIEW_DECIDE)
            report = await wiki_staleness.flag_stale(session, principal, dry_run=dry_run)
        except HTTPException as error:
            sys.exit(f"{error.status_code}: {error.detail}")
        finally:
            await engine.dispose()

    verb = "将标记" if dry_run else "已标记"
    print(
        f"检查 {report.published} 个已发布页面，发现 {report.stale} 个待复核："
        f"{verb} {report.newly_flagged} 个，清除 {report.cleared} 个。"
    )
    for finding in report.findings:
        print(f"  - {finding.title}（第 {finding.revision_number} 版）：{finding.reason}")
    if dry_run:
        print("  这是预演，未改动任何数据；确认后去掉 --dry-run 再执行。")


async def extract_citations_command(username: str, dry_run: bool) -> None:
    """把条文里的引用抽成 ``cite`` 边（设计 §8.4）；需 document.write，操作写审计。

    引用是**正则从正文抽的文本事实**（条文自己写着「本法第八十七条」），不是模型联想出来的边；
    目标条款不在库里的引用直接丢弃并计数，不猜。
    """
    async with SessionFactory() as session:
        try:
            async with session.begin():
                principal = await cli_principal(session, username, DOCUMENT_WRITE)
            report = await legal_corpus_graph.link_citations(session, principal, dry_run=dry_run)
        except HTTPException as error:
            sys.exit(f"{error.status_code}: {error.detail}")
        finally:
            await engine.dispose()

    verb = "将写入" if dry_run else "已写入"
    print(
        f"扫描 {report.provisions} 条条款，抽到 {report.extracted} 处引用，"
        f"{verb} {report.linked} 条边。"
    )
    print(f"未解析 {report.unresolved} 处（目标条款不在库里，已丢弃）：")
    for sample in report.unresolved_samples:
        print(f"  - {sample}")
    if dry_run:
        print("  这是预演，未改动任何数据；确认后去掉 --dry-run 再执行。")


async def relink_versions_command(
    username: str,
    instrument_ids: list[UUID],
    dry_run: bool,
) -> None:
    """按当前择优规则重挂版本树（设计 §7、§8.3）；需 source.manage，操作写审计。

    重选的是「哪份原件当主原件」——属**来源归属**层面的判断（§20.3 把来源管理归知识管理员），
    因此用 source.manage 而不是 document.write；与 withdraw-document 同一条维护路径。
    """
    async with SessionFactory() as session:
        try:
            async with session.begin():
                principal = await cli_principal(session, username, SOURCE_MANAGE)
            report = await legal_corpus_relink.relink_versions(
                session,
                principal,
                instrument_ids=set(instrument_ids) or None,
                dry_run=dry_run,
            )
        except HTTPException as error:
            sys.exit(f"{error.status_code}: {error.detail}")
        finally:
            await engine.dispose()

    verb = "将重挂" if dry_run else "已重挂"
    print(
        f"{verb} {report.instruments} 个本体的 {report.relinked} 份原件"
        f"（候选 {report.candidates}）。"
    )
    for change in report.changed:
        print(
            f"  - {change.instrument_title} / {change.version_label}："
            f"{change.previous_artifact or '（无）'} → {change.adopted_artifact}"
        )
    if not report.changed:
        print("  主原件没有变化。")
    if report.skipped:
        print(f"  跳过（取不到解析产物）：{list(report.skipped)}")
    if report.created_versions:
        print(f"  ⚠️ 新建了版本：{list(report.created_versions)}")
    if dry_run:
        print("  这是预演，未改动任何数据；确认后去掉 --dry-run 再执行。")


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


async def set_document_source(username: str, document_id: UUID, source_id: UUID) -> None:
    """更正原件来源归属；需 source.manage，变更写审计（设计 §20.3）。"""
    async with SessionFactory() as session:
        try:
            async with session.begin():
                principal = await cli_principal(session, username, SOURCE_MANAGE)
            artifact = await documents_service.set_source(
                session, principal, document_id, source_id
            )
            async with session.begin():
                target = await sources_service.get_source(session, artifact.source_id)
                label = f"{target.name} ({target.id})"
        except HTTPException as error:
            sys.exit(f"{error.status_code}: {error.detail}")
        finally:
            await engine.dispose()
    print(f"Document {artifact.id} now belongs to source {label}")


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


def _exit_missing_pg_tool(tool: str):
    """pg_dump / pg_restore 不在 PATH 时给出可操作的提示，而不是裸的 FileNotFoundError。"""
    sys.exit(
        f"未找到 {tool}：备份与恢复依赖 PostgreSQL 客户端工具。\n"
        f"  请安装与数据库服务器同版本的客户端工具（pg_dump / pg_restore）并加入 PATH；\n"
        f"  若数据库运行在 Docker 中，可改用容器内的 pg_dump/pg_restore，见 CLAUDE.md「本机开发」。"
    )


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
    try:
        result = subprocess.run(
            ["pg_dump", "--format=custom", "-f", str(dump_path)] + conn_args + [db_name],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        shutil.rmtree(backup_dir, ignore_errors=True)
        _exit_missing_pg_tool("pg_dump")
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
    try:
        result = subprocess.run(
            ["pg_restore", "--clean", "--if-exists", "-d", db_name]
            + conn_args
            + [str(src / "db.dump")],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        _exit_missing_pg_tool("pg_restore")
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


async def run_worker(once: bool, poll_seconds: float | None) -> None:
    """按需启动单进程 worker（设计 §3.1、§14.2）。worker 不绕过权限，也无需用户身份。"""
    settings = get_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    storage = get_storage()
    common = {
        "lease_seconds": settings.worker_lease_seconds,
        "backoff_base": settings.worker_backoff_base_seconds,
        "backoff_max": settings.worker_backoff_max_seconds,
    }
    try:
        if once:
            processed = await worker_runner.run_once(
                SessionFactory, storage, worker_id=worker_runner.new_worker_id(), **common
            )
            print("processed one job" if processed else "no job available")
        else:
            await worker_runner.run_forever(
                SessionFactory,
                storage,
                poll_seconds=poll_seconds or settings.worker_poll_seconds,
                **common,
            )
    except KeyboardInterrupt:
        print("\nworker stopped")
    finally:
        await engine.dispose()


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

    set_source = commands.add_parser(
        "set-document-source",
        help="更正已导入原件的来源归属（FR-01；需 source.manage，变更写审计）",
    )
    set_source.add_argument("--as", dest="actor", required=True, help="执行操作的用户名")
    set_source.add_argument("--document", required=True, type=UUID, help="原件 ID")
    set_source.add_argument("--source", required=True, type=UUID, help="目标来源 ID")

    update = commands.add_parser(
        "update-source",
        help="更正来源登记信息（FR-01；需 source.manage，变更写审计）",
    )
    update.add_argument("--as", dest="actor", required=True, help="执行操作的用户名")
    update.add_argument("--source", required=True, type=UUID, help="来源 ID")
    update.add_argument("--name")
    update.add_argument("--type", choices=SOURCE_TYPES)
    update.add_argument("--trust", choices=TRUST_LEVELS)
    update.add_argument("--license-note", help="授权说明（不允许清空）")
    update.add_argument("--url")
    update.add_argument("--publisher")
    update.add_argument(
        "--last-checked-at",
        type=datetime.fromisoformat,
        help="来源最后核查时间（ISO 8601，带时区）；未核查时不填",
    )

    withdraw = commands.add_parser(
        "withdraw-document",
        help="整批撤下原件及其解析产物（设计 §15.3；需 source.manage，操作写审计）",
    )
    withdraw.add_argument("--as", dest="actor", required=True, help="执行操作的用户名")
    withdraw.add_argument(
        "--document", action="append", type=UUID, default=[], help="原件 ID，可重复"
    )
    withdraw.add_argument("--from-source", type=UUID, help="撤下该来源下的全部原件")
    withdraw.add_argument("--reason", required=True, help="撤下原因（写入审计）")
    withdraw.add_argument("--dry-run", action="store_true", help="只输出将删除的内容，不改动数据")

    relink = commands.add_parser(
        "relink-versions",
        help="按当前择优规则重挂版本树（设计 §7、§8.3；需 source.manage，操作写审计）",
    )
    relink.add_argument("--as", dest="actor", required=True, help="执行操作的用户名")
    relink.add_argument(
        "--instrument",
        action="append",
        type=UUID,
        default=[],
        help="限定本体 ID，可重复；缺省为全部",
    )
    relink.add_argument("--dry-run", action="store_true", help="只输出将发生的变化，不改动数据")

    citations = commands.add_parser(
        "extract-citations",
        help="把条文里的引用抽成 cite 边（设计 §8.4；需 document.write，操作写审计）",
    )
    citations.add_argument("--as", dest="actor", required=True, help="执行操作的用户名")
    citations.add_argument("--dry-run", action="store_true", help="只输出将写入的边数，不改动数据")

    stale = commands.add_parser(
        "flag-stale-pages",
        help="标记依据已失效的已发布 Wiki 页面（设计 §10.2；需 review.decide，操作写审计）",
    )
    stale.add_argument("--as", dest="actor", required=True, help="执行操作的用户名")
    stale.add_argument("--dry-run", action="store_true", help="只输出将标记的页面，不改动数据")

    ask = commands.add_parser(
        "ask",
        help="证据约束问答（设计 §9；本地生成模型，需 document.read）",
    )
    ask.add_argument("--as", dest="actor", required=True, help="执行操作的用户名")
    ask.add_argument("--question", required=True, help="要问的问题")
    ask.add_argument("--limit", type=int, default=5, help="取多少条依据（默认 5）")
    ask.add_argument("--max-new-tokens", type=int, default=512, help="最多生成多少 token")
    ask.add_argument("--model", default=None, help="覆盖生成模型（默认取配置里的）")
    ask.add_argument(
        "--async",
        dest="run_async",
        action="store_true",
        help="异步执行：提交后立刻返回 run id，由 worker 在后台跑（需 CACHE_URL）",
    )

    answer_run = commands.add_parser(
        "answer-run", help="查看一次问答运行（含短期缓存里的问题与回答，脱敏后）"
    )
    answer_run.add_argument("--as", dest="actor", required=True, help="执行操作的用户名")
    answer_run.add_argument("--run", required=True, help="运行记录 id")

    answer_runs = commands.add_parser(
        "answer-runs", help="列出问答运行记录；--pending 只看待人工复核（设计 §9.1、§9.3）"
    )
    answer_runs.add_argument("--as", dest="actor", required=True, help="执行操作的用户名")
    answer_runs.add_argument("--limit", type=int, default=20, help="最多列出多少条（默认 20）")
    answer_runs.add_argument("--pending", action="store_true", help="只看待人工复核的")

    review_run = commands.add_parser(
        "review-run", help="标记一条待审运行已人工复核（设计 §9.3 第三层）"
    )
    review_run.add_argument("--as", dest="actor", required=True, help="执行操作的用户名")
    review_run.add_argument("--run", required=True, help="运行记录 id")
    review_run.add_argument("--note", default=None, help="复核备注")

    worker = commands.add_parser(
        "run-worker", help="按需启动单进程 worker，领取并处理后台任务（设计 12.2）"
    )
    worker.add_argument("--once", action="store_true", help="只处理一个任务后退出")
    worker.add_argument("--poll-seconds", type=float, default=None, help="无任务时的轮询间隔（秒）")

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

    if args.command == "run-worker":
        asyncio.run(run_worker(args.once, args.poll_seconds))
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

    if args.command == "set-document-source":
        asyncio.run(set_document_source(args.actor, args.document, args.source))
        return

    if args.command == "update-source":
        # 只提交实际给出的字段；未给出的保持不变（--url "" 会因校验失败而被拒）
        payload = {
            field: value
            for field, value in {
                "name": args.name,
                "source_type": args.type,
                "trust_level": args.trust,
                "license_note": args.license_note,
                "url": args.url,
                "publisher": args.publisher,
                "last_checked_at": args.last_checked_at,
            }.items()
            if value is not None
        }
        try:
            data = UpdateSource(**payload)
        except ValidationError as error:
            sys.exit(str(error))
        asyncio.run(update_source(args.actor, args.source, data))
        return

    if args.command == "withdraw-document":
        if not args.document and args.from_source is None:
            sys.exit("Give --document (repeatable) or --from-source")
        asyncio.run(
            withdraw_documents_command(
                args.actor, args.document, args.from_source, args.reason, args.dry_run
            )
        )
        return

    if args.command == "relink-versions":
        asyncio.run(relink_versions_command(args.actor, args.instrument, args.dry_run))
        return

    if args.command == "ask":
        handler = ask_async_command if args.run_async else ask_command
        asyncio.run(handler(args.actor, args.question, args.limit, args.max_new_tokens, args.model))
        return

    if args.command == "answer-run":
        asyncio.run(answer_run_command(args.actor, args.run))
        return

    if args.command == "answer-runs":
        asyncio.run(answer_runs_command(args.actor, args.limit, args.pending))
        return

    if args.command == "review-run":
        asyncio.run(review_run_command(args.actor, args.run, args.note))
        return

    if args.command == "flag-stale-pages":
        asyncio.run(flag_stale_pages_command(args.actor, args.dry_run))
        return

    if args.command == "extract-citations":
        asyncio.run(extract_citations_command(args.actor, args.dry_run))
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
