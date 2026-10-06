"""证据装配与上下文预算（`app/modules/answering/assembly.py`，设计 §8.2 第 9 步、§9.4）。

**不连数据库、不加载模型**——装配本来就是纯函数。
"""

from dataclasses import dataclass

from app.modules.answering import assembly


@dataclass(frozen=True)
class _Hit:
    instrument_title: str
    provision_number: str
    provision_display: str | None
    legal_status: str
    text: str


def _hit(article: str, length: int, law: str = "中华人民共和国某法") -> _Hit:
    return _Hit(law, article, f"第{article}条", "effective", "文" * length)


def _assemble(hits, budget_chars: int):
    """把「上下文窗口」直接当字符预算用：指令/问题/输出预留都置 0，只剩 `BUDGET_MARGIN_CHARS`。"""
    return assembly.assemble_evidence(
        hits,
        "",
        instructions_chars=0,
        max_new_tokens=0,
        context_tokens=budget_chars,
    )


def test_everything_fits():
    result = _assemble([_hit("1", 50), _hit("2", 50)], 2000)
    assert result.complete is True
    assert result.notice is None
    assert len(result.included) == 2


def test_oversized_item_is_skipped_whole_and_the_rest_still_fit():
    """**整条进或整条不进**：装不下的整条跳过，但后面的短条目仍有机会装上。"""
    small_a, huge, small_b = _hit("1", 100), _hit("2", 5000), _hit("3", 100)
    result = _assemble([small_a, huge, small_b], 1000)

    assert [item.provision_number for item in result.included] == ["1", "3"]
    assert [item.provision_number for item in result.excluded] == ["2"]
    # 装进去的是**完整条文**，不是截断过的
    assert result.included[0].text == small_a.text
    assert result.notice is not None
    assert "只装入了前 2 条" in result.notice
    assert "第2条" in result.notice


def test_nothing_fits_refuses_instead_of_truncating():
    """一条都装不下就不生成结论（§9.4「证据无法完整装配时……转人工」）。"""
    result = _assemble([_hit("1", 5000)], 1000)
    assert result.included == ()
    assert result.notice == assembly.TOO_LARGE_NOTICE
    assert result.complete is False


def test_tiny_context_window_is_reported_not_crashed():
    """窗口小到连指令与输出都放不下——不是装配能解决的问题，如实报告即可。"""
    result = _assemble([_hit("1", 10)], 10)
    assert result.included == ()
    assert result.notice == assembly.TOO_LARGE_NOTICE


def test_inclusion_order_follows_retrieval_order():
    """命中顺序就是相关性顺序，装的时候不能重排。"""
    hits = [_hit("1", 20), _hit("2", 20), _hit("3", 20)]
    result = _assemble(hits, 1000)
    assert [item.provision_number for item in result.included] == ["1", "2", "3"]


def test_assembled_evidence_actually_fits_the_budget():
    """装配出来的证据块必须真的装得下——这是本模块存在的全部理由。

    ⚠️ 首版 `_cost` 自己写了一份排版，把效力状态那段漏了，于是**估少**了两格；
    估少的方向就是超出窗口被静默截断。现在 `_cost` 直接量 `build_evidence` 的输出。
    """
    from app.modules.answering.evidence import build_evidence

    hits = [_hit("1", 100), _hit("2", 5000), _hit("3", 100)]
    budget = 1000 - assembly.BUDGET_MARGIN_CHARS
    result = assembly.assemble_evidence(
        hits, "", instructions_chars=0, max_new_tokens=0, context_tokens=1000
    )
    assert len(build_evidence(result.included)) <= budget
