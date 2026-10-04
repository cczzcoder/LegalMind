"""本地嵌入模型适配层（设计 8.2、9.5）。

**数据不默认外发**（§9.5「敏感资料是否允许外发必须单独确认，不默认外发」），因此只走本地模型。

``sentence-transformers`` 的依赖很重（torch 以 GB 计），它在可选依赖 ``embeddings`` 里，
所以**导入放在函数内部**：没装时给出可操作提示，而不是抛一段 ImportError 堆栈。

**为什么自己下模型而不用 huggingface_hub**：本机 HuggingFace 直连不通，只能走镜像；而镜像会先
``HEAD /resolve/...`` 拿到 307，跳到 ``/api/resolve-cache/...``，该路径返回 **403**，于是文件落成
**0 字节**（实测，hf_hub_download 与 snapshot_download 都一样）。``/resolve/`` 直链本身是好的，
所以这里按已知文件布局直接拉，缺哪个跳过哪个。
"""

import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.core.config import get_settings

# 各仓库共有的文件；不在仓库里的会被 404 跳过
_SHARED_FILES = (
    "config.json",
    "config_sentence_transformers.json",
    "modules.json",
    "sentence_bert_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
    "sentencepiece.bpe.model",
    "1_Pooling/config.json",
)
# 权重文件：按顺序取第一个存在的（BGE-M3 只有 .bin，text2vec 两者都有）
_WEIGHT_FILES = ("model.safetensors", "pytorch_model.bin")


@dataclass(frozen=True)
class EmbeddingModel:
    """一个可用的本地嵌入模型。``name`` 同时是写进 ``provision_embeddings.model`` 的标识。"""

    name: str
    dimensions: int
    max_tokens: int
    note: str


MODELS: tuple[EmbeddingModel, ...] = (
    EmbeddingModel("BAAI/bge-m3", 1024, 8192, "长文本（8192 位置）、中文检索精度高；主选"),
    EmbeddingModel(
        "shibing624/text2vec-base-chinese", 768, 512, "轻量快速、成本极低；512 位置上限"
    ),
)

DEFAULT_MODEL = MODELS[0].name


def model(name: str) -> EmbeddingModel:
    for item in MODELS:
        if item.name == name:
            return item
    raise ValueError(f"Unknown embedding model: {name}（可选：{[m.name for m in MODELS]}）")


def model_dir(name: str) -> Path:
    return Path(get_settings().embedding_model_dir).expanduser() / name.replace("/", "__")


def _fetch(url: str, destination: Path, *, label: str = "") -> int:
    """下到 ``<name>.part`` 再改名——中途断了不会留下一个"看起来完整"的半截权重。

    每 100 MB 打一次进度：本机到镜像只有 ~0.6 MB/s，BGE-M3 要下 2.2 GB，没有进度会以为卡死。
    """
    partial = destination.with_name(destination.name + ".part")
    partial.parent.mkdir(parents=True, exist_ok=True)  # 仓库里有 1_Pooling/ 这类嵌套目录
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    total = 0
    mark = 0
    try:
        with (
            urllib.request.urlopen(request, timeout=60) as response,
            partial.open("wb") as handle,
        ):
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                handle.write(chunk)
                total += len(chunk)
                if label and total - mark >= 100 * 1024 * 1024:
                    mark = total
                    print(f"    … {label} {total / 1048576:.0f} MB", flush=True)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(destination)
    return total


def download(name: str, *, force: bool = False) -> Path:
    """把模型文件下到 ``model_dir(name)``，返回该目录（可被 ``SentenceTransformer`` 直接加载）。"""
    spec = model(name)
    directory = model_dir(name)
    directory.mkdir(parents=True, exist_ok=True)
    base = f"{get_settings().hf_endpoint.rstrip('/')}/{spec.name}/resolve/main"
    downloaded: list[str] = []
    for filename in _SHARED_FILES:
        target = directory / filename
        if target.is_file() and target.stat().st_size > 0 and not force:
            continue
        try:
            size = _fetch(f"{base}/{filename}", target, label=filename)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                continue
            raise
        downloaded.append(f"{filename} ({size / 1048576:.1f} MB)")
        print(f"  + {filename} ({size / 1048576:.1f} MB)", flush=True)
    if not (directory / "config.json").is_file():
        raise RuntimeError(f"{spec.name} 的 config.json 没下下来，检查 {base}")

    weights = [name_ for name_ in _WEIGHT_FILES if (directory / name_).is_file()]
    if not weights:
        for filename in _WEIGHT_FILES:
            try:
                size = _fetch(f"{base}/{filename}", directory / filename, label=filename)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    continue
                raise
            downloaded.append(f"{filename} ({size / 1048576:.1f} MB)")
            print(f"  + {filename} ({size / 1048576:.1f} MB)", flush=True)
            break
    print(f"模型就绪：{directory}（本次下载 {len(downloaded)} 个文件）", flush=True)
    return directory


@lru_cache(maxsize=2)
def load(name: str):
    """加载模型；进程内缓存，因此第一次之后不再读盘。

    ⚠️ 缓存意味着**模型会常驻进程内存**（BGE-M3 约 2 GB）。设计 §9.5 说第一阶段 GPU 优先用于
    分阶段运行 Embedding/Reranker/OCR、不默认同时加载多个模型；要长期常驻应按第 18 节评估拆成
    独立的嵌入服务。
    """
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as error:  # pragma: no cover - 只在没装可选依赖时触发
        raise RuntimeError(
            "缺少本地嵌入依赖。安装：.venv/Scripts/python.exe -m pip install -e .[embeddings]"
            "（torch 体积以 GB 计，故意不放进默认依赖）"
        ) from error
    directory = model_dir(name)
    if not (directory / "config.json").is_file():
        print(f"首次使用，下载 {name} 权重 …", flush=True)
        download(name)
    return SentenceTransformer(str(directory))


def encode(
    name: str, texts: list[str], *, batch_size: int = 16, progress: bool = False
) -> list[list[float]]:
    """把文本编码成**归一化**向量（于是余弦相似度就是点积，与 ``cosine_distance`` 对齐）。"""
    vectors = load(name).encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=progress,
    )
    return [list(map(float, row)) for row in vectors]
