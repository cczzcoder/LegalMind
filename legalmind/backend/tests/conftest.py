import os
import random
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = "postgresql+asyncpg://unused:unused@localhost/unused"
os.environ["TRUSTED_PROXIES"] = ""
# ⚠️ **测试不连真 Redis**：`CACHE_URL` 从环境继承（本机开发时 shell 里就有），不显式清空的话
# 测试会去连真实缓存——既依赖外部服务，又会污染真实数据。需要缓存的用例自己注入内存替身。
os.environ["CACHE_URL"] = ""

from cryptography.fernet import Fernet

# 测试专用密钥，每次运行随机生成
os.environ["MFA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def migrated_test_database():
    """把 TEST_DATABASE_URL 指向的库迁移到最新版本；未配置时跳过集成测试。"""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set; skipping PostgreSQL integration tests")

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        check=True,
    )
    return TEST_DATABASE_URL


@pytest.fixture
async def engine(migrated_test_database):
    # asyncpg 连接绑定事件循环，每个测试使用独立引擎
    engine = create_async_engine(migrated_test_database, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def make_client(session_factory):
    from app.core.database import get_session
    from app.main import app

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session

    def make(ip: str | None = None) -> httpx.AsyncClient:
        # 默认使用随机来源 IP，避免按 IP 的失败计数在测试间累积
        if ip is None:
            ip = f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(ip, 12345)),
            base_url="http://test",
        )

    yield make
    app.dependency_overrides.clear()


@pytest.fixture
def make_user(session_factory):
    from app.models import Organization
    from app.modules.identity import service
    from app.modules.identity.schemas import CreateUser
    from tests.helpers import PASSWORD

    async def make(*roles: str, organization_id=None):
        async with session_factory() as session:
            if organization_id is None:
                async with session.begin():
                    organization = Organization(name=f"org-{uuid4()}")
                    session.add(organization)
                organization_id = organization.id

            return await service.create_user(
                session,
                organization_id,
                None,
                CreateUser(
                    username=f"u-{uuid4().hex[:12]}",
                    password=PASSWORD,
                    roles=list(roles),
                ),
            )

    return make


@pytest.fixture
def semantics_pass(monkeypatch):
    """把 §9.3 **第二层语义核验**换成「通过」。

    链路上有**两次**模型调用：生成结论、语义判官。只关心前者的用例用这个 fixture——
    判官本身由 `tests/test_semantics.py`（服务端怎么处理判官的结论）与
    `scripts/evaluate_semantics.py`（判官本人的水平）负责。在这里再糊一个「判官替身」
    只会把两件事搅在一起，而且判官要求交出**条文原句**才能判支持，替身很难通用。
    """
    from app.modules.answering import semantics
    from app.modules.answering.semantics import SemanticReview

    monkeypatch.setattr(semantics, "review", lambda *_a, **_k: SemanticReview(ok=True, reviews=()))
