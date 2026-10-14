from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

# ⚠️ `Base` **不在这里**，在 `app/models/base.py`。原因：本模块在 import 时就会
# `create_async_engine(get_settings().database_url)`，若 `Base` 也定义在这里，则
# `app.models` 必须 import 本模块 ⇒ 形成 `core ↔ models` 的循环依赖，而且
# 「import 模型」会隐含要求 `DATABASE_URL` 存在（`alembic` 曾因此报
# `database_url Field required`）。搬走之后 `import app.models` 不再创建引擎。
# 需要 `Base` 请 `from app.models import Base`。

engine = create_async_engine(
    get_settings().database_url,
    pool_pre_ping=True,
)

SessionFactory = async_sessionmaker(
    engine,
    expire_on_commit=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session
