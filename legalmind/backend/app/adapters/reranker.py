"""本地重排序模型适配层（交叉编码器，设计 §8.2 的「重排序」）。

交叉编码器把**查询与候选条文一起**送进模型打分，所以比双塔的向量相似度更能分辨细粒度相关
——代价是**不能预先建索引**，只能对候选集重排。

**为什么到现在才做**：市面流水线是「召回 → RRF → 重排序」，RRF 把多路结果摊平、**由重排序收拾
噪声**。本项目先做的是**级联**（关键词优先、命中为空才向量），因为它在没有重排序时也能保住精度。
补上重排序之后才能实测回答「RRF + 重排序」是否优于级联（见 `doc/技术决策与踩坑记录.md`）。

依赖与嵌入模型一样在可选依赖 ``embeddings`` 里（``sentence-transformers``），所以导入放在函数内部。
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.adapters import hf_mirror
from app.core.config import get_settings

# 交叉编码器用 AutoModelForSequenceClassification + AutoTokenizer，不需要 modules.json 那套
_SHARED_FILES = (
    "config.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "sentencepiece.bpe.model",
    "vocab.txt",
)
_WEIGHT_FILES = ("model.safetensors", "pytorch_model.bin")


@dataclass(frozen=True)
class RerankerModel:
    """一个可用的本地重排序模型。"""

    name: str
    note: str


MODELS: tuple[RerankerModel, ...] = (
    RerankerModel("BAAI/bge-reranker-base", "交叉编码器，中英双语；权重约 1.1 GB"),
)

DEFAULT_MODEL = MODELS[0].name


def model(name: str) -> RerankerModel:
    for item in MODELS:
        if item.name == name:
            return item
    raise ValueError(f"Unknown reranker model: {name}（可选：{[m.name for m in MODELS]}）")


def model_dir(name: str) -> Path:
    return Path(get_settings().embedding_model_dir).expanduser() / name.replace("/", "__")


def download(name: str, *, force: bool = False) -> Path:
    spec = model(name)
    directory = model_dir(name)
    hf_mirror.download_repo(
        spec.name,
        directory,
        _SHARED_FILES,
        endpoint=get_settings().hf_endpoint,
        weights=_WEIGHT_FILES,
        force=force,
    )
    # ⚠️ 收尾校验要连**权重**一起看，理由见 `hf_mirror.is_complete` 的注释
    if not hf_mirror.is_complete(directory, _WEIGHT_FILES):
        raise RuntimeError(
            f"{spec.name} 没下全（缺 config.json 或权重），检查 {get_settings().hf_endpoint}"
        )
    return directory


@lru_cache(maxsize=1)
def load(name: str):
    """加载交叉编码器；进程内缓存。

    ⚠️ 与嵌入模型一样会**常驻内存**（bge-reranker-base 约 1.1 GB）。设计 §9.5 说第一阶段 GPU
    优先用于分阶段运行 Embedding/Reranker/OCR、**不默认同时加载多个模型**——重排序要跑就与嵌入
    模型同时驻留，约 3 GB，上生产前按第 18 节评估。
    """
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as error:  # pragma: no cover - 只在没装可选依赖时触发
        raise RuntimeError(
            "缺少本地嵌入依赖。安装：.venv/Scripts/python.exe -m pip install -e .[embeddings]"
        ) from error
    directory = model_dir(name)
    # ⚠️ **别只看 config.json**（理由见 `hf_mirror.is_complete`）：半截目录会跳过下载、永不复发
    if not hf_mirror.is_complete(directory, _WEIGHT_FILES):
        print(f"首次使用（或上次没下全），下载 {name} 权重 …", flush=True)
        download(name)
    return CrossEncoder(str(directory))


def score(name: str, query: str, texts: list[str], *, batch_size: int = 8) -> list[float]:
    """给「查询 + 每条候选」打分，越大越相关。"""
    if not texts:
        return []
    pairs = [[query, text] for text in texts]
    values = load(name).predict(pairs, batch_size=batch_size)
    return [float(value) for value in values]
