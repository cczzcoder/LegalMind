"""送进本地模型的证据与提示（设计 §9.2、§9.5）。

抽成单独模块是为了**一处定义、三处共用**：

- 回答层拼提示（`service.answer_question`）；
- 生成质量评测复用**同一条提示**（`scripts/evaluate_answering.py`）——评测另写一份提示，
  测出来的就不是线上跑的东西了；
- 确定性校验判断「模型说的东西在不在证据里」（`verification`）——它得知道证据是
  **怎么呈现给模型的**（证据编号怎么给、效力状态翻成了什么中文），否则会误判。

**输出形态是结构化的**（§9.2「模型仅输出结构化主张和允许的证据 ID」）：`build_evidence` 给每条
条文编的 `[1]` `[2]` **就是模型要回填的 `evidence_ids`**，所以引用核验从「正则猜哪句是引用」
变成「查 ID 在不在集合里」。证据块的排版与编号因此**不能随便改**——改了模型回填的 ID 就对不上。

⚠️ **指令放在用户消息里，不放 system 角色**——这是实测出来的，不是风格问题：同一份证据、同一个
问题，用 system 角色给指令时 Qwen2.5-1.5B 一律回「依据不足」（它抓住那句退路照抄），并进用户消息
后立刻答对。小模型对 system 角色的指令跟随很脆。**换更大的模型时也建议保持这个形态**，除非重新评测过。
"""

INSTRUCTIONS = (
    "请只依据下面给出的条文原文回答问题，不要使用条文以外的知识。\n"
    "按下面的 JSON 结构输出，不要输出 JSON 以外的任何内容：\n"
    '{"claims":[{"claim_id":"c1","text":"结论，只写条文支持的内容","evidence_ids":["1"],'
    '"conditions":["适用前提"],"limitations":["尚未确认的事项"]}],'
    '"missing_facts":["还缺什么"],"conflicts":[]}\n'
    "要求：\n"
    '- evidence_ids 只能填条文前面的编号（如 "1"、"2"）；**每条主张至少要有一个编号**，'
    "没有条文支持的结论不要写。\n"
    "- 每条主张要**写完整**：把条文里的**主体**（谁）与**适用范围**（对什么情形）带上，"
    "不要只写谓语部分。\n"
    "- 条文没写到的内容不要补，不要给法律意见或推测。\n"
    "- 若条文确实与问题无关，claims 留空数组，并在 missing_facts 里说明还缺什么。\n"
    "- 条文后的括号里是效力状态；其中「施行日期未知」只表示没提取到施行日期，"
    "**不影响你依据条文内容作答**。"
)

# 效力状态要**翻成中文再给模型**：直接把 ``unknown`` 这个英文枚举值塞进提示，实测会让小模型
# 读成「这条不可靠」然后拒答（同一份证据、同一个问题，去掉括号就答对了）。
# 设计 §8.3 要求把效力状态回传给**回答层**（由它决定能不能下结论），不等于要把原始枚举喂给模型。
STATUS_LABELS = {
    "effective": "现行有效",
    "not_yet_effective": "尚未生效，不得作为现行依据",
    "repealed": "已被取代",
    "unknown": "施行日期未知",
}


def build_evidence(hits) -> str:
    """把检索命中拼成证据块。**只放条文原文与出处**，不放别的东西。

    方括号里的序号**就是模型要回填的 `evidence_ids`**（§9.2），所以顺序与格式是契约的一部分。
    """
    blocks = []
    for index, hit in enumerate(hits, start=1):
        status = STATUS_LABELS.get(hit.legal_status, hit.legal_status)
        blocks.append(
            f"[{index}] {hit.instrument_title} {hit.provision_display or hit.provision_number}"
            f"（{status}）\n{hit.text}"
        )
    return "\n\n".join(blocks)


def build_messages(hits, question: str) -> list[dict]:
    """拼出送进本地模型的对话消息。"""
    return [
        {
            "role": "user",
            "content": f"{INSTRUCTIONS}\n\n条文原文：\n\n{build_evidence(hits)}\n\n问题：{question}",
        },
    ]


def evidence_display(hits) -> dict[str, str]:
    """证据编号 → 人类可读的依据标题（`{"1": "《中华人民共和国劳动法》第五十条"}`）。

    **由服务端生成**（§9.2「服务端负责生成引用标题和访问入口，不接受模型自行生成的来源链接」）：
    模型只回填编号，标题一律从这里取，模型写的「依据：《某法》第某条」不进这个映射。
    """
    return {
        str(index): f"《{hit.instrument_title}》{hit.provision_display or hit.provision_number}"
        for index, hit in enumerate(hits, start=1)
    }
