"""送进本地模型的证据与提示（设计 §9.2、§9.5）。

抽成单独模块是为了**一处定义、三处共用**：

- 回答层拼提示（`service.answer_question`）；
- 生成质量评测复用**同一条提示**（`scripts/evaluate_answering.py`）——评测另写一份提示，
  测出来的就不是线上跑的东西了；
- 确定性校验判断「模型说的东西在不在证据里」（`verification.verify`）——它得知道证据是
  **怎么呈现给模型的**（效力状态翻成了什么中文、条号显示成什么形态），否则会误判。
"""

# ⚠️ **指令放在用户消息里，不放 system 角色**——这是实测出来的，不是风格问题。
#
# 同一份证据、同一个问题，用 system 角色给指令时 Qwen2.5-1.5B 一律回「依据不足」（它抓住了
# 那句退路照抄），并进用户消息后立刻答对。小模型对 system 角色的指令跟随很脆，把拒绝条款单独
# 放进 system 尤其容易触发。**换更大的模型时也建议保持这个形态**，除非重新评测过。
INSTRUCTIONS = (
    "请只依据下面给出的条文原文回答问题，不要使用条文以外的知识，并在结论后标注依据的条号"
    "（格式如：《中华人民共和国监狱法》第五十条）。\n"
    "只陈述条文写了什么，不要给出法律意见或推测。\n"
    "条文后的括号里是效力状态；其中「施行日期未知」只表示没提取到施行日期，"
    "**不影响你依据条文内容作答**。\n"
    "只有当条文确实与问题无关时，才回答「依据不足」，并说明还缺什么。"
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
    """把检索命中拼成证据块。**只放条文原文与出处**，不放别的东西。"""
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
