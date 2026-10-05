"""重排序模型适配层（设计 §8.2 的「重排序」）：纯单元测试，不加载模型。

真正的效果由 `backend/scripts/evaluate_retrieval.py --rerank` 在金标准上衡量——结论见
`doc/技术决策与踩坑记录.md` §1.9。
"""

import pytest

from app.adapters import reranker


def test_default_model_is_the_base_reranker():
    assert reranker.DEFAULT_MODEL == "BAAI/bge-reranker-base"


def test_unknown_model_is_rejected():
    with pytest.raises(ValueError, match="Unknown reranker model"):
        reranker.model("nope/not-a-model")


def test_model_dir_is_derived_from_the_model_name():
    # 与嵌入模型同一套目录规则，仓库名里的斜杠不能变成目录层级
    assert reranker.model_dir("BAAI/bge-reranker-base").name == "BAAI__bge-reranker-base"


def test_scoring_nothing_returns_nothing_without_loading_the_model():
    """空候选集不该触发模型加载——调用方会频繁传空。"""
    assert reranker.score("BAAI/bge-reranker-base", "任意查询", []) == []
