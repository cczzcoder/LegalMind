"""ORM 基类与建表约束的共用小工具。

⚠️ `Base` 定义在这里、**不在 `app.core.database`**，是为了断开 `core ↔ models` 的循环依赖。
`app/core/database.py` 在 import 时就会 `create_async_engine(get_settings().database_url)`，
若 `Base` 留在那里，则「import 模型」会隐含要求 `DATABASE_URL` 存在
（`alembic` 曾因此报 `database_url Field required`）。搬过来之后，
`import app.models` 不再创建数据库引擎。
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ('" + "', '".join(values) + "')"
