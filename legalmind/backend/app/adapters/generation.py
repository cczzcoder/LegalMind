"""本地生成模型适配层（设计 §9.4、§9.5）。

**只走本地模型，且不提供任何外部 API 的回落路径。** §9.5 的决策是「因合规要求，P6 架构锁定为
本地部署模型，默认关闭任何外部 API 接口」，所以这里**没有**「没配本地模型就调外部服务」这种分支：
模型不可用就是**不可用**，`available()` 返回 ``False``，由调用方如实降级成「只给检索证据 +
转人工」，绝不偷偷外发。

**运行时用 Ollama**，权重是 Qwen2.5-7B-Instruct 的 Q4_K_M GGUF。

⚠️ **为什么不直接在 Python 里读 GGUF**：``transformers`` 加载 GGUF 时会把整个模型**反量化成
fp32**（``gguf.quants.dequantize`` 返回 float32），7B 需要约 28 GB 内存，普通开发机放不下——
实测 OOM 在「Unable to allocate 259 MiB」。**量化只在磁盘上省空间，加载后不省内存**，这一层
必须交给真正的量化推理引擎。另一条路 ``llama-cpp-python`` 在本机装不上（源码包撞 Windows 260
字符路径上限、预编译 wheel 在 GitHub 不可达、自己编译又卡在安全策略封禁 ``reg.exe``），
两条都在《技术决策与踩坑记录》§5.1 里。

所以：**权重由本模块下到本地**（`download()`），**由 Ollama 导入并推理**（`ops/Modelfile.qwen2.5-7b`）。
"""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from app.adapters import hf_mirror
from app.core.config import get_settings


@dataclass(frozen=True)
class GenerationModel:
    """一个可用的本地生成模型。

    ``name`` 是 **Ollama 里的模型名**（由 ``ops/Modelfile.qwen2.5-7b`` 创建）；
    ``source`` / ``gguf_file`` 是下载权重用的，与推理运行时无关。
    """

    name: str
    source: str
    gguf_file: str
    quantization: str
    note: str


MODELS: tuple[GenerationModel, ...] = (
    GenerationModel(
        name="legalmind-qwen2.5-7b",
        source="bartowski/Qwen2.5-7B-Instruct-GGUF",
        gguf_file="Qwen2.5-7B-Instruct-Q4_K_M.gguf",
        quantization="Q4_K_M",
        note="Qwen2.5-7B-Instruct Q4_K_M，约 4.4 GB；由 Ollama 承载，常驻约 5 GB",
    ),
)

DEFAULT_MODEL = MODELS[0].name


def model(name: str) -> GenerationModel:
    for item in MODELS:
        if item.name == name:
            return item
    raise ValueError(f"Unknown generation model: {name}（可选：{[m.name for m in MODELS]}）")


def model_dir(name: str) -> Path:
    """GGUF 权重的落地目录（Ollama 导入时从这里读）。"""
    spec = model(name)
    return Path(get_settings().embedding_model_dir).expanduser() / Path(spec.gguf_file).stem


def download(name: str, *, force: bool = False) -> Path:
    """把 GGUF 权重下到本地。**导入 Ollama 是另一步**（见 ``ops/Modelfile.qwen2.5-7b``）。"""
    spec = model(name)
    directory = model_dir(name)
    hf_mirror.download_repo(
        spec.source,
        directory,
        (),
        endpoint=get_settings().hf_endpoint,
        weights=(spec.gguf_file,),
        force=force,
    )
    if not (directory / spec.gguf_file).is_file():
        raise RuntimeError(f"{spec.gguf_file} 没下下来，检查 {get_settings().hf_endpoint}")
    return directory


def _post(path: str, payload: dict, *, timeout: float) -> dict:
    request = urllib.request.Request(
        f"{get_settings().ollama_host.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def available(name: str) -> bool:
    """本地模型是否真的可用——**不可用就是不可用**，由调用方如实降级，不做任何外部回落。"""
    try:
        request = urllib.request.Request(f"{get_settings().ollama_host.rstrip('/')}/api/tags")
        with urllib.request.urlopen(request, timeout=5) as response:
            tags = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False
    # Ollama 的模型名带 ``:latest`` 后缀，别只按全等匹配——实测这样会误判成「不可用」
    names = {item.get("name", "") for item in tags.get("models", [])}
    return name in names or f"{name}:latest" in names


def generate(
    name: str,
    messages: list[dict],
    *,
    max_new_tokens: int = 512,
    timeout: float = 600.0,
) -> str:
    """按对话消息生成回复。

    温度取 0（**贪心解码**）——法律场景要可复现，同一个问题不该给出两个答案。
    """
    payload = {
        "model": name,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0, "num_predict": max_new_tokens},
    }
    result = _post("/api/chat", payload, timeout=timeout)
    return (result.get("message") or {}).get("content", "").strip()
