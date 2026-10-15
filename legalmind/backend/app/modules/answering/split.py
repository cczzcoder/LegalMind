"""§9.1 之前的一层：把「一次问了好几个问题」拆成独立问题。

⚠️ **必须在规范化之前拆**：`rewrite.normalize` 会把标点全部去掉，
`社保怎么缴？工资怎么发？` 走到那一步就成了 `社保怎么缴工资怎么发`——标点没了，想拆也拆不了。
（实测：不拆的话检索词会变成 `['社保', '缴工资', '发']`，其中 `缴工资` 正是标点被抹掉后
把「缴」和「工资」粘在一起产生的废词。）

⚠️ **只在问号 / 分号处拆，绝不在逗号处拆**：`用人单位无故不缴纳社会保险费，会被怎么处理？`
是**一个**问题，在逗号处拆开就碎了。

⚠️ 这一层目前**只是一个纯函数**，还没有接进 `CREATED` 之后的执行路径——
拆分本身对不对要先用测试钉住，再决定拆完是「各自独立跑」还是「走澄清问用户先答哪个」。
"""

import re

from app.modules.answering.clarify import MIN_SUBSTANCE_CHARS, substance

#: 分隔符：句末问号与分号。**不含逗号、顿号**——那是句内停顿，不是问题边界。
_SEPARATORS = re.compile(r"[？?；;]+")


def split_questions(question: str) -> list[str]:
    """把一次提问拆成若干**独立问题**。

    - 只有一个问题时**原样返回**（长度 1），调用方不必判断「有没有拆过」；
    - 拆出的片段要过一遍「有没有实质内容」的判据（复用澄清分支的 `substance`）：
      `怎么办？然后呢？` 的第二段剥完是空的，**不算一个独立问题**，直接丢掉——
      否则会把用户根本没问的东西当成一个问题拿去检索。

    ⚠️ 只在**问号 / 分号**处拆（理由见模块 docstring）。没有这些分隔符时**一律不拆**，
    哪怕读起来像两个问题——**宁可漏拆，也不要拆错**：拆错了用户要面对两个都不是他问的问题。
    """
    text = (question or "").strip()
    if not text:
        return []
    parts = [item.strip() for item in _SEPARATORS.split(text)]
    kept = [item for item in parts if len(substance(item)) >= MIN_SUBSTANCE_CHARS]
    if len(kept) <= 1:
        return [text]
    return kept
