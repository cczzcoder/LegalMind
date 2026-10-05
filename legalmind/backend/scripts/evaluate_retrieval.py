"""检索通路选型：关键词 / 向量 / 混合（设计 §8.2、§8.3、§17.1）。

§8.3 要求中文关键词方案「用测试集选型」；§8.2 的多路检索里还有向量通路。本脚本把候选实现跑在
**同一份金标准**上，比 Recall@k / 准确率 / MRR / 误报 / 耗时，报告写到 ``evaluations/reports/``。

**金标准**（``evaluations/datasets/retrieval_queries.json``）：期望集 = 全库中「去空白文本」包含
全部 terms 的（法, 条）。去空白是为了不被 PDF 折行空格影响（实测「民用航 空器」）。它衡量的是
**词面命中**，不是语义召回——所以向量通路在这份金标准上只反映「它能不能找到同一个条款」，
**不能据此判断它处理同义改写的本事**（那需要另一份问句式金标准，尚未建立）。

跑法::

    set -a && . ./.env && set +a
    export DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:5432/${POSTGRES_DB}"

    # 只比关键词（不需要嵌入依赖）
    .venv/Scripts/python.exe scripts/evaluate_retrieval.py

    # 加上向量与混合（会加载本地嵌入模型，首次要下载权重）
    .venv/Scripts/python.exe scripts/evaluate_retrieval.py \
        --paths keyword,vector,hybrid \
        --models BAAI/bge-m3,shibing624/text2vec-base-chinese
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
from app.modules.retrieval import graph, keyword, rewrite, vector
from app.modules.retrieval.keyword import STRATEGIES, Strategy

_ARTICLE_SORT = func.regexp_replace(ProvisionIdentity.provision_number, "[^0-9].*$", "", "g").cast(
    Integer
)

Pair = tuple[str, str]
Search = Callable[[object, Principal, str, int], Awaitable[list[Pair]]]


@dataclass(frozen=True)
class Retriever:
    name: str
    summary: str
    search: Search


def _load_queries(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["queries"]


async def _prepare(session) -> None:
    """``trigram`` / ``word_trigram`` 两个候选需要 pg_trgm（``vector`` 由迁移 0015 启用）。

    pg_trgm 随 PostgreSQL 提供，不是新增常驻组件（设计 §18）。
    """
    await session.execute(sql_text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    await session.commit()


def _dedupe(pairs: list[Pair], top_k: int) -> list[Pair]:
    seen: list[Pair] = []
    for pair in pairs:
        if pair not in seen:
            seen.append(pair)
        if len(seen) == top_k:
            break
    return seen


def _keyword_retriever(strategy: Strategy) -> Retriever:
    async def search(session, principal, query: str, top_k: int) -> list[Pair]:
        rows = await _keyword_rows(session, principal, strategy, query, top_k * 3)
        return _pairs(_unique(rows, top_k))

    return Retriever(f"keyword:{strategy.name}", strategy.summary, search)


def _pairs(rows) -> list[Pair]:
    """实体行 → (法, 条)。行是 (ProvisionVersion, ProvisionIdentity, LegalVersion, LegalInstrument, …)。"""
    return [(row[3].title, row[1].provision_number) for row in rows]


def _unique(rows, top_k: int) -> list:
    """按 (法, 条) 去重——同一（法, 条）可能因多版本重复。"""
    seen: list = []
    keys: set = set()
    for row in rows:
        key = (row[3].title, row[1].provision_number)
        if key in keys:
            continue
        keys.add(key)
        seen.append(row)
        if len(seen) == top_k:
            break
    return seen


async def _keyword_rows(session, principal: Principal, strategy: Strategy, query: str, limit: int):
    statement = (
        select(ProvisionVersion, ProvisionIdentity, LegalVersion, LegalInstrument, SourceArtifact)
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
    return list((await session.execute(statement)).all())


async def _keyword_hits(
    session, principal: Principal, strategy: Strategy, query: str, top_k: int
) -> list[Pair]:
    return _pairs(
        _unique(await _keyword_rows(session, principal, strategy, query, top_k * 3), top_k)
    )


def _rewritten_retriever(strategy: Strategy, layers: str) -> Retriever:
    """先改写查询再交给关键词通路，各变体结果**取并集**——并集只可能提高召回，不会降低。"""

    async def search(session, principal, query: str, top_k: int) -> list[Pair]:
        result = rewrite.rewrite(query)
        if layers == "norm":
            variants = (result.original, result.normalized)
        elif layers == "strip":
            variants = (result.original, result.normalized, result.stripped)
        else:
            variants = result.variants()
        merged: list[Pair] = []
        for variant in dict.fromkeys(item for item in variants if item):
            merged.extend(await _keyword_hits(session, principal, strategy, variant, top_k))
        return _dedupe(merged, top_k)

    return Retriever(
        f"keyword:{strategy.name}+{layers}",
        f"查询改写（{layers}）+ {strategy.summary}",
        search,
    )


def _vector_retriever(model_name: str, query_vectors: dict[str, list[float]], max_distance: float):
    async def search(session, principal, query: str, top_k: int) -> list[Pair]:
        query_vector = query_vectors.get(query)
        if query_vector is None:
            return []
        statement = vector.statement(
            model=model_name,
            query_vector=query_vector,
            principal=principal,
            limit=top_k * 3,
            max_distance=max_distance,
        )
        rows = (await session.execute(statement)).all()
        # 语句返回 (ProvisionVersion, ProvisionIdentity, LegalVersion, LegalInstrument, SourceArtifact)
        return _pairs(_unique(list(rows), top_k))

    return Retriever(
        f"vector:{model_name}",
        f"向量通路（{model_name}），余弦距离 ≤ {max_distance}，按距离升序",
        search,
    )


def _graph_retriever(
    strategy: Strategy, model_name: str, query_vectors: dict[str, list[float]], max_distance: float
) -> Retriever:
    """级联拿到种子，再把种子**引用的条款**补在后面（设计 §8.4 的一跳出边扩展）。

    用来回答 §8.4 要求的「有 / 无图扩展对比」——种子部分与 `cascade` 同一口径，
    差别只在末尾多了引用邻居。
    """

    async def search(session, principal, query: str, top_k: int) -> list[Pair]:
        seeds: list = []
        for variant in rewrite.rewrite(query).variants():
            rows = await _keyword_rows(session, principal, strategy, variant, top_k * 3)
            seeds = _unique([*seeds, *rows], top_k)
            if len(seeds) == top_k:
                break
        if not seeds:
            value = query_vectors.get(query)
            if value is None:
                return []
            rows = (
                await session.execute(
                    vector.statement(
                        model=model_name,
                        query_vector=value,
                        principal=principal,
                        limit=top_k * 3,
                        max_distance=max_distance,
                    )
                )
            ).all()
            seeds = _unique(list(rows), top_k)
        if not seeds:
            return []
        related = (
            await session.execute(
                graph.statement(
                    seed_version_ids=[row[0].id for row in seeds],
                    principal=principal,
                    limit=top_k * 3,
                )
            )
        ).all()
        return _pairs(_unique([*seeds, *related], top_k))

    return Retriever(
        f"graph:{strategy.name}+{model_name}",
        f"级联 + 一跳引用扩展（{strategy.name} 取种子，命中为空用 {model_name} 兜底）",
        search,
    )


def _hybrid_retriever(parts: list[Retriever], *, depth: int, rrf_k: int) -> Retriever:
    """RRF 融合（设计 §8.2 第 6 步）：score = Σ 1/(k + 名次)，与各路的分值尺度无关。"""

    async def search(session, principal, query: str, top_k: int) -> list[Pair]:
        scores: dict[Pair, float] = {}
        for part in parts:
            hits = await part.search(session, principal, query, depth)
            for rank, pair in enumerate(hits, start=1):
                scores[pair] = scores.get(pair, 0.0) + 1.0 / (rrf_k + rank)
        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return [pair for pair, _score in ordered][:top_k]

    return Retriever(
        "hybrid:" + "+".join(part.name.split(":", 1)[1] for part in parts),
        "RRF 融合（k=" + str(rrf_k) + "）：" + " ＋ ".join(part.name for part in parts),
        search,
    )


def _cascade_retriever(primary: Retriever, fallback: Retriever) -> Retriever:
    """关键词（含改写）优先，**命中为空才走向量**。

    依据是实测：关键词命中时准确率 1.000，而向量通路永远返回 top-k、必然夹带无关条文
    （词面集上准确率 0.074）。盲目 RRF 融合等于把这份噪声加到关键词上（准确率 1.000 → 0.076），
    所以这里改成「先精确、空了再兜底语义」——两边各自的强项都不受损。
    """

    async def search(session, principal, query: str, top_k: int) -> list[Pair]:
        hits = await primary.search(session, principal, query, top_k)
        if hits:
            return hits
        return await fallback.search(session, principal, query, top_k)

    return Retriever(
        "cascade:" + primary.name.split(":", 1)[1] + ">" + fallback.name.split(":", 1)[1],
        f"级联：先 {primary.name}，命中为空再 {fallback.name}",
        search,
    )


def _build_retrievers(
    paths: list[str],
    models: list[str],
    max_distance: float,
    rrf_k: int,
    depth: int,
    rewrite_layers: list[str],
) -> tuple[list[Retriever], dict[str, dict[str, list[float]]]]:
    """按 ``paths`` 组装要跑的检索器；向量查询向量按模型预计算一次，避免每条查询重复编码。"""
    retrievers: list[Retriever] = []
    query_vectors: dict[str, dict[str, list[float]]] = {}
    if "keyword" in paths:
        retrievers += [_keyword_retriever(strategy) for strategy in STRATEGIES]
        # 改写消融：逐层加码，看每一层各自贡献多少
        for layers in rewrite_layers:
            retrievers.append(_rewritten_retriever(keyword.selected(), layers))
    # cascade / graph 也要向量兜底，所以只要用到向量就先按模型建好查询向量缓存
    if any(item in paths for item in ("vector", "hybrid", "cascade", "graph")):
        for model_name in models:
            query_vectors[model_name] = {}
    if "vector" in paths:
        for model_name in models:
            retrievers.append(
                _vector_retriever(model_name, query_vectors[model_name], max_distance)
            )
    if "hybrid" in paths:
        for model_name in models:
            retrievers.append(
                _hybrid_retriever(
                    [
                        _keyword_retriever(keyword.selected()),
                        _vector_retriever(model_name, query_vectors[model_name], max_distance),
                    ],
                    depth=depth,
                    rrf_k=rrf_k,
                )
            )
    if "cascade" in paths:
        for model_name in models:
            retrievers.append(
                _cascade_retriever(
                    _rewritten_retriever(keyword.selected(), "expand"),
                    _vector_retriever(model_name, query_vectors[model_name], max_distance),
                )
            )
    if "graph" in paths:
        for model_name in models:
            retrievers.append(
                _graph_retriever(
                    keyword.selected(), model_name, query_vectors[model_name], max_distance
                )
            )
    return retrievers, query_vectors


def _score(hits: list[Pair], expect: set[Pair], top_k: int) -> dict:
    limited = hits[:top_k]
    found = [hit for hit in limited if hit in expect]
    rank = next((index + 1 for index, hit in enumerate(limited) if hit in expect), None)
    return {
        "found": len(found),
        "rank": rank,
        "recall": (len(found) / len(expect)) if expect else None,
        "reciprocal_rank": (1.0 / rank) if rank else 0.0,
    }


def _summarise(retriever: Retriever, per_query: list[dict], top_k: int) -> dict:
    scored = [row for row in per_query if row["recall"] is not None]
    total_expect = sum(len(row["expect"]) for row in scored)
    total_found = sum(row["found"] for row in scored)
    total_hits = sum(len(row["hits"]) for row in scored)
    return {
        "retriever": retriever.name,
        "summary": retriever.summary,
        "top_k": top_k,
        "queries": len(per_query),
        "recall": round(total_found / total_expect, 4) if total_expect else 0.0,
        "precision": round(total_found / total_hits, 4) if total_hits else None,
        "recall_at_1": round(
            sum(row["recall"] for row in scored if row["rank"] == 1) / len(scored)
            if scored
            else 0.0,
            4,
        ),
        "mrr": round(
            sum(row["reciprocal_rank"] for row in scored) / len(scored) if scored else 0.0, 4
        ),
        "no_hit_false_positives": sum(
            1 for row in per_query if row["type"] == "no_hit" and row["hits"]
        ),
        "errors": sum(1 for row in per_query if row["error"]),
        "avg_ms": round(sum(row["elapsed_ms"] for row in per_query) / len(per_query), 2),
    }


async def evaluate(
    dataset: Path, top_k: int, retrievers: list[Retriever]
) -> tuple[list[dict], dict[str, list[dict]], list[dict]]:
    queries = _load_queries(dataset)
    principal = Principal(organization_id=uuid4(), user_id=uuid4(), roles=frozenset())
    summaries: list[dict] = []
    details: dict[str, list[dict]] = {}

    async with SessionFactory() as session:
        await _prepare(session)
        for retriever in retrievers:
            per_query: list[dict] = []
            for item in queries:
                expect = {(title, number) for title, number in item["expect"]}
                try:
                    started = time.perf_counter()
                    hits = await retriever.search(session, principal, item["query"], top_k)
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    score = _score(hits, expect, top_k)
                    error = None
                except Exception as failure:  # noqa: BLE001 - 候选失败要如实记下，不能让整轮中断
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
            summaries.append(_summarise(retriever, per_query, top_k))
            details[retriever.name] = per_query
    return summaries, details, queries


def _markdown(summaries: list[dict], details: dict[str, list[dict]], top_k: int) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    out = [
        "# 检索通路选型（关键词 / 向量 / 混合）",
        "",
        f"生成时间：{now}　top-k：{top_k}",
        "",
        "期望集 = 全库中「去空白文本」包含全部 terms 的（法, 条）；衡量**词面命中**，不是语义召回。",
        "",
        "## 汇总",
        "",
        "| 通路 | Recall | 准确率 | Rank@1 | MRR | 无命中查询误报 | 错误 | 平均耗时 ms |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in summaries:
        out.append(
            f"| `{row['retriever']}` | {row['recall']} | {row['precision']} | {row['recall_at_1']} | "
            f"{row['mrr']} | {row['no_hit_false_positives']} | {row['errors']} | {row['avg_ms']} |"
        )
    out += ["", "## 通路说明", ""]
    for row in summaries:
        out.append(f"- `{row['retriever']}`：{row['summary']}")
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


async def run(args) -> int:
    paths = [item.strip() for item in args.paths.split(",") if item.strip()]
    models = [item.strip() for item in args.models.split(",") if item.strip()]
    rewrite_layers = [item.strip() for item in args.rewrite.split(",") if item.strip()]
    retrievers, query_vectors = _build_retrievers(
        paths, models, args.max_distance, args.rrf_k, args.depth, rewrite_layers
    )
    queries = _load_queries(Path(args.dataset))
    if query_vectors:
        from app.adapters import embedding

        texts = [item["query"] for item in queries]
        for model_name, cache in query_vectors.items():
            print(f"编码 {len(texts)} 条查询向量：{model_name} …")
            vectors = embedding.encode(model_name, texts, batch_size=args.batch_size)
            cache.update(zip(texts, vectors, strict=True))

    summaries, details, _ = await evaluate(Path(args.dataset), args.top_k, retrievers)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": args.dataset,
        "top_k": args.top_k,
        "queries": len(queries),
        "paths": paths,
        "models": models,
        "rewrite": rewrite_layers,
        "summary": summaries,
        "details": details,
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"retrieval_eval_{stamp}.json"
    md_path = out_dir / f"retrieval_eval_{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_markdown(summaries, details, args.top_k), encoding="utf-8")

    header = f"{'通路':<44}{'Recall':>8}{'准确率':>8}{'Rank@1':>9}{'MRR':>8}{'误报':>6}{'ms':>8}"
    print(header)
    print("-" * len(header))
    for row in summaries:
        print(
            f"{row['retriever']:<44}{row['recall']:>8}{row['precision']!s:>8}"
            f"{row['recall_at_1']:>9}{row['mrr']:>8}"
            f"{row['no_hit_false_positives']:>6}{row['avg_ms']:>8}"
        )
    print(f"\nJSON: {json_path}\nMD:   {md_path}")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="检索通路选型（只读）")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--paths", default="keyword", help="逗号分隔：keyword,vector,hybrid")
    parser.add_argument("--models", default="", help="逗号分隔的嵌入模型；vector/hybrid 需要")
    parser.add_argument("--max-distance", type=float, default=vector.DEFAULT_MAX_DISTANCE)
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument("--depth", type=int, default=50, help="融合时每路先取多少条")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument(
        "--rewrite",
        default="",
        help="查询改写消融，逗号分隔：norm（仅规范化）,strip（+剥离疑问）,expand（+同义扩展）",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not Path(args.dataset).is_file():
        print(f"dataset not found: {args.dataset}", file=sys.stderr)
        return 2
    if ("vector" in args.paths or "hybrid" in args.paths) and not args.models:
        print("--paths 含 vector/hybrid 时必须给 --models", file=sys.stderr)
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
