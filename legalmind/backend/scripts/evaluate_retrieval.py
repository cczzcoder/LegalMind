"""中文关键词检索方案选型（设计 §8.2、§8.3、§17.1）。

§8.3 要求关键词方案「用测试集选型」。本脚本把 ``app.modules.retrieval.keyword`` 里的候选实现
跑在金标准上，比 **Recall@k / MRR / 误报 / 耗时**，报告写到 ``evaluations/reports/``。

**金标准**（``evaluations/datasets/retrieval_queries.json``）：期望集 = 全库中「去空白文本」
包含全部 terms 的（法, 条）。去空白是为了不被 PDF 折行空格影响（实测「民用航 空器」），
每条查询的出处记在 ``note`` 里。它衡量的是**词面命中**，不是语义召回——语义那一层要等向量
通路，不能拿它下结论。

跑法::

    set -a && . ./.env && set +a
    export DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:5432/${POSTGRES_DB}"
    .venv/Scripts/python.exe scripts/evaluate_retrieval.py [--top-k 20] [--json]
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("APP_ENV", "development")

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

DEFAULT_DATASET = BACKEND_DIR.parent / "evaluations" / "datasets" / "retrieval_queries.json"
DEFAULT_OUT = BACKEND_DIR.parent / "evaluations" / "reports"

if not os.environ.get("DATABASE_URL"):
    print(
        "DATABASE_URL 未设置。先在 legalmind/ 下加载环境再运行：\n"
        "  set -a && . ./.env && set +a\n"
        '  export DATABASE_URL="postgresql+asyncpg://$POSTGRES_USER:$POSTGRES_PASSWORD'
        '@127.0.0.1:5432/$POSTGRES_DB"\n'
        "  .venv/Scripts/python.exe scripts/evaluate_retrieval.py",
        file=sys.stderr,
    )
    raise SystemExit(2)

from sqlalchemy import Integer, func, select
from sqlalchemy import text as sql_text

from app.core.database import SessionFactory
from app.core.security import Principal
from app.models import (
    LegalInstrument,
    LegalVersion,
    ProvisionIdentity,
    ProvisionVersion,
    SourceArtifact,
)
from app.modules.authorization.service import AuthorizationService
from app.modules.retrieval.keyword import STRATEGIES, Strategy

_ARTICLE_SORT = func.regexp_replace(ProvisionIdentity.provision_number, "[^0-9].*$", "", "g").cast(
    Integer
)


def _load_queries(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["queries"]


async def _prepare(session) -> None:
    """候选 trigram / word_trigram 需要 pg_trgm。它随 PostgreSQL 提供，不是新增常驻组件（§18）。"""
    await session.execute(sql_text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    await session.commit()


def _statement(strategy: Strategy, query: str, principal: Principal, limit: int):
    return (
        select(LegalInstrument.title, ProvisionIdentity.provision_number)
        .select_from(ProvisionVersion)
        .join(ProvisionIdentity, ProvisionVersion.provision_identity_id == ProvisionIdentity.id)
        .join(LegalVersion, ProvisionVersion.legal_version_id == LegalVersion.id)
        .join(LegalInstrument, LegalVersion.instrument_id == LegalInstrument.id)
        .join(SourceArtifact, LegalVersion.artifact_id == SourceArtifact.id)
        .where(AuthorizationService.document_scope(principal))
        .where(strategy.match(query))
        .order_by(*strategy.order(query), LegalInstrument.title, _ARTICLE_SORT)
        .limit(limit)
    )


async def _run_query(session, strategy: Strategy, query: str, principal: Principal, top_k: int):
    """返回 top-k 的 (法, 条) 列表；同一（法, 条）可能有多个版本，去重后取前 top_k。"""
    started = time.perf_counter()
    rows = (await session.execute(_statement(strategy, query, principal, top_k * 3))).all()
    elapsed_ms = (time.perf_counter() - started) * 1000
    seen: list[tuple[str, str]] = []
    for title, number in rows:
        key = (title, number)
        if key not in seen:
            seen.append(key)
        if len(seen) == top_k:
            break
    return seen, elapsed_ms


def _score(hits: list[tuple[str, str]], expect: set[tuple[str, str]], top_k: int) -> dict:
    """单条查询的打分。命中集按 top_k 截断（超出 k 的不算）。"""
    limited = hits[:top_k]
    found = [hit for hit in limited if hit in expect]
    rank = next((index + 1 for index, hit in enumerate(limited) if hit in expect), None)
    return {
        "found": len(found),
        "rank": rank,
        "recall": (len(found) / len(expect)) if expect else None,
        "reciprocal_rank": (1.0 / rank) if rank else 0.0,
    }


async def evaluate(
    dataset: Path, top_k: int
) -> tuple[list[dict], dict[str, list[dict]], list[dict]]:
    queries = _load_queries(dataset)
    principal = Principal(organization_id=uuid4(), user_id=uuid4(), roles=frozenset())
    summaries: list[dict] = []
    details: dict[str, list[dict]] = {}

    async with SessionFactory() as session:
        await _prepare(session)
        for strategy in STRATEGIES:
            per_query: list[dict] = []
            for item in queries:
                expect = {(title, number) for title, number in item["expect"]}
                try:
                    hits, elapsed_ms = await _run_query(
                        session, strategy, item["query"], principal, top_k
                    )
                    score = _score(hits, expect, top_k)
                    error = None
                except Exception as failure:  # noqa: BLE001 - 候选失败要如实记下来，不能让整轮中断
                    hits, elapsed_ms, error = [], 0.0, f"{type(failure).__name__}: {failure}"
                    score = {"found": 0, "rank": None, "recall": None, "reciprocal_rank": 0.0}
                per_query.append(
                    {
                        "id": item["id"],
                        "type": item["type"],
                        "query": item["query"],
                        "expect": sorted(expect),
                        "hits": hits,
                        "elapsed_ms": round(elapsed_ms, 2),
                        **score,
                        "error": error,
                    }
                )
            scored = [row for row in per_query if row["recall"] is not None]
            total_expect = sum(len(row["expect"]) for row in scored)
            total_found = sum(row["found"] for row in scored)
            total_hits = sum(len(row["hits"]) for row in scored)
            summaries.append(
                {
                    "strategy": strategy.name,
                    "summary": strategy.summary,
                    "top_k": top_k,
                    "queries": len(per_query),
                    "scored_queries": len(scored),
                    "recall": round(total_found / total_expect, 4) if total_expect else 0.0,
                    # 返回的结果里有多少是真正期望的——召回高但夹带一堆无关条文时这里会掉下来
                    "precision": round(total_found / total_hits, 4) if total_hits else None,
                    "recall_at_1": round(
                        sum(row["recall"] for row in scored if row["rank"] == 1) / len(scored)
                        if scored
                        else 0.0,
                        4,
                    ),
                    "mrr": round(
                        sum(row["reciprocal_rank"] for row in scored) / len(scored)
                        if scored
                        else 0.0,
                        4,
                    ),
                    "no_hit_false_positives": sum(
                        1 for row in per_query if row["type"] == "no_hit" and row["hits"]
                    ),
                    "errors": sum(1 for row in per_query if row["error"]),
                    "avg_ms": round(
                        sum(row["elapsed_ms"] for row in per_query) / len(per_query), 2
                    ),
                }
            )
            details[strategy.name] = per_query
    return summaries, details, queries


def _markdown(summaries: list[dict], details: dict[str, list[dict]], top_k: int) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    out = [
        "# 中文关键词检索方案选型",
        "",
        f"生成时间：{now}　top-k：{top_k}",
        "",
        "期望集 = 全库中「去空白文本」包含全部 terms 的（法, 条）；衡量**词面命中**，不是语义召回。",
        "",
        "## 汇总",
        "",
        "| 方案 | Recall | 准确率 | Rank@1 命中率 | MRR | 无命中查询误报 | 错误 | 平均耗时 ms |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in summaries:
        out.append(
            f"| `{row['strategy']}` | {row['recall']} | {row['precision']} | "
            f"{row['recall_at_1']} | {row['mrr']} | "
            f"{row['no_hit_false_positives']} | {row['errors']} | {row['avg_ms']} |"
        )
    out += ["", "## 方案说明", ""]
    for row in summaries:
        out.append(f"- `{row['strategy']}`：{row['summary']}")
    out += ["", "## 逐条明细", ""]
    for name, rows in details.items():
        out.append(f"### {name}")
        out.append("")
        out.append("| id | 类型 | 查询 | 期望 | 命中 | 首个正确排名 | 耗时 ms |")
        out.append("| --- | --- | --- | --- | --- | --- | --- |")
        for row in rows:
            expect = (
                "、".join(f"{title[:8]}·{number}" for title, number in row["expect"]) or "（无）"
            )
            hits = (
                "、".join(f"{title[:8]}·{number}" for title, number in row["hits"][:5]) or "（无）"
            )
            out.append(
                f"| {row['id']} | {row['type']} | {row['query']} | {expect} | {hits} | "
                f"{row['rank'] or '—'} | {row['elapsed_ms']} |"
            )
        out.append("")
    return "\n".join(out) + "\n"


async def run(dataset: Path, out_dir: Path, top_k: int, print_json: bool) -> int:
    summaries, details, queries = await evaluate(dataset, top_k)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": str(dataset),
        "top_k": top_k,
        "queries": len(queries),
        "summary": summaries,
        "details": details,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"retrieval_eval_{stamp}.json"
    md_path = out_dir / f"retrieval_eval_{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_markdown(summaries, details, top_k), encoding="utf-8")

    header = f"{'方案':<18}{'Recall':>8}{'准确率':>8}{'Rank@1':>9}{'MRR':>8}{'误报':>6}{'错误':>6}{'ms':>8}"
    print(header)
    print("-" * len(header))
    for row in summaries:
        print(
            f"{row['strategy']:<18}{row['recall']:>8}{row['precision']!s:>8}"
            f"{row['recall_at_1']:>9}{row['mrr']:>8}"
            f"{row['no_hit_false_positives']:>6}{row['errors']:>6}{row['avg_ms']:>8}"
        )
    print(f"\nJSON: {json_path}\nMD:   {md_path}")
    if print_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="中文关键词检索方案选型（只读）")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    dataset = Path(args.dataset)
    if not dataset.is_file():
        print(f"dataset not found: {dataset}", file=sys.stderr)
        return 2
    return asyncio.run(run(dataset, Path(args.out), args.top_k, args.json))


if __name__ == "__main__":
    raise SystemExit(main())
