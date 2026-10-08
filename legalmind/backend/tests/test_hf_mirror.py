"""模型缓存目录的**就绪判据**（`app/adapters/hf_mirror.py`、`embedding.py`、`reranker.py`）。

不联网、不加载模型——钉的是「什么算下全了」这一条：**配置在**且**至少一个非空权重在**。

⚠️ 为什么值得单独钉：判据原先只看 `config.json`，而它是 `download_repo` 里**第一个**下的文件、
权重是**最后一个**。一次被中断的首次下载会留下「config 在、权重不在」的目录，之后每次 `load()`
都跳过下载，然后在 transformers 深处报一个和「没下全」毫无关系的错（`no file named
model.safetensors, or pytorch_model.bin`）——**而且永远不会自愈**。
"""

import sys
import types
from pathlib import Path

from app.adapters import embedding, hf_mirror, reranker

WEIGHTS = ("model.safetensors", "pytorch_model.bin")


def _write(path: Path, content: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


# ---------------------------------------------------------------- 判据本身


def test_a_directory_with_only_config_is_not_complete(tmp_path):
    """**这条就是那个 bug**：config 在、权重不在 → 不算下全（旧判据会说「好了」）。"""
    _write(tmp_path / "config.json")
    assert hf_mirror.is_complete(tmp_path, WEIGHTS) is False


def test_a_zero_byte_weight_does_not_count(tmp_path):
    """镜像那条老路会把文件落成 0 字节，`is_file()` 会把它当成下好了。"""
    _write(tmp_path / "config.json")
    _write(tmp_path / "pytorch_model.bin", b"")
    assert hf_mirror.is_complete(tmp_path, WEIGHTS) is False


def test_weights_without_config_are_not_complete(tmp_path):
    _write(tmp_path / "pytorch_model.bin")
    assert hf_mirror.is_complete(tmp_path, WEIGHTS) is False


def test_config_plus_any_one_non_empty_weight_is_complete(tmp_path):
    """权重文件名各仓库不一样，**任一**非空即可（BGE-M3 只有 `.bin`）。"""
    _write(tmp_path / "config.json")
    _write(tmp_path / "pytorch_model.bin")
    assert hf_mirror.is_complete(tmp_path, WEIGHTS) is True


def test_an_empty_directory_is_not_complete(tmp_path):
    assert hf_mirror.is_complete(tmp_path, WEIGHTS) is False


# ---------------------------------------------------------------- 下载时的跳过规则


def _fake_fetch(calls: list[str]):
    def fetch(url, destination, *, label=""):
        calls.append(Path(url).name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"weights")
        return 7

    return fetch


def test_a_zero_byte_weight_is_redownloaded(monkeypatch, tmp_path):
    """0 字节的权重不能当成「已存在」——否则和半截目录一样永不复发。"""
    _write(tmp_path / "pytorch_model.bin", b"")
    calls: list[str] = []
    monkeypatch.setattr(hf_mirror, "fetch", _fake_fetch(calls))

    hf_mirror.download_repo(
        "BAAI/bge-m3", tmp_path, (), endpoint="http://x", weights=("pytorch_model.bin",)
    )

    assert calls == ["pytorch_model.bin"]
    assert hf_mirror.is_complete(tmp_path, WEIGHTS) is False  # 缺 config，仍不算全


def test_a_present_weight_is_not_redownloaded(monkeypatch, tmp_path):
    _write(tmp_path / "config.json")
    _write(tmp_path / "pytorch_model.bin")
    calls: list[str] = []
    monkeypatch.setattr(hf_mirror, "fetch", _fake_fetch(calls))

    hf_mirror.download_repo(
        "BAAI/bge-m3", tmp_path, ("config.json",), endpoint="http://x", weights=WEIGHTS
    )

    assert calls == []


# ---------------------------------------------------------------- 收尾校验与 load()


def test_download_refuses_a_repo_that_yielded_no_weights(monkeypatch, tmp_path):
    """收尾校验要连权重一起看——只查 config 的话，权重下失败也会被当成「就绪」。"""
    monkeypatch.setattr(embedding, "model_dir", lambda _name: tmp_path)

    def only_config(_repo, directory, _filenames, **_kwargs):
        _write(Path(directory) / "config.json")
        return ["config.json"]

    monkeypatch.setattr(hf_mirror, "download_repo", only_config)

    try:
        embedding.download("BAAI/bge-m3")
    except RuntimeError as error:
        assert "没下全" in str(error)
    else:  # pragma: no cover - 旧实现会走到这里
        raise AssertionError("只有 config.json 时不该当成下全了")


def test_load_redownloads_a_half_downloaded_directory(monkeypatch, tmp_path):
    """**半截目录要能自愈**：`load()` 发现权重不在就得重新下载，而不是交给 transformers 去报错。"""
    _write(tmp_path / "config.json")  # 只有配置 —— 正是被打断的那次留下的样子
    monkeypatch.setattr(embedding, "model_dir", lambda _name: tmp_path)

    asked: list[str] = []

    def fake_download(name, **_kwargs):
        asked.append(name)
        _write(tmp_path / "pytorch_model.bin")
        return tmp_path

    monkeypatch.setattr(embedding, "download", fake_download)

    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = lambda _path: "loaded"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)

    embedding.load.cache_clear()
    try:
        assert embedding.load("BAAI/bge-m3") == "loaded"
    finally:
        embedding.load.cache_clear()

    assert asked == ["BAAI/bge-m3"]


def test_load_does_not_redownload_a_complete_directory(monkeypatch, tmp_path):
    """反面：下全了就**不该**再触发下载（否则每次冷启动都要重拉 2 GB）。"""
    _write(tmp_path / "config.json")
    _write(tmp_path / "pytorch_model.bin")
    monkeypatch.setattr(embedding, "model_dir", lambda _name: tmp_path)

    asked: list[str] = []
    monkeypatch.setattr(embedding, "download", lambda name, **_k: asked.append(name))

    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = lambda _path: "loaded"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)

    embedding.load.cache_clear()
    try:
        assert embedding.load("BAAI/bge-m3") == "loaded"
    finally:
        embedding.load.cache_clear()

    assert asked == []


def test_reranker_uses_the_same_readiness_rule(monkeypatch, tmp_path):
    """重排序适配层与嵌入层共用判据——两处各写一份就会再次分叉。"""
    _write(tmp_path / "config.json")
    monkeypatch.setattr(reranker, "model_dir", lambda _name: tmp_path)

    asked: list[str] = []
    monkeypatch.setattr(reranker, "download", lambda name, **_k: asked.append(name))

    fake_module = types.ModuleType("sentence_transformers")
    fake_module.CrossEncoder = lambda _path: "loaded"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)

    reranker.load.cache_clear()
    try:
        assert reranker.load("BAAI/bge-reranker-base") == "loaded"
    finally:
        reranker.load.cache_clear()

    assert asked == ["BAAI/bge-reranker-base"]
