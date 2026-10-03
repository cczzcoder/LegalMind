"""来源登记与原始资料导入集成测试（FR-01、FR-02、FR-10、FR-11、设计 7.3）：需要 TEST_DATABASE_URL。"""

from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.adapters.storage import LocalFileStorage, get_storage
from app.core.config import get_settings
from app.models import AuditEvent, Job, OutboxEvent, SourceArtifact
from tests.helpers import login

pytestmark = pytest.mark.anyio

LAW_TEXT = "中华人民共和国劳动法\n第一条　为了保护劳动者的合法权益……".encode()


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
            "name": "国家法律法规数据库",
            "source_type": "official",
            "trust_level": "high",
            "url": "https://flk.npc.gov.cn",
            "license_note": "官方公开发布的法律法规文本",
            **extra,
        },
    )


async def import_file(client, source_id, content=LAW_TEXT, filename="劳动法.txt", **params):
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
        imported = await import_file(client, source_id)
        assert imported.status_code == 202
        body = imported.json()
        document_id = body["document"]["id"]
        job = await client.get(f"/api/v1/jobs/{body['job_id']}")
        listed = await client.get("/api/v1/documents", params={"source_id": source_id})
        download = await client.get(f"/api/v1/documents/{document_id}/content")

    document = body["document"]
    assert document["original_filename"] == "劳动法.txt"
    assert document["media_type"] == "text/plain"
    assert document["size_bytes"] == len(LAW_TEXT)
    assert document["acquired_at"] is None
    # 解析 worker 尚未实现，任务只登记为 pending
    assert job.status_code == 200
    assert job.json()["status"] == "pending"
    assert job.json()["job_type"] == "document.parse"
    assert [item["id"] for item in listed.json()] == [document_id]

    assert download.status_code == 200
    assert download.content == LAW_TEXT
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
        first = await import_file(client, source_id)
        duplicate = await import_file(client, source_id, filename="另一个名字.txt")
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
            .where(SourceArtifact.organization_id == editor.organization_id)
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
            listed = await reader_client.get("/api/v1/documents")
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


async def test_other_organization_cannot_see_or_use(make_client, make_user, storage):
    _, editor, source_id = await setup_org(make_client, make_user)
    outsider_admin, outsider_editor, _ = await setup_org(make_client, make_user)

    async with make_client() as client:
        await login(client, editor.username)
        imported = (await import_file(client, source_id)).json()
    document_id = imported["document"]["id"]

    async with make_client() as editor_client, make_client() as admin_client:
        await login(editor_client, outsider_editor.username)
        responses = (
            await editor_client.get(f"/api/v1/documents/{document_id}"),
            await editor_client.get(f"/api/v1/documents/{document_id}/content"),
            await editor_client.get(f"/api/v1/jobs/{imported['job_id']}"),
            # 不能把文件挂到别的组织的来源下
            await import_file(editor_client, source_id, b"cross-org"),
        )
        sources = await editor_client.get("/api/v1/sources")
        await login(admin_client, outsider_admin.username)
        grant = await admin_client.put(
            f"/api/v1/documents/{document_id}/grants/{outsider_editor.id}"
        )

    assert [r.status_code for r in responses] == [404, 404, 404, 404]
    assert grant.status_code == 404
    assert source_id not in {item["id"] for item in sources.json()}


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

    async with make_client() as client:
        await login(client, admin.username)
        created = await create_source(client)
        duplicate = await create_source(client)
        no_license = await create_source(client, name="甲", license_note="  ")
        bad_url = await create_source(client, name="乙", url="javascript:alert(1)")
        naive_time = await create_source(client, name="丙", last_checked_at="2026-01-01T00:00:00")

    assert created.status_code == 201
    # 未核查时为未知，不用入库时间代替
    assert created.json()["last_checked_at"] is None
    assert duplicate.status_code == 409
    assert no_license.status_code == bad_url.status_code == naive_time.status_code == 422


async def test_job_lookup_limited_to_parse_jobs(make_client, make_user, session_factory):
    editor = await make_user("editor")
    async with session_factory() as session, session.begin():
        job = Job(
            organization_id=editor.organization_id,
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
