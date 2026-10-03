"""来源登记与原始资料导入集成测试（FR-01、FR-02、FR-10、FR-11、设计 7.3）：需要 TEST_DATABASE_URL。"""

import hashlib
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.adapters.storage import LocalFileStorage, get_storage
from app.core.config import get_settings
from app.models import AuditEvent, Job, OutboxEvent, SourceArtifact
from tests.helpers import login

pytestmark = pytest.mark.anyio

LAW_TEXT = "中华人民共和国劳动法\n第一条　为了保护劳动者的合法权益……".encode()


def law_text() -> bytes:
    """原件按 sha256 全库唯一（设计 21.2），测试每次用唯一内容避免互相冲突。"""
    return LAW_TEXT + uuid4().hex.encode()


@pytest.fixture
def storage(tmp_path, make_client):
    # make_client 结束时会清空 dependency_overrides
    from app.main import app

    storage = LocalFileStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: storage
    return storage


def stored_objects(storage: LocalFileStorage) -> list:
    return [path for path in storage.root.rglob("*") if path.is_file()]


async def create_source(client, **extra):
    return await client.post(
        "/api/v1/sources",
        json={
            # 来源属公共法律数据，名称全库唯一（设计 21.2）；测试用唯一名避免互相冲突
            "name": f"国家法律法规数据库-{uuid4().hex[:8]}",
            "source_type": "official",
            "trust_level": "high",
            "url": "https://flk.npc.gov.cn",
            "license_note": "官方公开发布的法律法规文本",
            **extra,
        },
    )


async def import_file(client, source_id, content=None, filename="劳动法.txt", **params):
    if content is None:
        content = law_text()
    return await client.post(
        "/api/v1/documents",
        params={"source_id": source_id, "filename": filename, "sensitivity": "public", **params},
        content=content,
        headers={"Content-Type": "application/octet-stream"},
    )


async def setup_org(make_client, make_user):
    """新建组织并登记来源，返回 (知识管理员, 编辑, 来源 ID)。"""
    admin = await make_user("knowledge_admin")
    editor = await make_user("editor", organization_id=admin.organization_id)
    async with make_client() as client:
        await login(client, admin.username)
        response = await create_source(client)
    assert response.status_code == 201
    return admin, editor, response.json()["id"]


async def test_import_registers_document_job_and_audit(
    make_client, make_user, storage, session_factory
):
    _, editor, source_id = await setup_org(make_client, make_user)

    async with make_client() as client:
        await login(client, editor.username)
        content = law_text()
        imported = await import_file(client, source_id, content)
        assert imported.status_code == 202
        body = imported.json()
        document_id = body["document"]["id"]
        job = await client.get(f"/api/v1/jobs/{body['job_id']}")
        listed = await client.get("/api/v1/documents", params={"source_id": source_id})
        download = await client.get(f"/api/v1/documents/{document_id}/content")

    document = body["document"]
    assert document["original_filename"] == "劳动法.txt"
    assert document["media_type"] == "text/plain"
    assert document["size_bytes"] == len(content)
    assert document["acquired_at"] is None
    # 解析 worker 尚未实现，任务只登记为 pending
    assert job.status_code == 200
    assert job.json()["status"] == "pending"
    assert job.json()["job_type"] == "document.parse"
    assert [item["id"] for item in listed.json()] == [document_id]

    assert download.status_code == 200
    assert download.content == content
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["x-content-type-options"] == "nosniff"
    assert len(stored_objects(storage)) == 1

    async with session_factory() as session:
        actions = list(
            await session.scalars(
                select(AuditEvent.action).where(AuditEvent.resource_id == UUID(document_id))
            )
        )
        outbox = await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.payload["document_id"].astext == document_id)
        )
    assert sorted(actions) == ["document.downloaded", "document.imported"]
    assert outbox == 1


async def test_duplicate_and_invalid_files_rejected_without_leftovers(
    make_client, make_user, storage, session_factory
):
    _, editor, source_id = await setup_org(make_client, make_user)

    async with make_client() as client:
        await login(client, editor.username)
        content = law_text()
        first = await import_file(client, source_id, content)
        # 同一内容重复导入被拒：原件按 sha256 全库唯一（设计 21.2）
        duplicate = await import_file(client, source_id, content, filename="另一个名字.txt")
        executable = await import_file(client, source_id, b"MZ\x90\x00", filename="a.exe")
        mismatch = await import_file(client, source_id, b"%PDF-1.7", filename="a.docx")
        empty = await import_file(client, source_id, b"")
        unknown_source = await import_file(client, "00000000-0000-0000-0000-000000000000", b"other")

    assert first.status_code == 202
    assert duplicate.status_code == 409
    assert executable.status_code == mismatch.status_code == empty.status_code == 422
    assert unknown_source.status_code == 404
    # 被拒绝的文件不落盘，也不登记
    assert len(stored_objects(storage)) == 1
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(SourceArtifact)
            .where(SourceArtifact.sha256 == hashlib.sha256(content).hexdigest())
        )
    assert count == 1


async def test_upload_size_limit(make_client, make_user, storage, monkeypatch):
    _, editor, source_id = await setup_org(make_client, make_user)
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 16)

    async with make_client() as client:
        await login(client, editor.username)
        response = await import_file(client, source_id, b"x" * 17)

    assert response.status_code == 413
    assert stored_objects(storage) == []


async def test_confidential_must_stay_restricted(make_client, make_user, storage):
    admin, editor, source_id = await setup_org(make_client, make_user)

    async with make_client() as client, make_client() as admin_client:
        await login(client, editor.username)
        open_confidential = await import_file(client, source_id, sensitivity="confidential")
        restricted = await import_file(
            client, source_id, sensitivity="confidential", access_scope="restricted"
        )
        document_id = restricted.json()["document"]["id"]

        await login(admin_client, admin.username)
        widen = await admin_client.put(
            f"/api/v1/documents/{document_id}/access", json={"access_scope": "organization"}
        )

    assert open_confidential.status_code == 422
    assert restricted.status_code == 202
    assert widen.status_code == 422


async def test_restricted_document_hidden_until_granted(make_client, make_user, storage):
    admin, editor, source_id = await setup_org(make_client, make_user)
    reader = await make_user("reader", organization_id=admin.organization_id)

    async with (
        make_client() as editor_client,
        make_client() as reader_client,
        make_client() as admin_client,
    ):
        await login(editor_client, editor.username)
        imported = await import_file(editor_client, source_id, access_scope="restricted")
        document_id = imported.json()["document"]["id"]
        job_id = imported.json()["job_id"]
        # 创建者自动获得授权
        owner_view = await editor_client.get(f"/api/v1/documents/{document_id}")

        await login(reader_client, reader.username)
        await login(admin_client, admin.username)

        async def reader_sees() -> tuple[int, int, int, bool]:
            # 按本用例的来源过滤：/documents 默认 limit=50 且按随机 UUID 排序，测试库累积后
            # 不过滤就可能取不到本用例的原件（公共数据全库共享，列表会越来越长）
            listed = await reader_client.get("/api/v1/documents", params={"source_id": source_id})
            return (
                (await reader_client.get(f"/api/v1/documents/{document_id}")).status_code,
                (await reader_client.get(f"/api/v1/documents/{document_id}/content")).status_code,
                (await reader_client.get(f"/api/v1/jobs/{job_id}")).status_code,
                document_id in {item["id"] for item in listed.json()},
            )

        before = await reader_sees()
        missing = await reader_client.get("/api/v1/documents/00000000-0000-0000-0000-000000000000")
        grant_url = f"/api/v1/documents/{document_id}/grants/{reader.id}"
        granted = await admin_client.put(grant_url)
        during = await reader_sees()
        revoked = await admin_client.delete(grant_url)
        after = await reader_sees()
        revoke_again = await admin_client.delete(grant_url)

    assert owner_view.status_code == 200
    assert before == (404, 404, 404, False)
    missing_body = missing.json()
    assert missing_body["code"] == "not_found"
    assert missing_body["message"] == "Document not found"
    assert missing_body["trace_id"]
    assert granted.status_code == 200
    assert during == (200, 200, 200, True)
    assert revoked.status_code == 204
    assert after == before
    assert revoke_again.status_code == 404


async def test_public_document_is_readable_across_organizations(make_client, make_user, storage):
    """原件属公共法律数据、全局共享（设计 21.2）：跨组织可读，来源全局可见。"""
    _, editor, source_id = await setup_org(make_client, make_user)
    outsider_admin, outsider_editor, _ = await setup_org(make_client, make_user)

    async with make_client() as client:
        await login(client, editor.username)
        imported = (await import_file(client, source_id)).json()
    document_id = imported["document"]["id"]

    async with make_client() as outsider_client, make_client() as admin_client:
        await login(outsider_client, outsider_editor.username)
        responses = (
            await outsider_client.get(f"/api/v1/documents/{document_id}"),
            await outsider_client.get(f"/api/v1/documents/{document_id}/content"),
            await outsider_client.get(f"/api/v1/jobs/{imported['job_id']}"),
        )
        sources = await outsider_client.get("/api/v1/sources")
        await login(admin_client, outsider_admin.username)
        # knowledge_admin 可管理公共原件的授权；管理授权不等于可阅读内容
        grant = await admin_client.put(
            f"/api/v1/documents/{document_id}/grants/{outsider_editor.id}"
        )

    assert [r.status_code for r in responses] == [200, 200, 200]
    assert source_id in {item["id"] for item in sources.json()}
    assert grant.status_code == 200


async def test_restricted_document_still_requires_a_grant(make_client, make_user, storage):
    """restricted 原件仍需显式授权（设计 21.2）：全局共享不放开受限资料。"""
    _, editor, source_id = await setup_org(make_client, make_user)
    _, outsider_editor, _ = await setup_org(make_client, make_user)

    async with make_client() as client:
        await login(client, editor.username)
        imported = (await import_file(client, source_id, access_scope="restricted")).json()
    document_id = imported["document"]["id"]

    async with make_client() as outsider_client:
        await login(outsider_client, outsider_editor.username)
        read = await outsider_client.get(f"/api/v1/documents/{document_id}")
        content = await outsider_client.get(f"/api/v1/documents/{document_id}/content")

    assert read.status_code == 404
    assert content.status_code == 404


async def test_download_refuses_tampered_or_missing_file(
    make_client, make_user, storage, session_factory
):
    _, editor, source_id = await setup_org(make_client, make_user)

    async with make_client() as client:
        await login(client, editor.username)
        document_id = (await import_file(client, source_id)).json()["document"]["id"]
        async with session_factory() as session:
            key = await session.scalar(
                select(SourceArtifact.object_key).where(SourceArtifact.id == UUID(document_id))
            )
        path = storage._path(key)

        path.write_bytes(b"tampered")
        tampered = await client.get(f"/api/v1/documents/{document_id}/content")
        path.unlink()
        missing = await client.get(f"/api/v1/documents/{document_id}/content")

    assert tampered.status_code == missing.status_code == 503
    async with session_factory() as session:
        downloads = await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.resource_id == UUID(document_id),
                AuditEvent.action == "document.downloaded",
            )
        )
    # 未交付文件时不记下载
    assert downloads == 0


async def test_source_validation_and_duplicates(make_client, make_user):
    admin = await make_user("knowledge_admin")
    name = f"来源-{uuid4().hex[:8]}"

    async with make_client() as client:
        await login(client, admin.username)
        created = await create_source(client, name=name)
        # 来源名全库唯一（设计 21.2）：同名重复登记被拒
        duplicate = await create_source(client, name=name)
        no_license = await create_source(client, name="甲", license_note="  ")
        bad_url = await create_source(client, name="乙", url="javascript:alert(1)")
        naive_time = await create_source(client, name="丙", last_checked_at="2026-01-01T00:00:00")

    assert created.status_code == 201
    # 未核查时为未知，不用入库时间代替
    assert created.json()["last_checked_at"] is None
    assert duplicate.status_code == 409
    assert no_license.status_code == bad_url.status_code == naive_time.status_code == 422


async def test_update_source_records_changes_with_audit(make_client, make_user, session_factory):
    """更正来源登记信息（FR-01、设计 §20.3）：写审计含前后值，授权说明不可清空。"""
    admin = await make_user("knowledge_admin")
    async with make_client() as client:
        await login(client, admin.username)
        created = await create_source(client, name=f"待更正来源-{uuid4().hex[:8]}")
        assert created.status_code == 201
        source_id = created.json()["id"]
        other = await create_source(client, name=f"另一个来源-{uuid4().hex[:8]}")
        other_name = other.json()["name"]

        updated = await client.put(
            f"/api/v1/sources/{source_id}",
            json={
                "url": "https://flk.npc.gov.cn/index",
                "publisher": "全国人大常委会办公厅",
                "license_note": "官方渠道公开发布；电子文本与标准文本不一致时以标准文本为准。",
            },
        )
        cleared = await client.put(f"/api/v1/sources/{source_id}", json={"license_note": None})
        renamed = await client.put(f"/api/v1/sources/{source_id}", json={"name": other_name})
        empty = await client.put(f"/api/v1/sources/{source_id}", json={})

    assert updated.status_code == 200
    body = updated.json()
    assert body["url"] == "https://flk.npc.gov.cn/index"
    assert body["publisher"] == "全国人大常委会办公厅"
    # 未提交的字段保持不变
    assert body["source_type"] == "official"
    assert body["name"] == created.json()["name"]
    # 授权说明是「未登记授权说明的来源不得入库」的判据，不能清空
    assert cleared.status_code == 422
    assert renamed.status_code == 409
    assert empty.status_code == 422

    async with session_factory() as session:
        event = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "source.updated",
                AuditEvent.resource_id == UUID(source_id),
            )
        )
        assert event.payload["changed_fields"] == ["license_note", "publisher", "url"]
        # 留痕要能看出原来写的是什么，而不只是改了哪些字段
        assert event.payload["before"]["url"] == "https://flk.npc.gov.cn"
        assert event.payload["after"]["url"] == "https://flk.npc.gov.cn/index"
        assert event.payload["before"]["publisher"] is None
        assert event.payload["after"]["publisher"] == "全国人大常委会办公厅"


async def test_update_source_requires_source_manage(make_client, make_user):
    admin = await make_user("knowledge_admin")
    editor = await make_user("editor", organization_id=admin.organization_id)
    async with make_client() as client:
        await login(client, admin.username)
        source_id = (await create_source(client)).json()["id"]

    async with make_client() as client:
        await login(client, editor.username)
        denied = await client.put(
            f"/api/v1/sources/{source_id}", json={"publisher": "全国人大常委会办公厅"}
        )

    assert denied.status_code == 403


async def test_job_lookup_limited_to_parse_jobs(make_client, make_user, session_factory):
    editor = await make_user("editor")
    async with session_factory() as session, session.begin():
        job = Job(
            job_type="other.task",
            payload={},
            idempotency_key=f"other-{editor.id}",
            status="pending",
            attempt_count=0,
            max_attempts=1,
        )
        session.add(job)

    async with make_client() as client:
        await login(client, editor.username)
        response = await client.get(f"/api/v1/jobs/{job.id}")

    assert response.status_code == 404


async def test_set_source_moves_document_and_audits(
    make_client, make_user, storage, session_factory
):
    """更正原件来源归属（FR-01、设计 §20.3）：改归属、写审计、重复设置是空操作。

    必须请求 ``storage`` fixture：否则上传走真实的 ``get_storage()``，把原件写进仓库的
    ``data/artifacts/``（污染开发环境）。
    """
    admin, _, source_a = await setup_org(make_client, make_user)
    async with make_client() as client:
        await login(client, admin.username)
        created = await create_source(client, name=f"中国法律资源库-{uuid4().hex[:8]}")
        assert created.status_code == 201
        source_b = created.json()["id"]
        imported = await import_file(client, source_a)
        assert imported.status_code == 202
        document_id = imported.json()["document"]["id"]

        moved = await client.put(
            f"/api/v1/documents/{document_id}/source", json={"source_id": source_b}
        )
        # 目标来源不存在时 404
        unknown = await client.put(
            f"/api/v1/documents/{document_id}/source", json={"source_id": str(uuid4())}
        )
        # 已是该来源：空操作，不重复写审计
        again = await client.put(
            f"/api/v1/documents/{document_id}/source", json={"source_id": source_b}
        )

    assert moved.status_code == 200
    assert moved.json()["source_id"] == source_b
    assert unknown.status_code == 404
    assert again.status_code == 200

    async with session_factory() as session:
        changes = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "document.source_changed",
                    AuditEvent.resource_id == UUID(document_id),
                )
            )
        ).all()
        assert len(changes) == 1
        assert changes[0].payload["before_source_id"] == source_a
        assert changes[0].payload["after_source_id"] == source_b
        # 留痕要能看出改成了哪个来源，而不只是 id
        assert changes[0].payload["after_source_name"].startswith("中国法律资源库")


async def test_set_source_requires_source_manage(make_client, make_user, storage, session_factory):
    """编辑有 document.write 但没有 source.manage（需求第 3 节），不得改来源归属。

    必须请求 ``storage`` fixture：否则上传走真实的 ``get_storage()``，把原件写进仓库的
    ``data/artifacts/``（污染开发环境）。
    """
    admin, editor, source_a = await setup_org(make_client, make_user)
    async with make_client() as client:
        await login(client, admin.username)
        source_b = (await create_source(client, name=f"另一来源-{uuid4().hex[:8]}")).json()["id"]
        imported = await import_file(client, source_a)
        document_id = imported.json()["document"]["id"]

    async with make_client() as client:
        await login(client, editor.username)
        denied = await client.put(
            f"/api/v1/documents/{document_id}/source", json={"source_id": source_b}
        )

    assert denied.status_code == 403

    async with session_factory() as session:
        artifact = await session.get(SourceArtifact, UUID(document_id))
        assert artifact.source_id == UUID(source_a)
