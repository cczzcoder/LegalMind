"""入库质量门禁校验（设计 §7、§8.3、§17.1、§20.3）。

只读扫描库里的原件，逐个校验来源登记、元数据完整性与解析/条款质量，
按 ``app.modules.legal_corpus.quality`` 的三态给出结论：

- **failed**：硬要求不过（来源未登记 / 授权说明为空 / 无解析产物 / 未挂法律版本）。
- **degraded**：已落库但降级待审（``review_status='pending'``，或该版本没有条款身份）。
- **passed**：高置信度自动落库（``review_status='approved'``）且条款齐全。

不改动任何数据；退出码 1 表示门禁不通过（有 failed，或 ``--fail-on-degraded`` 时的 degraded）。

用法::

    # 先在 legalmind/ 下加载环境并导出 DATABASE_URL
    set -a && . ./.env && set +a
    export DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:5432/${POSTGRES_DB}"

    .venv/Scripts/python.exe scripts/quality_gate.py [--source 名称] [--json] [--out 目录]

报告写成 JSON + Markdown，默认落到 ``evaluations/reports/``（该目录已 gitignore）。
"""

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("APP_ENV", "development")

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

DEFAULT_OUT = BACKEND_DIR.parent / "evaluations" / "reports"

if not os.environ.get("DATABASE_URL"):
    print(
        "DATABASE_URL 未设置。先在 legalmind/ 下加载环境再运行：\n"
        "  set -a && . ./.env && set +a\n"
        '  export DATABASE_URL="postgresql+asyncpg://$POSTGRES_USER:$POSTGRES_PASSWORD'
        '@127.0.0.1:5432/$POSTGRES_DB"\n'
        "  .venv/Scripts/python.exe scripts/quality_gate.py",
        file=sys.stderr,
    )
    raise SystemExit(2)

from app.core.database import SessionFactory
from app.modules.legal_corpus import quality

_MEDIA_LABELS = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}


def _media_label(media_type: str) -> str:
    return _MEDIA_LABELS.get(media_type, media_type)


def _render(rows: list[dict]) -> str:
    lines = ["| 结论 | 原件 | 来源 | 格式 | 分块 | 版本 | 审核 | 条款 | 原因 |"]
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in rows:
        lines.append(
            f"| {row['status']} | {row['filename']} | {row['source_name'] or '—'} | "
            f"{_media_label(row['media_type'])} | {_empty(row['chunk_count'])} | "
            f"{row['version_label'] or '—'} | {row['review_status'] or '—'} | "
            f"{row['provision_count']} | {', '.join(row['reasons']) or '—'} |"
        )
    return "\n".join(lines)


def _empty(value: object) -> str:
    return "—" if value is None else str(value)


def _markdown(rows: list[dict], summary: dict) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    out = [
        "# 入库质量门禁报告",
        "",
        f"生成时间：{now}",
        "",
        f"- 原件总数：{summary['total']}",
        f"- passed：{summary['passed']}",
        f"- degraded：{summary['degraded']}",
        f"- failed：{summary['failed']}",
        "",
        "## 逐原件",
        "",
        _render(rows),
        "",
        "## 说明",
        "",
        "- **failed**：来源未登记 / 授权说明为空（§20.3）、无解析产物、未挂法律版本——不得进入正式证据范围。",
        "- **degraded**：已落库但 `review_status='pending'`（版本标识回退、标题退化、多原件冲突），",
        "  或该版本没有条款身份——需人工确认，第 7 节的「版本关联确认」仍是人工环节。",
        "- **passed**：高置信度自动落库且条款齐全。**自动确认不等于人工复核**。",
        "",
    ]
    return "\n".join(out)


async def run(source_name: str | None) -> list[dict]:
    async with SessionFactory() as session:
        facts = await quality.collect_facts(session, source_name=source_name)
    rows: list[dict] = []
    for fact in facts:
        verdict = quality.assess(fact)
        rows.append({**asdict(fact), "status": verdict.status, "reasons": list(verdict.reasons)})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="入库质量门禁校验（只读）")
    parser.add_argument("--source", default=None, help="只校验指定名称的来源；缺省为全库")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="报告输出目录")
    parser.add_argument("--json", action="store_true", help="把报告 JSON 打到标准输出")
    parser.add_argument(
        "--fail-on-degraded",
        action="store_true",
        help="把 degraded 也算门禁不通过（只放行高置信度）",
    )
    args = parser.parse_args()

    rows = asyncio.run(run(args.source))
    counts = Counter(row["status"] for row in rows)
    summary = {
        "total": len(rows),
        "passed": counts.get(quality.PASSED, 0),
        "degraded": counts.get(quality.DEGRADED, 0),
        "failed": counts.get(quality.FAILED, 0),
    }

    print(_render(rows))
    print(
        f"\n合计 {summary['total']}：passed {summary['passed']}、"
        f"degraded {summary['degraded']}、failed {summary['failed']}"
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    payload = {"generated_at": datetime.now(UTC).isoformat(), "summary": summary, "artifacts": rows}
    json_path = out_dir / f"quality_gate_{stamp}.json"
    md_path = out_dir / f"quality_gate_{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_markdown(rows, summary), encoding="utf-8")
    print(f"\nJSON: {json_path}\nMD:   {md_path}")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    failed = summary["failed"] > 0
    degraded = summary["degraded"] > 0
    return 1 if failed or (args.fail_on_degraded and degraded) else 0


if __name__ == "__main__":
    raise SystemExit(main())
