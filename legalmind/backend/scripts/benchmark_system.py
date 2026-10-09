"""系统瓶颈基线（设计 §14.4、§18；实施计划 P7「瓶颈评估与扩容」）。

第 18 节把每一项扩容都挂在「有测试或运行记录作为依据」上，并且**明说不许因为组件看起来专业
就提前引入**。本脚本负责产出那份依据——测什么、在哪测、拿什么数说话，都写进
``evaluations/reports/`` 的 JSON + Markdown。

四组测量，全是**本机实测**，不是估算：

1. **容量**（§14.4 的「先测量」清单前几项）：各表行数、库与索引大小、原件目录大小；
2. **检索延迟**：关键词通路与向量通路各跑 N 次，报 P50 / P95 / 最大值，并**记录实际走的通路**
   （`SearchResponse.path`）——级联是「命中为空才走向量」，不问清楚就会把关键词的耗时记成向量的；
3. **端到端问答分段**：借 `on_state` 回调把「检索 → 装配证据 → 生成 → 核验」四段分别计时，
   回答「瓶颈在检索还是在生成」——这决定了该优化哪一头；
4. **单进程并发**：同一事件循环里并发跑检索（1 / 2 / 4 / 8 档），看吞吐是否随并发上升。
   这是 §18「多进程 API：压测显示单进程成为瓶颈」的**第一手依据**，**不是** uvicorn 的
   HTTP 层压测（那需要凭据与常驻服务，见报告里的说明）。

跑法::

    set -a && . ./.env && set +a
    export DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:5432/${POSTGRES_DB}"
    .venv/Scripts/python.exe scripts/benchmark_system.py

可选参数：``--retrieval-n``、``--answering-n``、``--levels``、``--skip-answering``、``--out``。

⚠️ **本脚本不改任何数据**：问答走 `record=False`（不落 `answer_runs`、不写审计），检索是只读的。
评测脚本不该污染被评测的系统——否则「跑一次评测」本身就会把容量基线推高。
"""

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("APP_ENV", "development")

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text

from app.adapters import embedding
from app.core.config import get_settings
from app.core.database import SessionFactory
from app.core.security import Principal
from app.modules.answering.service import answer_question
from app.modules.parsing.memory import current_rss_bytes, peak_working_set_bytes
from app.modules.retrieval.schemas import SearchQuery
from app.modules.retrieval.service import search_provisions

REPO_ROOT = BACKEND_DIR.parent
DEFAULT_QUERIES = REPO_ROOT / "evaluations" / "datasets" / "retrieval_queries.json"
DEFAULT_QUESTIONS = REPO_ROOT / "evaluations" / "datasets" / "retrieval_questions.json"
DEFAULT_OUT = REPO_ROOT / "evaluations" / "reports"

#: 问答分段：`on_state` 报出来的状态名 → 它标记的时刻属于哪一段的**开始**
_STAGE_START = {
    "RETRIEVING": "retrieve",
    "ASSEMBLING_EVIDENCE": "assemble",
    "GENERATING": "generate",
    "VERIFYING": "verify",
}


def _pct(sorted_values: list[float], p: float) -> float:
    """最近秩（nearest-rank）分位数——样本少时比插值更诚实。"""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(p / 100 * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


@dataclass
class Timing:
    """一组耗时样本（秒）。"""

    label: str
    samples: list[float] = field(default_factory=list)

    def add(self, seconds: float) -> None:
        self.samples.append(seconds)

    def stats(self) -> dict:
        if not self.samples:
            return {"n": 0}
        ordered = sorted(self.samples)
        return {
            "n": len(ordered),
            "mean_ms": round(statistics.fmean(ordered) * 1000, 1),
            "p50_ms": round(_pct(ordered, 50) * 1000, 1),
            "p95_ms": round(_pct(ordered, 95) * 1000, 1),
            "max_ms": round(ordered[-1] * 1000, 1),
        }


def _system_memory() -> dict:
    """总内存与可用内存。Windows 走 `GlobalMemoryStatusEx`，Linux 读 `/proc/meminfo`。"""
    try:
        import ctypes

        class _StatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _StatusEx()
        status.dwLength = ctypes.sizeof(_StatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {}
        return {
            "total_gib": round(status.ullTotalPhys / 1024**3, 2),
            "available_gib": round(status.ullAvailPhys / 1024**3, 2),
            "load_percent": int(status.dwMemoryLoad),
        }
    except (AttributeError, OSError, ImportError):
        pass
    try:
        fields = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            fields[key] = int(rest.strip().split()[0]) * 1024
        return {
            "total_gib": round(fields["MemTotal"] / 1024**3, 2),
            "available_gib": round(fields["MemAvailable"] / 1024**3, 2),
        }
    except (KeyError, OSError, ValueError):
        return {}


async def measure_capacity() -> dict:
    """容量：各表行数与占用、库大小、原件目录大小（§14.4）。"""
    async with SessionFactory() as session:
        tables = (
            await session.execute(
                text(
                    "SELECT relname, pg_total_relation_size(relid) AS bytes "
                    "FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC"
                )
            )
        ).all()
        # ⚠️ **不要用 `pg_stat_user_tables.n_live_tup` 当行数**：它是统计信息估计值，未
        # `ANALYZE` 时读成 0——实测库里 2345 条条款版本被读成 0，直接让人误判「库里没数据」。
        # 容量基线要的是真数，所以逐表 `count(*)`；表都小（最大的几万行），代价可接受。
        rows = [
            (name, int(await session.scalar(text(f'SELECT count(*) FROM "{name}"'))), int(size))
            for name, size in tables
        ]
        # `pg_database_size` / `pg_indexes_size` 回来的是 Decimal，转成 int 再进 JSON
        db_bytes = int(
            await session.scalar(text("SELECT pg_database_size(current_database())")) or 0
        )
        index_bytes = int(
            await session.scalar(
                text("SELECT sum(pg_indexes_size(relid)) FROM pg_stat_user_tables")
            )
            or 0
        )

    settings = get_settings()
    root = Path(settings.storage_root)
    if not root.is_absolute():
        root = BACKEND_DIR / root
    files = 0
    stored_bytes = 0
    if root.exists():
        for path in root.rglob("*"):
            if path.is_file():
                files += 1
                stored_bytes += path.stat().st_size

    return {
        "tables": [
            {"name": name, "rows": int(live), "bytes": int(size)} for name, live, size in rows
        ],
        "database_mib": round((db_bytes or 0) / 1024**2, 2),
        "index_mib": round((index_bytes or 0) / 1024**2, 2),
        "storage_root": str(root),
        "storage_files": files,
        "storage_mib": round(stored_bytes / 1024**2, 2),
        "memory": {
            "system": _system_memory(),
            "process_rss_mib": round(current_rss_bytes() / 1024**2, 1),
            "process_peak_mib": round(peak_working_set_bytes() / 1024**2, 1),
        },
    }


def _load_queries(path: Path, limit: int | None) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    queries = [item["query"] for item in payload["queries"]]
    return queries[:limit] if limit else queries


async def measure_retrieval(principal: Principal, queries: list[str], repeat: int) -> dict:
    """检索延迟。**每条都记下实际走的通路**——级联会把关键词的耗时混进「向量」里。"""
    keyword = Timing("keyword")
    semantic = Timing("semantic(cascade)")
    paths: dict[str, int] = {}

    async def run(query: SearchQuery, timing: Timing) -> None:
        async with SessionFactory() as session:
            started = time.perf_counter()
            response = await search_provisions(session, principal, query)
            timing.add(time.perf_counter() - started)
        paths[response.path] = paths.get(response.path, 0) + 1

    # 预热：嵌入模型首次加载约 2 GB 常驻、耗时以十秒计。**必须显式预热编码器**——级联只在
    # 关键词命中为空时才走向量，拿一条「关键词能命中」的查询当预热等于没预热（实测踩到：
    # 首次向量调用把 31.6 s 的模型加载记进了 P95）。
    embedding.encode(get_settings().embedding_model, [queries[0]])
    async with SessionFactory() as session:
        await search_provisions(session, principal, SearchQuery(keyword=queries[0], limit=5))

    for _ in range(repeat):
        for query in queries:
            await run(SearchQuery(keyword=query, limit=5), keyword)
            await run(SearchQuery(semantic=query, limit=5), semantic)

    return {
        "keyword": keyword.stats(),
        "semantic": semantic.stats(),
        "paths": paths,
        "note": (
            "semantic 走级联：命中为空才向量。paths 是实际通路分布，"
            "向量占比高才说明这组数字反映向量延迟。"
        ),
    }


async def measure_answering(principal: Principal, questions: list[str]) -> dict:
    """端到端问答分段计时（§9.4）。`record=False`：不落库、不写审计。"""
    # ⚠️ `verify` 这一段**必须单列**：第二层语义核验（V1.48 起接入）是**每条主张一次模型调用**，
    # 挂在 `VERIFYING` 状态里。不单列的话它的耗时会全被算进「生成」——实测差点把这一层的代价看漏
    # （「生成」7.2 s → 34.5 s，其实是生成 + 判官混在一起）。
    stage_names = ("retrieve", "assemble", "generate", "verify")
    stages: dict[str, Timing] = {name: Timing(name) for name in stage_names}
    total = Timing("total")
    outcomes: list[dict] = []

    for question in questions:
        marks: dict[str, float] = {}
        started = time.perf_counter()

        async def on_state(state: str, _marks=marks) -> None:
            stage = _STAGE_START.get(state)
            if stage is not None and stage not in _marks:
                _marks[stage] = time.perf_counter()

        async with SessionFactory() as session:
            answer = await answer_question(
                session, principal, question, limit=5, record=False, on_state=on_state
            )
        total.add(time.perf_counter() - started)

        order = list(stage_names)
        for index, name in enumerate(order):
            begin = marks.get(name)
            if begin is None:
                continue
            # 下一段的开始就是本段的结束；没有下一段时用总耗时兜底
            following = next((marks[n] for n in order[index + 1 :] if n in marks), None)
            stages[name].add((following or (started + total.samples[-1])) - begin)

        outcomes.append(
            {
                "question": question,
                "path": answer.path,
                "blocked_by": answer.blocked_by,
                "citations": len(answer.citations),
                "seconds": round(answer.seconds, 2),
            }
        )

    return {
        "stages": {name: timing.stats() for name, timing in stages.items()},
        "total": total.stats(),
        "runs": outcomes,
        "note": (
            "stages 由 on_state 分段；生成段包含模型加载（若尚未常驻）。"
            "核验段 = 第一层确定性核验 + **第二层语义判官**（每条主张一次模型调用）。"
        ),
    }


async def measure_concurrency(
    principal: Principal, queries: list[str], levels: list[int], rounds: int = 3
) -> list[dict]:
    """单进程并发：同一事件循环里并发跑检索，看吞吐是否随并发上升（§18）。

    每一档跑 ``rounds`` 轮、每轮发 ``level`` 个并发请求，**把所有请求的延迟合起来**算分位、
    用总墙钟算吞吐。⚠️ 单轮样本太小：8 并发只有 8 个请求，实测同一台机器两次跑出来的曲线
    一个单调上升（49 → 81/s）、一个上下跳（45 → 14 → 60 → 67/s）——机器上还有别的负载，
    少样本会被它主导。所以**结论只看量级与趋势，不抠小数**。
    """

    async def one(query: str) -> float:
        async with SessionFactory() as session:
            started = time.perf_counter()
            await search_provisions(session, principal, SearchQuery(keyword=query, limit=5))
            return time.perf_counter() - started

    results: list[dict] = []
    for level in levels:
        latencies: list[float] = []
        wall_started = time.perf_counter()
        for round_index in range(rounds):
            batch = [
                queries[(round_index * level + offset) % len(queries)] for offset in range(level)
            ]
            latencies.extend(await asyncio.gather(*(one(query) for query in batch)))
        wall = time.perf_counter() - wall_started
        ordered = sorted(latencies)
        results.append(
            {
                "concurrency": level,
                "requests": len(latencies),
                "wall_ms": round(wall * 1000, 1),
                "throughput_per_s": round(len(latencies) / wall, 1) if wall else None,
                "latency_p50_ms": round(_pct(ordered, 50) * 1000, 1),
                "latency_p95_ms": round(_pct(ordered, 95) * 1000, 1),
            }
        )
    return results


def render_markdown(payload: dict) -> str:
    capacity = payload["capacity"]
    lines = [
        "# 系统瓶颈基线",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 语料：{payload['corpus']}",
        "",
        "## 一、容量（§14.4）",
        "",
        f"- 数据库 {capacity['database_mib']} MiB（其中索引 {capacity['index_mib']} MiB）",
        (
            f"- 原件目录 {capacity['storage_root']}：{capacity['storage_files']} 个文件 / "
            f"{capacity['storage_mib']} MiB"
        ),
        "",
        "| 表 | 行数 | 占用 (MiB) |",
        "| --- | ---: | ---: |",
    ]
    for table in capacity["tables"]:
        lines.append(f"| {table['name']} | {table['rows']} | {table['bytes'] / 1024**2:.2f} |")

    memory = capacity["memory"]
    system = memory.get("system") or {}
    lines += ["", "## 二、内存", ""]
    if system:
        lines.append(
            f"- 系统内存 {system.get('total_gib')} GiB，可用 {system.get('available_gib')} GiB"
            + (f"，负载 {system['load_percent']}%" if "load_percent" in system else "")
        )
    lines.append(
        f"- 本进程 RSS {memory['process_rss_mib']} MiB，峰值 {memory['process_peak_mib']} MiB"
    )
    lines.append("- ⚠️ 服务侧（PostgreSQL / Ollama）占用未在此采样，见报告说明。")

    retrieval = payload.get("retrieval")
    if retrieval:
        lines += [
            "",
            "## 三、检索延迟（§8.2）",
            "",
            "| 通路 | 次数 | P50 | P95 | 最大 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for key, label in (("keyword", "关键词"), ("semantic", "语义（级联）")):
            stats = retrieval[key]
            if not stats.get("n"):
                continue
            lines.append(
                f"| {label} | {stats['n']} | {stats['p50_ms']} ms | {stats['p95_ms']} ms | "
                f"{stats['max_ms']} ms |"
            )
        lines.append("")
        lines.append(f"实际通路分布：{retrieval['paths']}")

    answering = payload.get("answering")
    if answering:
        lines += [
            "",
            "## 四、端到端问答分段（§9.4）",
            "",
            "| 段 | 次数 | P50 | P95 |",
            "| --- | ---: | ---: | ---: |",
        ]
        for name, label in (
            ("retrieve", "检索"),
            ("assemble", "装配证据"),
            ("generate", "生成"),
            ("verify", "核验（含语义判官）"),
            ("total", "合计"),
        ):
            stats = answering["total"] if name == "total" else answering["stages"].get(name)
            if not stats or not stats.get("n"):
                continue
            lines.append(
                f"| {label} | {stats['n']} | {stats['p50_ms']} ms | {stats['p95_ms']} ms |"
            )
        blocked = [run for run in answering["runs"] if run["blocked_by"]]
        if blocked:
            lines.append("")
            lines.append(f"⚠️ 有 {len(blocked)} 次运行被门禁拦下：{blocked}")

    concurrency = payload.get("concurrency")
    if concurrency:
        lines += [
            "",
            "## 五、单进程并发（§18「多进程 API」）",
            "",
            "| 并发 | 请求数 | 墙钟 | 吞吐 /s | 延迟 P50 | 延迟 P95 |",
            "| ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for row in concurrency:
            lines.append(
                f"| {row['concurrency']} | {row['requests']} | {row['wall_ms']} ms | "
                f"{row['throughput_per_s']} | {row['latency_p50_ms']} ms | "
                f"{row['latency_p95_ms']} ms |"
            )

    lines += ["", "## 六、口径说明", "", payload["notes"], ""]
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> dict:
    queries = _load_queries(Path(args.queries), args.query_limit)
    questions = _load_queries(Path(args.questions), args.answering_n)
    principal = Principal(organization_id=uuid4(), user_id=uuid4(), roles=frozenset())

    capacity = await measure_capacity()
    retrieval = await measure_retrieval(principal, queries, args.retrieval_n)
    concurrency = await measure_concurrency(
        principal, queries, args.levels, rounds=args.concurrency_rounds
    )
    answering = None
    if not args.skip_answering:
        answering = await measure_answering(principal, questions)

    settings = get_settings()
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "corpus": f"{len(queries)} 条词面查询 / {len(questions)} 条问句",
        "models": {
            "embedding": settings.embedding_model,
            "generation": settings.generation_model,
        },
        "capacity": capacity,
        "retrieval": retrieval,
        "concurrency": concurrency,
        "answering": answering,
        "notes": (
            "① 问答走 `record=False`，不落 `answer_runs`、不写审计，检索只读——跑评测不改数据。"
            "② 检索与并发都做了预热，嵌入模型的首次加载不计入延迟。"
            "③ 并发测的是**同一事件循环内的服务层**，不是 uvicorn HTTP 层压测；"
            "HTTP 层需要凭据与常驻服务，若要做另开一轮（§18 的触发条件要求的是可复现的压测记录）。"
            "④ 容量里的行数是**逐表 `count(*)` 的精确值**——`pg_stat_user_tables.n_live_tup` 是"
            "统计信息估计值，未 `ANALYZE` 时会读成 0（实测把 2345 条条款版本读成 0），"
            "拿它当容量基线会直接得出「库里没数据」的错误结论。"
            "⑤ 内存只采了本进程与整机，**服务侧（PostgreSQL / Ollama）未单独采样**；"
            "整机负载包含其它应用，不能直接当成「本服务占用」。"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="系统瓶颈基线（设计 §14.4、§18）")
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES))
    parser.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    parser.add_argument("--query-limit", type=int, default=10, help="词面查询取样条数")
    parser.add_argument("--retrieval-n", type=int, default=5, help="每条查询重复轮数")
    parser.add_argument("--answering-n", type=int, default=3, help="问答取样条数")
    parser.add_argument("--levels", default="1,2,4,8", help="并发档位")
    parser.add_argument(
        "--concurrency-rounds", type=int, default=3, help="每档并发跑几轮（多轮聚合降噪）"
    )
    parser.add_argument("--skip-answering", action="store_true", help="跳过问答（无需本地模型）")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--print", action="store_true", help="把 JSON 打到 stdout")
    args = parser.parse_args()
    args.levels = [int(item) for item in args.levels.split(",") if item.strip()]

    payload = asyncio.run(_run(args))

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"system_benchmark_{stamp}.json"
    md_path = out_dir / f"system_benchmark_{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")

    summary = {
        "json": str(json_path),
        "markdown": str(md_path),
        "retrieval": payload["retrieval"]["keyword"] | {"path": "keyword"},
        "paths": payload["retrieval"]["paths"],
        "semantic": payload["retrieval"]["semantic"],
        "concurrency": payload["concurrency"],
    }
    if payload.get("answering"):
        summary["answering_total"] = payload["answering"]["total"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.print:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
