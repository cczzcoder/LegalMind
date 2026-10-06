"""生成质量评测：本地生成模型单独评测（设计 §9.5「达标后再接入正式问答」）。

**评测的是「这条提示词 + 这个本地模型」**，不是检索——每条用例显式给出要喂给模型的条款
（`evaluations/datasets/generation_quality.json` 的 `evidence`），检索通路另有三份金标准
（`evaluate_retrieval.py`）。所以本脚本**不经过** `answer_question`，只复用它的提示词构造
（`answering.service.build_messages`）——评测另写一份提示，测出来的就不是线上跑的东西了。

**指标全是确定性的**（见 `app/modules/evaluation/scoring.py`）：引用召回、凭空引用、关键要素
覆盖、禁项触犯、拒答正确性、效力状态提示。**不打语义分**——那要语义裁判，而 §9.5 锁本地模型、
不引入外部服务。

跑法::

    set -a && . ./.env && set +a
    export DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:5432/${POSTGRES_DB}"

    # 只校验数据集（不加载模型，几秒）
    .venv/Scripts/python.exe scripts/evaluate_answering.py --check-only

    # 全量评测（22 条 × 约 25 s 生成）
    .venv/Scripts/python.exe scripts/evaluate_answering.py

    # 冒烟：只跑几条
    .venv/Scripts/python.exe scripts/evaluate_answering.py --ids g01,a01

    # 当门禁用：未达标退出码 1
    .venv/Scripts/python.exe scripts/evaluate_answering.py --gate
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

DEFAULT_DATASET = BACKEND_DIR.parent / "evaluations" / "datasets" / "generation_quality.json"
DEFAULT_OUT = BACKEND_DIR.parent / "evaluations" / "reports"

if not os.environ.get("DATABASE_URL"):
    print(
        "DATABASE_URL 未设置。先在 legalmind/ 下加载环境再运行：\n"
        "  set -a && . ./.env && set +a\n"
        '  export DATABASE_URL="postgresql+asyncpg://$POSTGRES_USER:$POSTGRES_PASSWORD'
        '@127.0.0.1:5432/$POSTGRES_DB"\n'
        "  .venv/Scripts/python.exe scripts/evaluate_answering.py",
        file=sys.stderr,
    )
    raise SystemExit(2)

from sqlalchemy import select

from app.adapters import generation
from app.core.database import SessionFactory, engine
from app.models import LegalInstrument, LegalVersion, ProvisionIdentity, ProvisionVersion
from app.modules.answering import claims, service, verification
from app.modules.answering.evidence import evidence_display
from app.modules.evaluation import scoring

NO_ANSWER = "（未评测：模型不可用）"


@dataclass(frozen=True)
class PinnedProvision:
    """一条被钉住的证据条款。

    字段名与 `retrieval.schemas.ProvisionHit` 对齐，**这样可以直接喂给
    `answering.service.build_messages`**——评测用的就是线上那条提示词。
    """

    instrument_title: str
    provision_number: str
    provision_display: str
    legal_status: str
    text: str


async def load_corpus(pairs: set[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """按 (法, 条) 取出条款正文、效力状态与展示标签。**取不到就是数据集与语料脱节**。"""
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
            key = (title, number)
            if key not in pairs:
                continue
            display = number
            if isinstance(structure_path, dict):
                display = structure_path.get("article") or display
            corpus[key] = {"text": text, "legal_status": status, "display": display}
    return corpus


def _evidence_items(case: dict, corpus: dict) -> tuple[list[PinnedProvision], list[dict]]:
    """把用例的 `evidence` 变成（喂给模型的对象，打分用的字典）。"""
    pins: list[PinnedProvision] = []
    for law, article in case.get("evidence", []):
        entry = corpus[(law, article)]
        pins.append(
            PinnedProvision(
                instrument_title=law,
                provision_number=article,
                provision_display=entry["display"],
                legal_status=entry["legal_status"],
                text=entry["text"],
            )
        )
    return pins, [
        {"law": law, "article": article, **corpus[(law, article)]}
        for law, article in case["evidence"]
    ]


def _markdown(payload: dict) -> str:
    summary = payload["summary"]
    lines = [
        "# 生成质量评测报告",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 数据集：`{payload['dataset']}`（{summary['cases']} 条用例）",
        f"- 模型：`{payload['model']}`（温度 0）",
        f"- 结论：**{'达标' if summary['passed'] else '未达标'}**",
        "",
        "## 指标",
        "",
        "| 指标 | 值 | 阈值 | 方向 | 达标 |",
        "| --- | --- | --- | --- | --- |",
    ]
    labels = {
        "citation_recall": "引用召回",
        "no_fabrication_rate": "无凭空引用",
        "fact_coverage": "关键要素覆盖",
        "forbidden_rate": "禁项触犯率",
        "abstain_accuracy": "拒答正确率",
        "over_abstain_rate": "过度拒答率",
        "status_flag_rate": "效力状态提示率",
    }
    for key, check in summary["checks"].items():
        value = "—" if check["value"] is None else f"{check['value']:.3f}"
        direction = "越低越好" if check["lower_is_better"] else "越高越好"
        ok = "—" if check["ok"] is None else ("是" if check["ok"] else "**否**")
        lines.append(
            f"| {labels.get(key, key)} | {value} | {check['threshold']} | {direction} | {ok} |"
        )

    lines += [
        "",
        "## 诊断（不计入达标）",
        "",
    ]
    for key, item in summary.get("diagnostics", {}).items():
        value = "—" if item["value"] is None else f"{item['value']:.3f}"
        lines.append(f"- {labels.get(key, key)}：{value}（{item['note']}）")

    lines += [
        "",
        "## 逐条",
        "",
        "| 用例 | 类别 | 引用召回 | 关键要素 | 引用编号 | 凭空引用 | 禁项 | 拒答 | 耗时 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in payload["records"]:
        recall = "—" if record["citation_recall"] is None else f"{record['citation_recall']:.2f}"
        facts = "—" if record["fact_coverage"] is None else f"{record['fact_coverage']:.2f}"
        unwarranted = ", ".join(record["unwarranted_citations"]) or "—"
        forbidden = ", ".join(record["forbidden_hits"]) or "—"
        abstain = "对" if record["abstain_correct"] else "**错**"
        cited = ", ".join(record.get("cited_evidence_ids") or []) or "—"
        lines.append(
            f"| {record['id']} | {record['category']} | {recall} | {facts} | {cited} | "
            f"{unwarranted} | {forbidden} | {abstain} | {record['seconds']:.1f}s |"
        )

    lines += ["", "## 失败明细", ""]
    failures = [r for r in payload["records"] if r["problems"]]
    if not failures:
        lines.append("无。")
    for record in failures:
        lines.append(f"### {record['id']}（{record['category']}）")
        lines.append("")
        lines.append(f"- 问题：{'；'.join(record['problems'])}")
        lines.append(f"- 期望引用：{record['expected_citations']}")
        lines.append(f"- 答案：{record['answer'] or '（空）'}")
        lines.append("")

    flagged = [r for r in payload["records"] if r.get("diagnostics")]
    if flagged:
        lines += ["## 诊断明细", ""]
        for record in flagged:
            lines.append(f"- {record['id']}：{'；'.join(record['diagnostics'])}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _problems(record: dict) -> list[str]:
    """把一条结果里「该报告出来的」问题列出来——达标与否由 `summarise` 判，这里给明细。"""
    issues = []
    if record["expect_abstain"]:
        if not record["abstained"]:
            issues.append("该拒答却作答")
    else:
        if record["abstained"]:
            issues.append("不该拒答却拒答")
        if record["citation_recall"] is not None and record["citation_recall"] < 1.0:
            issues.append(
                f"引用召回不足（命中 {record['cited_expected']} / 期望 {record['expected_citations']}）"
            )
        if record["missing_facts"]:
            issues.append(f"缺关键要素：{record['missing_facts']}")
    if record["unwarranted_citations"]:
        issues.append(f"凭空引用：{record['unwarranted_citations']}")
    if record.get("unwarranted_evidence_ids"):
        issues.append(f"引用了不存在的证据编号：{record['unwarranted_evidence_ids']}")
    if record["forbidden_hits"]:
        issues.append(f"说了干扰条款的规则：{record['forbidden_hits']}")
    if record.get("format_ok") is False:
        issues.append(f"没有按 §9.2 输出结构化结果：{record.get('format_error')}")
    return issues


def _diagnostics(record: dict) -> list[str]:
    """只报告、不算问题。

    **效力状态提示是回答层的职责**（§8.3），不是模型该做的事——所以「模型没自己想到说」是
    诊断信息，不是模型的缺陷。回答层的强制提示由 `answering/service.py` 确定性实现、
    `tests/test_answering.py` 守。
    """
    notes = []
    if record["status_flag_required"] and not record["status_flagged"]:
        notes.append("模型未主动提示依据未生效（回答层会强制提示，见 §8.3）")
    if record.get("verification_ok") is False:
        notes.append(f"§9.3 第一层门禁会拦下这条：{record['verification_issues']}")
    if record.get("repaired"):
        notes.append("模型输出需要容错修复才能解析（多段 JSON 并列）")
    return notes


async def run(args) -> int:
    dataset = scoring.load_dataset(args.dataset)

    structural = scoring.validate_structure(dataset)
    if structural:
        print("数据集结构有问题：", file=sys.stderr)
        for problem in structural:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    cases = dataset["cases"]
    if args.ids:
        wanted = {item.strip() for item in args.ids.split(",") if item.strip()}
        cases = [case for case in cases if case["id"] in wanted]
        if not cases:
            print(f"--ids 没有匹配到用例：{args.ids}", file=sys.stderr)
            return 2

    pairs = {
        (law, article) for case in dataset["cases"] for law, article in case.get("evidence", [])
    }
    pairs |= {
        (law, article)
        for case in dataset["cases"]
        for law, article in case.get("expect_citations", [])
    }
    corpus = await load_corpus(pairs)

    # 「期望是推导的、可复核的」：每条事实都要能在条款正文里找到出处
    drift = scoring.validate_against_corpus(dataset, corpus)
    if drift:
        print("数据集与语料不一致（期望不再可复核）：", file=sys.stderr)
        for problem in drift:
            print(f"  - {problem}", file=sys.stderr)
        return 2
    print(f"数据集自检通过：{len(dataset['cases'])} 条用例，{len(corpus)} 条条款。")

    if args.check_only:
        return 0

    model_name = args.model or generation.DEFAULT_MODEL
    if not generation.available(model_name):
        print(
            f"本地生成模型不可用：{model_name}。§9.5 锁定本地模型、不回落外部服务——"
            "先确认 Ollama 在跑且模型已导入（ops/Modelfile.qwen2.5-7b）。",
            file=sys.stderr,
        )
        return 3

    records = []
    for case in cases:
        pins, evidence = _evidence_items(case, corpus)
        started = time.perf_counter()
        raw = generation.generate(
            model_name,
            service.build_messages(pins, case["question"]),
            max_new_tokens=args.max_new_tokens,
            schema=claims.SCHEMA,
        )
        seconds = time.perf_counter() - started
        # §9.2：模型给的是「主张 + 证据编号」，所以打分与核验都对着结构，不再从自由文本里猜引用
        parsed = claims.parse(raw)
        if parsed.ok:
            record = scoring.score_case(
                case,
                "\n".join(claim.text for claim in parsed.answer.claims),
                evidence,
                cited_evidence_ids=parsed.answer.cited_evidence_ids,
                abstained=parsed.answer.abstained,
            )
            record["answer"] = claims.render(parsed.answer, evidence_display(pins))
            verified = verification.verify_claims(parsed.answer, case["question"], pins)
            record["verification_ok"] = verified.ok
            record["verification_issues"] = [issue.detail for issue in verified.issues]
        else:
            # 解析都失败就无从核验——把 `verification_ok` 留空，别污染门禁触发率
            record = scoring.score_case(case, raw, evidence, cited_evidence_ids=(), abstained=False)
            record["answer"] = raw
            record["verification_ok"] = None
            record["verification_issues"] = []
        record["format_ok"] = parsed.ok
        record["repaired"] = parsed.repaired
        record["format_error"] = parsed.error
        record["cited_evidence_ids"] = list(parsed.answer.cited_evidence_ids) if parsed.ok else []
        record["question"] = case["question"]
        record["seconds"] = seconds
        record["note"] = case.get("note")
        record["problems"] = _problems(record)
        record["diagnostics"] = _diagnostics(record)
        records.append(record)
        flag = "OK" if not record["problems"] else "!!"
        print(f"  [{flag}] {record['id']} {seconds:5.1f}s  {case['question']}")

    summary = scoring.summarise(records, dataset.get("thresholds", {}))
    payload = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dataset": str(Path(args.dataset).name),
        "model": model_name,
        "max_new_tokens": args.max_new_tokens,
        "summary": summary,
        "records": records,
    }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"answering_eval_{stamp}.json"
    md_path = out_dir / f"answering_eval_{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_markdown(payload), encoding="utf-8")

    print()
    print(f"结论：{'达标' if summary['passed'] else '未达标'}")
    for key, check in summary["checks"].items():
        value = "—" if check["value"] is None else f"{check['value']:.3f}"
        mark = "—" if check["ok"] is None else ("✓" if check["ok"] else "✗")
        print(f"  {mark} {key}: {value}（阈值 {check['threshold']}）")
    for key, item in summary.get("diagnostics", {}).items():
        value = "—" if item["value"] is None else f"{item['value']:.3f}"
        print(f"  · {key}: {value}（诊断，{item['note']}）")
    print(f"报告：{json_path}")
    print(f"      {md_path}")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    if args.gate and not summary["passed"]:
        return 1
    return 0


async def _main(args) -> int:
    try:
        return await run(args)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="生成质量评测（需要本地模型；只读数据库）")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--model", default=None, help="覆盖生成模型（默认取配置里的）")
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--ids", default="", help="逗号分隔的用例 id，只跑这些（冒烟用）")
    parser.add_argument("--check-only", action="store_true", help="只校验数据集，不加载模型")
    parser.add_argument("--gate", action="store_true", help="未达标时退出码 1")
    parser.add_argument("--json", action="store_true")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
