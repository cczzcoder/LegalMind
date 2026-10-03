"""第 1 层解析器样本基准（设计 §6、§17.2）。

对样本目录里的真实 PDF 分别跑 pypdfium2 与 pdfplumber，测量：

- **速度**：总耗时、字符/秒
- **内存**：后台线程采样进程 RSS 得到的峰值增长（16 GB 单机 OOM 风险的直接依据）
- **§6 定位合规性**：块级 bbox 覆盖率、是否越界、字符偏移是否连续、chunk 能否还原原文
- **双后端一致性**：文本差异率、块数差、对应块 bbox 的平均 IoU（定位粒度是否一致）

对 DOCX 跑 python-docx 解析器，确认段落结构与偏移。

用法::

    .venv/Scripts/python.exe scripts/benchmark_parsers.py [样本目录] [--out 报告目录]

样本目录默认仓库根下的 ``法律文献/``。报告写成 JSON + Markdown，默认落到
``evaluations/reports/``（该目录已被 .gitignore 忽略，报告留在本机）。
"""

import argparse
import json
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

# 预热：把库导入开销排除在耗时/内存测量之外（否则第一个文件会背锅）

# 解析本身不需要数据库；在导入 app 之前给出占位配置，避免 Settings 校验失败
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://unused:unused@localhost/unused")

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.modules.documents.validation import DOCX, PDF
from app.modules.parsing.chunking import chunk_blocks
from app.modules.parsing.interface import ParseResult
from app.modules.parsing.memory import current_rss_bytes, peak_working_set_bytes
from app.modules.parsing.registry import PDF_BACKENDS, parse_document

DEFAULT_SAMPLES = BACKEND_DIR.parent.parent / "法律文献"
DEFAULT_OUT = BACKEND_DIR.parent / "evaluations" / "reports"
SAMPLE_INTERVAL = 0.005


class PeakSampler:
    """后台线程采样 RSS 峰值；跨平台，不依赖 psutil。"""

    def __init__(self, interval: float = SAMPLE_INTERVAL) -> None:
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.baseline = 0
        self.peak = 0

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, current_rss_bytes())
            self._stop.wait(self._interval)

    def __enter__(self) -> Self:
        self.baseline = current_rss_bytes()
        self.peak = self.baseline
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    @property
    def growth_mb(self) -> float:
        return max(0, self.peak - self.baseline) / (1024 * 1024)


@dataclass
class RunMetrics:
    file: str
    media_type: str
    backend: str
    parser: str
    parser_version: str
    pages: int
    blocks: int
    chunks: int
    chars: int
    seconds: float
    chars_per_second: float
    peak_rss_mb: float
    bbox_coverage: float
    bbox_in_range: float
    offsets_contiguous: bool
    spans_valid: bool
    round_trip_ok: bool
    quality_status: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class Comparison:
    file: str
    block_count_delta: int
    chars_delta: int
    text_identical: bool
    char_diff_ratio: float
    mean_bbox_iou: float
    min_bbox_iou: float


def _iou(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / union if union > 0 else 0.0


def _char_diff_ratio(left: str, right: str) -> float:
    if left == right:
        return 0.0
    length = max(len(left), len(right))
    if not length:
        return 0.0
    return (
        sum(1 for a, b in zip(left, right) if a != b) / length
        + abs(len(left) - len(right)) / length
    )


def _measure(
    result: ParseResult,
    seconds: float,
    peak_rss_mb: float,
    name: str,
    media_type: str,
    backend: str,
) -> RunMetrics:
    chunks = chunk_blocks(result.blocks, result.text)
    joined = "\n".join(chunk.text for chunk in chunks)
    boxes = [b.bbox for b in result.blocks]
    with_bbox = [b for b in boxes if b]
    in_range = [b for b in with_bbox if 0 <= b[0] <= b[2] <= 1 and 0 <= b[1] <= b[3] <= 1]
    contiguous = all(
        result.blocks[i].char_end + 1 == result.blocks[i + 1].char_start
        for i in range(len(result.blocks) - 1)
    )
    spans_valid = all(
        span.ordinal == index and span.block.char_end > span.block.char_start
        for chunk in chunks
        for index, span in enumerate(chunk.spans)
    )
    return RunMetrics(
        file=name,
        media_type=media_type,
        backend=backend,
        parser=result.parser,
        parser_version=result.parser_version,
        pages=result.page_count,
        blocks=len(result.blocks),
        chunks=len(chunks),
        chars=result.char_count,
        seconds=round(seconds, 4),
        chars_per_second=round(result.char_count / seconds, 1) if seconds > 0 else 0.0,
        peak_rss_mb=round(peak_rss_mb, 1),
        bbox_coverage=round(len(with_bbox) / len(boxes), 4) if boxes else 0.0,
        bbox_in_range=round(len(in_range) / len(with_bbox), 4) if with_bbox else 0.0,
        offsets_contiguous=contiguous,
        spans_valid=spans_valid,
        round_trip_ok=joined == result.text,
        quality_status=result.quality_status,
        warnings=list(result.warnings),
    )


def _parse_and_measure(path: Path, media_type: str, backend: str) -> tuple[RunMetrics, ParseResult]:
    content = path.read_bytes()
    before_peak = peak_working_set_bytes()
    with PeakSampler() as sampler:
        started = time.perf_counter()
        result = parse_document(content, media_type, pdf_backend=backend)
        seconds = time.perf_counter() - started
    # 采样线程对极短任务会抖动，用单调的峰值工作集差值兜底
    peak_mb = max(sampler.growth_mb, max(0, peak_working_set_bytes() - before_peak) / (1024 * 1024))
    return _measure(result, seconds, peak_mb, path.name, media_type, backend), result


def _compare(name: str, left: ParseResult, right: ParseResult) -> Comparison:
    pairs = [(_iou(a.bbox, b.bbox)) for a, b in zip(left.blocks, right.blocks) if a.bbox and b.bbox]
    return Comparison(
        file=name,
        block_count_delta=len(left.blocks) - len(right.blocks),
        chars_delta=left.char_count - right.char_count,
        text_identical=left.text == right.text,
        char_diff_ratio=round(_char_diff_ratio(left.text, right.text), 4),
        mean_bbox_iou=round(sum(pairs) / len(pairs), 4) if pairs else 0.0,
        min_bbox_iou=round(min(pairs), 4) if pairs else 0.0,
    )


def _recommend(runs: list[RunMetrics], comparisons: list[Comparison]) -> list[str]:
    """按设计 §6 精度要求给结论：先看定位合规，再比速度与内存。"""
    lines: list[str] = []
    by_backend: dict[str, list[RunMetrics]] = {}
    for run in runs:
        if run.media_type == PDF:
            by_backend.setdefault(run.backend, []).append(run)

    compliant: dict[str, bool] = {}
    for backend, items in by_backend.items():
        ok = all(
            item.bbox_coverage == 1.0
            and item.bbox_in_range == 1.0
            and item.offsets_contiguous
            and item.spans_valid
            and item.round_trip_ok
            for item in items
        )
        compliant[backend] = ok
        lines.append(
            f"- `{backend}`：§6 定位合规 = {ok}；"
            f"平均 {sum(i.seconds for i in items) / len(items):.3f}s/篇，"
            f"峰值内存 {max(i.peak_rss_mb for i in items):.1f} MB"
        )

    both = [b for b, ok in compliant.items() if ok]
    if len(both) == len(by_backend) and len(by_backend) > 1:
        fastest = min(by_backend, key=lambda b: sum(i.seconds for i in by_backend[b]))
        lightest = min(by_backend, key=lambda b: max(i.peak_rss_mb for i in by_backend[b]))
        lines.append(
            f"- 两个后端都满足 §6 的定位硬要求，选型由速度/内存决定：最快 `{fastest}`、"
            f"最省内存 `{lightest}`。"
        )
        if comparisons:
            mean_iou = sum(c.mean_bbox_iou for c in comparisons) / len(comparisons)
            lines.append(f"- 双后端定位一致性：对应块 bbox 平均 IoU = {mean_iou:.3f}。")
    elif len(both) == 1:
        lines.append(f"- 只有 `{both[0]}` 满足 §6 定位硬要求，应选它。")
    else:
        lines.append("- 没有后端完全满足 §6 定位硬要求，需人工复核后再定。")
    return lines


def _markdown(runs: list[RunMetrics], comparisons: list[Comparison], conclusion: list[str]) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    out = [f"# 第 1 层解析器样本基准\n\n生成时间：{now}\n", "## 逐次运行\n"]
    out.append(
        "| 文件 | 后端 | 页 | 块 | chunk | 字符 | 秒 | 字符/秒 | 峰值MB | bbox覆盖 | 越界 | 偏移连续 | 还原 | 质量 |"
    )
    out.append(
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    for run in runs:
        out_of_range = "—" if run.bbox_coverage == 0 else f"{1 - run.bbox_in_range:.2f}"
        out.append(
            f"| {run.file} | {run.backend} | {run.pages} | {run.blocks} | {run.chunks} | "
            f"{run.chars} | {run.seconds} | {run.chars_per_second} | {run.peak_rss_mb} | "
            f"{run.bbox_coverage} | {out_of_range} | {run.offsets_contiguous} | "
            f"{run.round_trip_ok} | {run.quality_status} |"
        )
    if comparisons:
        out.append("\n## 双后端一致性\n")
        out.append("| 文件 | 块数差 | 字符差 | 文本相同 | 字符差异率 | 平均IoU | 最小IoU |")
        out.append("| --- | --- | --- | --- | --- | --- | --- |")
        for item in comparisons:
            out.append(
                f"| {item.file} | {item.block_count_delta} | {item.chars_delta} | "
                f"{item.text_identical} | {item.char_diff_ratio} | {item.mean_bbox_iou} | "
                f"{item.min_bbox_iou} |"
            )
    out.append("\n## 结论（对照设计 §6）\n")
    out.extend(conclusion)
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", nargs="?", default=str(DEFAULT_SAMPLES))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    samples = Path(args.samples)
    if not samples.is_dir():
        print(f"sample directory not found: {samples}", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    runs: list[RunMetrics] = []
    comparisons: list[Comparison] = []

    for path in sorted(samples.glob("*.pdf")):
        results: dict[str, ParseResult] = {}
        for backend in PDF_BACKENDS:
            metrics, result = _parse_and_measure(path, PDF, backend)
            runs.append(metrics)
            results[backend] = result
            print(
                f"[pdf/{backend}] {path.name}: {metrics.seconds}s "
                f"{metrics.chars} chars, peak {metrics.peak_rss_mb} MB"
            )
        if len(results) > 1:
            left, right = results["pypdfium2"], results["pdfplumber"]
            comparisons.append(_compare(path.name, left, right))

    for path in sorted(samples.glob("*.docx")):
        metrics, _ = _parse_and_measure(path, DOCX, "python-docx")
        runs.append(metrics)
        print(
            f"[docx] {path.name}: {metrics.seconds}s {metrics.chars} chars, {metrics.blocks} blocks"
        )

    conclusion = _recommend(runs, comparisons)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "samples_dir": str(samples),
        "runs": [asdict(run) for run in runs],
        "comparisons": [asdict(item) for item in comparisons],
        "conclusion": conclusion,
    }
    json_path = out_dir / f"parse_benchmark_{stamp}.json"
    md_path = out_dir / f"parse_benchmark_{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_markdown(runs, comparisons, conclusion), encoding="utf-8")

    print("\n".join(conclusion))
    print(f"\nJSON: {json_path}\nMD:   {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
