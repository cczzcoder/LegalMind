"""为条款版本计算向量并落库（设计 8.2、8.3、14.1）。

读 ``provision_versions.text``（**脱敏文本**），用本地模型编码后写入 ``provision_embeddings``。
唯一键是 ``(条款版本, 模型)``，所以同一批条款可以**并存多个模型的向量**，重复跑只覆盖自己那一份。

模型调用**不放在事务里**（设计 §12.1）：先读完条款、再整批编码、最后一次性写入。

跑法::

    set -a && . ./.env && set +a
    export DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:5432/${POSTGRES_DB}"
    .venv/Scripts/python.exe scripts/embed_provisions.py --model BAAI/bge-m3
"""

import argparse
import asyncio
import os
import sys
import time
from uuid import uuid4

os.environ.setdefault("APP_ENV", "development")

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

if not os.environ.get("DATABASE_URL"):
    print(
        "DATABASE_URL 未设置。先在 legalmind/ 下加载环境再运行：\n"
        "  set -a && . ./.env && set +a\n"
        '  export DATABASE_URL="postgresql+asyncpg://$POSTGRES_USER:$POSTGRES_PASSWORD'
        '@127.0.0.1:5432/$POSTGRES_DB"\n'
        "  .venv/Scripts/python.exe scripts/embed_provisions.py --model BAAI/bge-m3",
        file=sys.stderr,
    )
    raise SystemExit(2)

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.adapters import embedding
from app.core.database import SessionFactory
from app.models import ProvisionEmbedding, ProvisionVersion


async def _load_texts(limit: int | None) -> list[tuple[str, str]]:
    async with SessionFactory() as session:
        statement = select(ProvisionVersion.id, ProvisionVersion.text).order_by(
            ProvisionVersion.created_at, ProvisionVersion.id
        )
        if limit is not None:
            statement = statement.limit(limit)
        return [(str(pid), text) for pid, text in (await session.execute(statement)).all()]


async def _store(rows: list[dict]) -> None:
    async with SessionFactory() as session, session.begin():
        for start in range(0, len(rows), 200):
            batch = rows[start : start + 200]
            statement = pg_insert(ProvisionEmbedding).values(batch)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["provision_version_id", "model"],
                    set_={
                        "embedding": statement.excluded.embedding,
                        "dimensions": statement.excluded.dimensions,
                    },
                )
            )


async def run(model_name: str, batch_size: int, limit: int | None) -> int:
    spec = embedding.model(model_name)
    texts = await _load_texts(limit)
    if not texts:
        print("没有条款版本可编码。", file=sys.stderr)
        return 2
    print(f"模型 {spec.name}（{spec.dimensions} 维 / {spec.max_tokens} 位置）→ {len(texts)} 条条款")
    started = time.perf_counter()
    vectors = embedding.encode(
        spec.name, [text for _pid, text in texts], batch_size=batch_size, progress=True
    )
    seconds = time.perf_counter() - started
    rows = [
        {
            "id": uuid4(),
            "provision_version_id": pid,
            "model": spec.name,
            "dimensions": len(vector),
            "embedding": vector,
        }
        for (pid, _text), vector in zip(texts, vectors, strict=True)
    ]
    await _store(rows)
    print(f"已写入 {len(rows)} 条向量（{seconds:.1f}s，{len(rows) / seconds:.1f} 条/秒）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="为条款版本计算向量并落库")
    parser.add_argument("--model", default=embedding.DEFAULT_MODEL, help="嵌入模型标识")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--limit", type=int, default=None, help="只编码前 N 条（试跑用）")
    args = parser.parse_args()
    return asyncio.run(run(args.model, args.batch_size, args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
