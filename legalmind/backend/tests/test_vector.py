"""向量通路与本地嵌入模型（设计 §8.2、§8.3）：纯单元测试，不需要数据库、不加载模型。

只覆盖**能离线验证**的部分：模型注册表、权重目录、向量语句的形状。真正的检索质量由
``backend/scripts/evaluate_retrieval.py`` 在金标准上衡量——那需要真模型和真库。
"""

from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.adapters import embedding
from app.core.security import Principal
from app.modules.retrieval import vector


def _principal() -> Principal:
    return Principal(organization_id=uuid4(), user_id=uuid4(), roles=frozenset())


def test_default_model_is_bge_m3():
    assert embedding.DEFAULT_MODEL == "BAAI/bge-m3"


def test_registry_matches_the_documented_model_sizes():
    """维度和位置上限是选型依据，写错会让 ``provision_embeddings`` 存进对不上的向量。"""
    bge = embedding.model("BAAI/bge-m3")
    assert (bge.dimensions, bge.max_tokens) == (1024, 8192)
    text2vec = embedding.model("shibing624/text2vec-base-chinese")
    assert (text2vec.dimensions, text2vec.max_tokens) == (768, 512)


def test_unknown_model_is_rejected():
    with pytest.raises(ValueError, match="Unknown embedding model"):
        embedding.model("nope/not-a-model")


def test_model_dir_is_derived_from_the_model_name():
    # 仓库名里的斜杠不能变成目录层级，否则权重会散在两个目录里
    assert embedding.model_dir("BAAI/bge-m3").name == "BAAI__bge-m3"


def test_vector_statement_orders_by_cosine_distance():
    statement = vector.statement(
        model="BAAI/bge-m3",
        query_vector=[0.0] * 1024,
        principal=_principal(),
        limit=10,
    )
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "<=>" in sql
    assert "provision_embeddings" in sql
    assert "ORDER BY" in sql.upper()


def test_vector_statement_applies_the_distance_threshold():
    """没有阈值就永远不会「查无结果」——向量检索总要有个下限。"""
    with_threshold = str(
        vector.statement(
            model="BAAI/bge-m3",
            query_vector=[0.0] * 1024,
            principal=_principal(),
            limit=10,
            max_distance=0.6,
        ).compile(dialect=postgresql.dialect())
    )
    without = str(
        vector.statement(
            model="BAAI/bge-m3",
            query_vector=[0.0] * 1024,
            principal=_principal(),
            limit=10,
            max_distance=None,
        ).compile(dialect=postgresql.dialect())
    )
    assert "<= $" in with_threshold or "<=" in with_threshold
    assert "<=" not in without.split("ORDER BY")[0].split("WHERE")[-1]
