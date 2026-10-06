"""证据装配与上下文预算（设计 §8.2 第 8、9 步、§9.4）。

**为什么需要**：检索只限制了**条数**（`limit`），没有限制**长度**——5 条条文可能是 500 字，也可能是
5000 字。装不下模型窗口时，推理引擎不会报错，而是**静默从前面截断**；而指令就在提示的最前面
（`evidence.build_messages` 把它们放进同一条用户消息），于是**模型先失去的是全部约束**，然后才轮到
最早的条文。设计 §9.4 写得很直接：「法律原文不得因上下文或资源限制被无提示截断；证据无法完整装配时，
限制回答范围或转人工」。所以这一层要自己算预算、自己如实报告。

**两条装配规则**：

1. **整条进或整条不进**——绝不把一条法律条文截一半。截一半的条文读起来仍像完整的，比不装更危险。
2. **装不下就如实说**：全部装不下 → 不生成结论（转人工）；只装下一部分 → 出结论，但**明确限定
   回答范围**并列出没装进去的是哪几条（`Assembly.notice`）。

**预算是估算，不是精确值**：本项目不持有模型的 tokenizer（推理在 Ollama 里），所以按**字符**估。
`CHAR_PER_TOKEN` 取 1（中文大致 1 字 ≈ 0.6–1 token），**宁可估多**——估多了只是少装一条，估少了会
触发上面那个静默截断。换模型后这个系数要按实际重新核。
"""

from dataclasses import dataclass

from app.modules.answering.evidence import build_evidence

#: 1 个 token 按几个字符估。**保守取 1**（中文大致 1 字 ≈ 0.6–1 token）。
CHAR_PER_TOKEN = 1
#: 预算里额外留的余量（字符）：证据块的拼接分隔、以及估算误差。
BUDGET_MARGIN_CHARS = 256

#: 一条依据都装不下时**不生成结论**（§9.4「证据无法完整装配时……转人工」）。
TOO_LARGE_NOTICE = (
    "检索到的依据超出本地生成模型的上下文预算，无法完整装配，未生成结论。"
    "请缩小问题范围，或转人工核对原始条文。"
)
#: 只装下一部分时的确定性提示——**限定回答范围**，并点名没装进去的是哪几条。
PARTIAL_NOTICE = (
    "依据过多，本次只装入了前 {included} 条（共检索到 {total} 条）；未装入：{excluded}。"
    "以下结论**只覆盖装入的依据**，未装入部分请另行核对或转人工。"
)


@dataclass(frozen=True)
class Assembly:
    """一次证据装配的结果。"""

    included: tuple
    excluded: tuple
    notice: str | None

    @property
    def complete(self) -> bool:
        return not self.excluded


def _cost(item) -> int:
    """一条依据在提示里占的字符数。

    **直接量 `build_evidence` 的输出**，不在这里另写一份排版——两处各写一份迟早分叉，
    而分叉的方向若是「估少了」，后果就是超出窗口、被推理引擎静默截断（本模块存在的理由）。
    """
    return len(build_evidence([item])) + 2  # +2 是块之间的空行


def assemble_evidence(
    hits,
    question: str,
    *,
    instructions_chars: int,
    max_new_tokens: int,
    context_tokens: int,
) -> Assembly:
    """按上下文预算把检索命中装成证据集。

    预算 = 模型窗口 − 指令 − 问题 − 输出预留 − 余量。命中按传入顺序（检索相关性）依次装入，
    **遇到装不下的整条跳过**（后面的更短条目仍有机会装入）。
    """
    budget = (
        context_tokens * CHAR_PER_TOKEN
        - instructions_chars
        - len(question or "")
        - max_new_tokens
        - BUDGET_MARGIN_CHARS
    )
    items = list(hits)
    if budget <= 0:
        # 窗口小到连指令与输出都放不下——不是装配能解决的问题，如实报告
        return Assembly(included=(), excluded=tuple(items), notice=TOO_LARGE_NOTICE)

    included: list = []
    excluded: list = []
    used = 0
    for item in items:
        cost = _cost(item)
        if used + cost <= budget:
            included.append(item)
            used += cost
        else:
            excluded.append(item)

    if not included and items:
        return Assembly(included=(), excluded=tuple(items), notice=TOO_LARGE_NOTICE)
    if not excluded:
        return Assembly(included=tuple(included), excluded=(), notice=None)

    detail = "、".join(
        f"{item.instrument_title}{item.provision_display or item.provision_number}"
        for item in excluded
    )
    return Assembly(
        included=tuple(included),
        excluded=tuple(excluded),
        notice=PARTIAL_NOTICE.format(included=len(included), total=len(items), excluded=detail),
    )
