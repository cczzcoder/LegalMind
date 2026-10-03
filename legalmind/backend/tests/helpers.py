"""集成测试共用的登录、MFA 与数据构造辅助函数。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pyotp

from app.core.security import CSRF_HEADER, hash_password

PASSWORD = "correct horse battery"


def sha256_hex() -> str:
    return uuid4().hex + uuid4().hex


async def seed_parse_chain(session) -> tuple:
    """建一条 org → user → source → artifact → parse_revision → chunk 的合法链路。

    返回 (organization, user, source, artifact, parse_revision, chunk)。
    """
    from app.models import Chunk, Organization, ParseRevision, Source, SourceArtifact, User

    organization = Organization(name=f"org-{uuid4()}")
    session.add(organization)
    await session.flush()

    user = User(
        organization_id=organization.id,
        username=f"u-{uuid4().hex[:12]}",
        password_hash=hash_password("test-password"),
        is_active=True,
    )
    session.add(user)
    await session.flush()

    source = Source(
        name=f"src-{uuid4().hex[:8]}",
        source_type="official",
        trust_level="high",
        license_note="测试来源",
        created_by=user.id,
    )
    session.add(source)
    await session.flush()

    artifact = SourceArtifact(
        source_id=source.id,
        object_key=uuid4().hex + uuid4().hex,
        sha256=sha256_hex(),
        size_bytes=1024,
        media_type="application/pdf",
        original_filename="example.pdf",
        sensitivity="internal",
        access_scope="organization",
        created_by=user.id,
    )
    session.add(artifact)
    await session.flush()

    parse_revision = ParseRevision(
        artifact_id=artifact.id,
        parser="test-parser",
        parser_version="1",
        config_version="v1",
        text_sha256=sha256_hex(),
        quality_status="ok",
        created_by=user.id,
    )
    session.add(parse_revision)
    await session.flush()

    chunk = Chunk(
        parse_revision_id=parse_revision.id,
        ordinal=0,
        text="第一条 测试条文",
        text_sha256=sha256_hex(),
    )
    session.add(chunk)
    await session.flush()
    return organization, user, source, artifact, parse_revision, chunk


# 用户名 -> (TOTP 密钥, 最近使用的时间步)，模拟用户手中的认证器
AUTHENTICATORS: dict[str, tuple[str, int]] = {}


def next_totp(username: str) -> str:
    # 已用过的时间步会被拒绝，同一时间窗内再次登录时使用下一个时间步（服务端允许 +1 偏差）
    secret, last = AUTHENTICATORS[username]
    totp = pyotp.TOTP(secret)
    timecode = max(totp.timecode(datetime.now(UTC)), last + 1)
    AUTHENTICATORS[username] = (secret, timecode)
    return totp.generate_otp(timecode)


async def enroll_mfa(client: httpx.AsyncClient, username: str) -> list[str]:
    enrollment = await client.post("/api/v1/auth/mfa/enroll")
    assert enrollment.status_code == 200
    AUTHENTICATORS[username] = (enrollment.json()["secret"], -1)
    confirmed = await client.post("/api/v1/auth/mfa/confirm", json={"code": next_totp(username)})
    assert confirmed.status_code == 200
    return confirmed.json()["recovery_codes"]


async def login(
    client: httpx.AsyncClient,
    username: str,
    password: str = PASSWORD,
    complete_mfa: bool = True,
):
    """登录；complete_mfa 为真时按服务端要求自动绑定或验证 TOTP。"""
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    if response.status_code == 200:
        client.headers[CSRF_HEADER] = response.json()["csrf_token"]
        status = response.json()["mfa_status"]
        if complete_mfa and status == "enroll":
            await enroll_mfa(client, username)
        elif complete_mfa and status == "verify":
            verified = await client.post(
                "/api/v1/auth/mfa/verify", json={"code": next_totp(username)}
            )
            assert verified.status_code == 204
    return response


async def create_page(client: httpx.AsyncClient, **extra):
    return await client.post(
        "/api/v1/wiki/pages",
        json={"title": "测试页面", "body": "正文", **extra},
    )


async def setup_source(make_client, make_user) -> tuple:
    """新建组织与来源，返回 (知识管理员, 编辑, 来源 ID)。来源登记需 knowledge_admin。"""
    admin = await make_user("knowledge_admin")
    editor = await make_user("editor", organization_id=admin.organization_id)
    async with make_client() as client:
        await login(client, admin.username)
        # 来源名全库唯一（设计 21.2）；测试用唯一名避免互相冲突
        response = await client.post(
            "/api/v1/sources",
            json={
                "name": f"国家法律法规数据库-{uuid4().hex[:8]}",
                "source_type": "official",
                "trust_level": "high",
                "url": "https://flk.npc.gov.cn",
                "license_note": "官方公开发布的法律法规文本",
            },
        )
    assert response.status_code == 201, response.text
    return admin, editor, response.json()["id"]


async def import_document_into_source(
    make_client, editor, source_id, content: bytes, filename: str
) -> UUID:
    """在**已登记**的来源下导入一个原件，返回原件 ID（用于构造「同来源多份原件」的场景）。"""
    async with make_client() as client:
        await login(client, editor.username)
        imported = await client.post(
            "/api/v1/documents",
            params={"source_id": source_id, "filename": filename, "sensitivity": "public"},
            content=content,
            headers={"Content-Type": "application/octet-stream"},
        )
    assert imported.status_code == 202, imported.text
    return UUID(imported.json()["document"]["id"])


async def import_document_for_parsing(
    make_client, make_user, content: bytes, filename: str
) -> tuple:
    """登记来源并导入一个原件，返回 (导入者, 原件 ID)。

    原件按 sha256 全库唯一（设计 21.2），调用方须保证 ``content`` 每次不同。
    """
    _, editor, source_id = await setup_source(make_client, make_user)
    document_id = await import_document_into_source(
        make_client, editor, source_id, content, filename
    )
    return editor, document_id


def build_minimal_pdf(pages: list[list[str]], *, marker: str | None = None) -> bytes:
    """构造一个最小可解析 PDF：每页若干行 Helvetica 文本，交叉引用表偏移正确。

    纯内置实现，不引入额外依赖；内容确定，便于断言定位字段。仅用于测试。

    ``marker`` 会作为 PDF 内容流注释写入首页——解析器忽略注释，但字节随之变化，
    便于让原件 sha256 在测试间保持唯一（设计 §21.2 要求原件全库唯一）。
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)  # 1 基对象号

    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(b"")  # /Pages 占位，最后回填
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    kids: list[int] = []
    for page_index, lines in enumerate(pages):
        content = "BT\n/F1 24 Tf\n24 TL\n72 700 Td\n"
        for index, line in enumerate(lines):
            if index:
                content += "T*\n"
            escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            content += f"({escaped}) Tj\n"
        content += "ET\n"
        if marker and page_index == 0:
            content += f"% {marker}\n"
        stream = content.encode("latin-1")
        content_number = add(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream"
        )
        page_number = add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents "
            + str(content_number).encode()
            + b" 0 R >>"
        )
        kids.append(page_number)

    objects[1] = (
        b"<< /Type /Pages /Kids ["
        + b" ".join(f"{kid} 0 R".encode() for kid in kids)
        + b"] /Count "
        + str(len(kids)).encode()
        + b" >>"
    )

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)
