"""本地生成模型适配层（设计 §9.4、§9.5）。

**只走本地模型，且不提供任何外部 API 的回落路径。** §9.5 的决策是「因合规要求，P6 架构锁定为
本地部署模型，默认关闭任何外部 API 接口」，所以这里**没有**「没配本地模型就调外部服务」这种分支：
配置缺失就是**不可用**，如实降级成「只给检索证据 + 转人工」，绝不偷偷外发。

**为什么用 transformers 读 GGUF，而不是 llama-cpp-python**：

- ``llama-cpp-python`` 在 PyPI 上只有源码包，Windows 上解压会撞 260 字符路径上限，装上也要 MSVC 编译；
- 它官方的预编译 wheel 索引（``abetlen.github.io``）可达，但 wheel 本体托管在 **GitHub releases**，
  本机不可达（502）。

``transformers`` 的 GGUF 支持正好够用：权重**以量化形态驻留**（``GGUFLinear`` 在 forward 里按需
反量化），不会展开成 fp16 的 14 GB——本机 15 GB 内存放不下展开后的模型。

模型按需加载、用完即释（§9.5 不默认常驻本地生成模型）；本模块用 ``lru_cache`` 缓存，调用方
决定何时清。
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.adapters import hf_mirror
from app.core.config import get_settings


@dataclass(frozen=True)
class GenerationModel:
    """一个可用的本地生成模型。``gguf_file`` 是量化权重文件名。"""

    name: str
    gguf_file: str
    quantization: str
    note: str


MODELS: tuple[GenerationModel, ...] = (
    GenerationModel(
        name="Qwen2.5-7B-Instruct-GGUF",
        gguf_file="Qwen2.5-7B-Instruct-Q4_K_M.gguf",
        quantization="Q4_K_M",
        note="Qwen2.5-7B-Instruct 的 Q4_K_M 量化，约 4.4 GB；**本机暂时跑不起来**（见下）",
    ),
    GenerationModel(
        name="Qwen2.5-1.5B-Instruct-GGUF",
        gguf_file="Qwen2.5-1.5B-Instruct-Q4_K_M.gguf",
        quantization="Q4_K_M",
        note="同族的 1.5B 量化，约 0.9 GB；**只用来验证链路**，不是选定的生产模型",
    ),
)

DEFAULT_MODEL = MODELS[0].name

# ⚠️ **本机跑 7B 的两个硬约束**（都不是代码问题）：
#
# 1. ``transformers`` 读 GGUF 会**把整个模型反量化成 fp32**（``gguf.quants.dequantize`` 返回
#    float32），7B 需要约 28 GB，本机 15 GB 内存放不下——实测 OOM 在「Unable to allocate 259 MiB」。
#    也就是说「量化权重」只在磁盘上省空间，**加载后不省内存**，这一层必须交给真正的量化推理引擎。
# 2. 真正的引擎（``llama-cpp-python``）在本机装不了：PyPI 只有源码包，解压撞 Windows 260 字符
#    路径上限；官方预编译 wheel 的索引可达但 wheel 本体托管在 **GitHub releases**，本机不可达（502）；
#    退而求其次自己编译，`vcvars64.bat` 内部要调 ``reg.exe``，而它被本机安全策略**明确封禁**。
#
# 所以 7B 这条路的**代码已经写好**（换 ``GENERATION_MODEL`` 即可），卡在运行环境上。
_TOKENIZER_FILES = (
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
    "generation_config.json",
)


def model(name: str) -> GenerationModel:
    for item in MODELS:
        if item.name == name:
            return item
    raise ValueError(f"Unknown generation model: {name}（可选：{[m.name for m in MODELS]}）")


def model_dir(name: str) -> Path:
    return Path(get_settings().embedding_model_dir).expanduser() / name.replace("/", "__")


def download(name: str, *, force: bool = False) -> Path:
    spec = model(name)
    directory = model_dir(name)
    hf_mirror.download_repo(
        spec.name,
        directory,
        _TOKENIZER_FILES,
        endpoint=get_settings().hf_endpoint,
        weights=(spec.gguf_file,),
        force=force,
    )
    if not (directory / spec.gguf_file).is_file():
        raise RuntimeError(f"{spec.gguf_file} 没下下来，检查 {get_settings().hf_endpoint}")
    return directory


@lru_cache(maxsize=1)
def load(name: str):
    """加载分词器与模型；进程内缓存。

    ⚠️ 常驻内存约 5 GB（量化权重 4.4 GB + KV cache）。§9.5 要求生成模型**按需加载、用完即释**，
    所以调用方拿到结果后应调 ``release()``，别把它留在内存里和 Embedding / Reranker 抢。
    """
    try:
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as error:  # pragma: no cover
        raise RuntimeError("缺少 transformers，安装：pip install -e .") from error

    spec = model(name)
    directory = model_dir(name)
    if not (directory / spec.gguf_file).is_file():
        print(f"首次使用，下载 {spec.gguf_file} …", flush=True)
        download(name)
    tokenizer = AutoTokenizer.from_pretrained(str(directory), gguf_file=spec.gguf_file)
    language_model = AutoModelForCausalLM.from_pretrained(str(directory), gguf_file=spec.gguf_file)
    language_model.eval()
    return tokenizer, language_model


def release() -> None:
    """释放常驻的模型内存（§9.5 不默认常驻生成模型）。

    调用方拿到结果后应调它——否则 5 GB 会和 Embedding / Reranker 一起挤在同一台机器上。
    """
    load.cache_clear()


def generate(
    name: str,
    messages: list[dict],
    *,
    max_new_tokens: int = 512,
) -> str:
    """按对话消息生成回复。**贪心解码**（``do_sample=False``）——法律场景要可复现。"""
    import torch

    tokenizer, language_model = load(name)
    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer(prompt, return_tensors="pt")
    with torch.no_grad():
        output = language_model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            pad_token_id=tokenizer.eos_token_id,
        )
    completion = output[0][inputs["input_ids"].shape[-1] :]
    return tokenizer.decode(completion, skip_special_tokens=True).strip()
