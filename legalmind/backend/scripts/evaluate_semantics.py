"""§9.3 第二层语义校验的度量：本地模型当判官，靠不靠得住？

金标准是 `evaluations/datasets/claim_verification.json`——一批**已知好坏**的断言：好断言是条文
原话（或同义改写），坏断言各有明确缺陷（过度概括 / 漏条件 / 漏例外 / 与证据相反 / 引错条款 / 证据不支持）。

**为什么不直接接进问答就算完**：第二层是语义判断，不能像第一层那样靠集合运算做。「让模型去判模型」
如果没有尺子，只是把信任从一处搬到另一处。**先量清楚它抓得住什么、漏掉什么，再决定让它拦结论。**

跑法::

    set -a && . ./.env && set +a
    export DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:5432/${POSTGRES_DB}"
    .venv/Scripts/python.exe scripts/evaluate_semantics.py [--gate] [--ids b01,g01]

指标只有两个，且**刻意不对称**：

- **缺陷检出率**（坏断言被拦下）：放行坏断言是这一层存在的理由，抓不住就没意义 → 阈值 0.9
- **好断言接受率**：判官过于谨慎只是多转一次人工，比漏判安全 → 阈值 0.8
"""

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("APP_ENV", "development")

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

DEFAULT_DATASET = BACKEND_DIR.parent / "evaluations" / "datasets" / "claim_verification.json"
DEFAULT_OUT = BACKEND_DIR.parent / "evaluations" / "reports"

if not os.environ.get("DATABASE_URL"):
    print(
        "DATABASE_URL 未设置。先在 legalmind/ 下加载环境再运行（见脚本 docstring）。",
        file=sys.stderr,
    )
    raise SystemExit(2)

from sqlalchemy import select

from app.adapters import generation
from app.core.database import SessionFactory, engine
from app.models import LegalInstrument, LegalVersion, ProvisionIdentity, ProvisionVersion
from app.modules.answering import semantics
from app.modules.answering.claims import Claim
from app.modules.evaluation import scoring


@dataclass(frozen=True)
class PinnedProvision:
    """与 `retrieval.schemas.ProvisionHit` 字段对齐，好直接喂给 `semantics.build_review_messages`。"""

    instrument_title: str
    provision_number: str
    provision_display: str
    legal_status: str
    text: str


async def load_corpus(pairs: set[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    corpus: dict[tuple[str, str], dict] = {}
    async with SessionFactory() as session:
        rows = await session.execute(
            select(
                LegalInstrument.title,
                ProvisionIdentity.provision_number,
                LegalVersion.legal_status,
                ProvisionVersion.structure_path,
                ProvisionVersion.text,
            )
            .join(ProvisionIdentity, ProvisionVersion.provision_identity_id == ProvisionIdentity.id)
            .join(LegalVersion, ProvisionVersion.legal_version_id == LegalVersion.id)
            .join(LegalInstrument, LegalVersion.instrument_id == LegalInstrument.id)
        )
        for title, number, status, structure_path, text in rows:
            if (title, number) not in pairs:
                continue
            display = number
            if isinstance(structure_path, dict):
                display = structure_path.get("article") or display
            corpus[(title, number)] = {"text": text, "legal_status": status, "display": display}
    return corpus


def _pin(case: dict, corpus: dict) -> list[PinnedProvision]:
    return [
        PinnedProvision(
            instrument_title=law,
            provision_number=article,
            provision_display=corpus[(law, article)]["display"],
            legal_status=corpus[(law, article)]["legal_status"],
            text=corpus[(law, article)]["text"],
        )
        for law, article in case["evidence"]
    ]


def _markdown(payload: dict) -> str:
    summary = payload["summary"]
    lines = [
        "# 第二层语义校验评测报告（判官 = 本地模型）",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 金标准：`{payload['dataset']}`（{summary['cases']} 条断言）",
        f"- 判官模型：`{payload['model']}`（温度 0）",
        f"- 结论：**{'达标' if summary['passed'] else '未达标'}**",
        "",
        "## 指标",
        "",
        "| 指标 | 值 | 阈值 | 方向 | 达标 |",
        "| --- | --- | --- | --- | --- |",
    ]
    labels = {"defect_detection_rate": "缺陷检出率", "supported_acceptance_rate": "好断言接受率"}
    for key, check in summary["checks"].items():
        value = "—" if check["value"] is None else f"{check['value']:.3f}"
        ok = "—" if check["ok"] is None else ("是" if check["ok"] else "**否**")
        lines.append(
            f"| {labels.get(key, key)} | {value} | {check['threshold']} | 越高越好 | {ok} |"
        )

    lines += [
        "",
        "## 逐条",
        "",
        "| 断言 | 期望 | 判官 | 缺陷类型 | 判官理由 | 耗时 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for record in payload["records"]:
        want = "支持" if record["expect_supported"] else "**不支持**"
        got = "支持" if record["supported"] else "不支持"
        mark = got if record["correct"] else f"**{got}**"
        lines.append(
            f"| {record['id']} | {want} | {mark} | {record['defect'] or '—'} | "
            f"{record['reason'][:60] or '—'} | {record.get('quote', '')[:30] or '—'} | "
            f"{record['seconds']:.1f}s |"
        )

    lines += ["", "## 判错的那些", ""]
    wrong = [r for r in payload["records"] if not r["correct"]]
    if not wrong:
        lines.append("无。")
    for record in wrong:
        lines.append(
            f"### {record['id']}（期望{'支持' if record['expect_supported'] else '不支持'}）"
        )
        lines.append("")
        lines.append(f"- 主张：{record['claim']}")
        lines.append(f"- 判官：{'支持' if record['supported'] else '不支持'}——{record['reason']}")
        lines.append(f"- 说明：{record['note']}")
        lines.append("")
    return "\n".join(lines) + "\n"


async def run(args) -> int:
    dataset = scoring.load_dataset(args.dataset)
    cases = dataset["cases"]
    if args.ids:
        wanted = {item.strip() for item in args.ids.split(",") if item.strip()}
        cases = [case for case in cases if case["id"] in wanted]
        if not cases:
            print(f"--ids 没有匹配到断言：{args.ids}", file=sys.stderr)
            return 2

    pairs = {(law, article) for case in dataset["cases"] for law, article in case["evidence"]}
    corpus = await load_corpus(pairs)
    missing = [pair for pair in pairs if pair not in corpus]
    if missing:
        print(f"金标准里的条款不在语料里：{missing}", file=sys.stderr)
        return 2

    model_name = args.model or generation.DEFAULT_MODEL
    if not generation.available(model_name):
        print(
            f"本地生成模型不可用：{model_name}（§9.5 锁定本地模型，不回落外部服务）",
            file=sys.stderr,
        )
        return 3

    def generate(messages, schema):
        return generation.generate(
            model_name, messages, schema=schema, max_new_tokens=args.max_new_tokens
        )

    records = []
    for case in cases:
        evidence = _pin(case, corpus)
        # 断言本身也走 §9.2 的结构（`claims.Claim`），这样判官看到的输入与线上完全一致
        target = Claim(text=case["claim"], evidence_ids=case["evidence_ids"])
        started = time.perf_counter()
        verdict = semantics.review_claim(target, case["question"], evidence, generate)
        seconds = time.perf_counter() - started
        supported = bool(verdict.supported) if verdict is not None else False
        record = {
            "id": case["id"],
            "expect_supported": case["expect_supported"],
            "defect": case.get("defect"),
            "held_out": bool(case.get("held_out")),
            "supported": supported,
            "correct": verdict is not None and supported == case["expect_supported"],
            "reason": verdict.summary() if verdict is not None else "判官没有给出可解析的结论",
            "quote": (verdict.evidence_quote if verdict is not None else ""),
            "claim": case["claim"],
            "note": case.get("note"),
            "seconds": seconds,
        }
        records.append(record)
        print(
            f"  [{'OK' if record['correct'] else '!!'}] {record['id']} {seconds:5.1f}s  {case['claim'][:40]}"
        )

    good = [r for r in records if r["expect_supported"]]
    bad = [r for r in records if not r["expect_supported"]]
    metrics = {
        "defect_detection_rate": (
            sum(1 for r in bad if not r["supported"]) / len(bad) if bad else None
        ),
        "supported_acceptance_rate": (
            sum(1 for r in good if r["supported"]) / len(good) if good else None
        ),
    }
    thresholds = dataset.get("thresholds", {})
    checks = {
        key: {
            "value": metrics[key],
            "threshold": thresholds.get(key),
            "ok": (
                None
                if metrics[key] is None or thresholds.get(key) is None
                else metrics[key] >= thresholds[key]
            ),
        }
        for key in ("defect_detection_rate", "supported_acceptance_rate")
    }
    held = [r for r in records if r["held_out"]]
    summary = {
        "cases": len(records),
        "counts": {"good": len(good), "bad": len(bad), "held_out": len(held)},
        "metrics": metrics,
        "checks": checks,
        "held_out_accuracy": (sum(1 for r in held if r["correct"]) / len(held) if held else None),
        "passed": all(check["ok"] for check in checks.values() if check["ok"] is not None),
    }
    payload = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dataset": Path(args.dataset).name,
        "model": model_name,
        "summary": summary,
        "records": records,
    }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"semantics_eval_{stamp}.json"
    md_path = out_dir / f"semantics_eval_{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_markdown(payload), encoding="utf-8")

    print()
    print(f"结论：{'达标' if summary['passed'] else '未达标'}")
    for key, check in checks.items():
        value = "—" if check["value"] is None else f"{check['value']:.3f}"
        mark = "—" if check["ok"] is None else ("✓" if check["ok"] else "✗")
        print(f"  {mark} {key}: {value}（阈值 {check['threshold']}）")
    held_value = summary["held_out_accuracy"]
    if held_value is not None:
        # **只看全量等于自证**：留出用例是调过提示词之后才加的，单独报出来
        print(
            f"  · 留出用例准确率: {held_value:.3f}（{summary['counts']['held_out']} 条，调提示词之后才加的）"
        )
    print(f"报告：{json_path}")
    print(f"      {md_path}")
    if args.gate and not summary["passed"]:
        return 1
    return 0


async def _main(args) -> int:
    try:
        return await run(args)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="第二层语义校验的判官评测（需要本地模型；只读数据库）"
    )
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--ids", default="", help="逗号分隔的断言 id，只跑这些")
    parser.add_argument("--gate", action="store_true", help="未达标时退出码 1")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
