"""本地生成适配层（`app/adapters/generation.py`）：**「探测得到」≠「用得了」**。

不联网、不加载模型——只钉住「传输失败必须被包装成 `GenerationUnavailable`」这一条。它是 §9.5
「模型不可用就如实降级、不回落外部服务」在**调用处**的兜底：`available()` 只查 `/api/tags`，
而 Ollama 在 runner 缺失时**照样返回模型列表**（实测 `llama-server binary not found`、调用统一
500，`ollama list` 与 `/api/tags` 却完全正常）。
"""

import json
import urllib.error

import pytest

from app.adapters import generation


def test_transport_failure_becomes_generation_unavailable(monkeypatch):
    """Ollama 回 500（runner 缺失时的典型表现）→ 包装成 `GenerationUnavailable`，不是裸的 HTTPError。"""

    def boom(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "http://127.0.0.1:11434/api/chat", 500, "Server Error", {}, None
        )

    monkeypatch.setattr(generation, "_post", boom)
    with pytest.raises(generation.GenerationUnavailable):
        generation.generate("legalmind-qwen2.5-7b", [{"role": "user", "content": "在吗"}])


def test_connection_refused_also_becomes_generation_unavailable(monkeypatch):
    """服务没起来（连接被拒）走同一条路——调用方只需要认一个异常类型。"""

    def boom(*_args, **_kwargs):
        raise urllib.error.URLError(ConnectionRefusedError(10061, "拒绝连接"))

    monkeypatch.setattr(generation, "_post", boom)
    with pytest.raises(generation.GenerationUnavailable):
        generation.generate("legalmind-qwen2.5-7b", [{"role": "user", "content": "在吗"}])


def test_a_non_json_body_also_becomes_generation_unavailable(monkeypatch):
    """反代/网关回一段 HTML 时，`json.loads` 的失败也要收住，不能漏出去变成 500。"""

    def boom(*_args, **_kwargs):
        raise json.JSONDecodeError("Expecting value", "<html>", 0)

    monkeypatch.setattr(generation, "_post", boom)
    with pytest.raises(generation.GenerationUnavailable):
        generation.generate("legalmind-qwen2.5-7b", [{"role": "user", "content": "在吗"}])


def test_successful_generation_is_unchanged(monkeypatch):
    """正常路径不能被这次改动碰到（内容照旧去空白）。"""
    monkeypatch.setattr(generation, "_post", lambda *_a, **_k: {"message": {"content": "  结论  "}})
    assert generation.generate("m", [{"role": "user", "content": "q"}]) == "结论"


def test_schema_is_sent_as_a_full_schema_not_a_plain_json_hint(monkeypatch):
    """§9.2：必须传**完整 JSON Schema**（约束解码），不能只用 `format: "json"` 这个软约束。"""
    captured: dict = {}

    def capture(_path, payload, *, timeout):
        captured.update(payload)
        return {"message": {"content": "{}"}}

    monkeypatch.setattr(generation, "_post", capture)
    schema = {"type": "object", "properties": {"claims": {"type": "array"}}}
    generation.generate("m", [{"role": "user", "content": "q"}], schema=schema)

    assert captured["format"] == schema
    assert captured["options"]["temperature"] == 0
