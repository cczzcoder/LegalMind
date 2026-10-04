"""查询改写（设计 §8.2 第 2 步「查询解析与改写」）。

用户不会按法条的措辞提问：他问「单位欠缴社保费会被怎么处理？」，法条写的是「用人单位无故不缴纳
社会保险费的」。改写就是把前者往后者推。分三层，**逐层可单独使用**，便于在金标准上做消融：

1. **规范化**：去空白、全角转半角、去掉问号感叹号——与写入侧同口径，任何查询都该先过这一层。
2. **剥离疑问与口语框架**：去掉「请问 / 怎么办 / 会被怎么处理 / 是否 / 哪些」这类框架词，只留
   实词。**纯规则、无词典**，因此不会过拟合到某一份金标准。
3. **同义与简称替换**：法律领域常见的简称↔全称（社保↔社会保险、低保↔最低生活保障…）。
   这一层**是人工维护的词典**，必然不完整；它的收益必须单独度量，不能和第 2 层混着看。

**改写不替代原查询**：``Rewrite.variants()`` 同时给出原查询与各层结果，调用方取**并集**——
于是改写只可能提高召回，不会因为改错而降低召回。
"""

import re
from dataclasses import dataclass

_WS = re.compile(r"[\s\u3000]+")
# 全角 ASCII（！到～）转半角，其余全角标点单独处理
_FULLWIDTH = {chr(code): chr(code - 0xFEE0) for code in range(0xFF01, 0xFF5F)}
_PUNCT = re.compile(r"[？?！!。，,；;：:、]+")

# 第 2 层：疑问与口语框架词。**纯规则**——不依赖任何词典，也就不会针对某份金标准调。
# 顺序有讲究：**长的框架词必须排在短的之前**（先删掉「会被怎么处理」，才不会只剩「会被」）。
_FRAMES = (
    "会被怎么处理",
    "会怎么处理",
    "会被如何",
    "我想知道",
    "麻烦问一下",
    "请问",
    "会被",
    "怎么办",
    "怎么处理",
    "怎么办理",
    "如何处理",
    "怎么算",
    "怎么认定",
    "谁来负责",
    "由谁负责",
    "谁负责",
    "由谁办理",
    "需要什么",
    "有哪些",
    "是什么",
    "属于什么",
    "算不算",
    "可以吗",
    "可不可以",
    "能不能",
    "行不行",
    "会不会",
    "要不要",
    "是否",
    "怎么",
    "如何",
    "哪些",
    "什么",
)

# 第 3 层：法律领域的常见简称与口语说法 → 规范用词。**人工维护、必然不完整**。
# 只收「口语/简称 → 法律用词」这类真正需要翻译的，同义反复的（如「劳动合同」→「劳动合同」）不收。
# 收录口径是通用法律与行政词汇，**没有照着金标准挑词**；但它与金标准出自同一作者，
# 收益里有一部分可能来自这种重叠，报告里要如实说明。
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "社保": ("社会保险",),
    "医保": ("基本医疗保险",),
    "个税": ("个人所得税",),
    "低保": ("最低生活保障",),
    "五保": ("特困人员供养",),
    "公积金": ("住房公积金",),
    "老板": ("用人单位",),
    "员工": ("劳动者",),
    "欠薪": ("拖欠工资",),
    "欠缴": ("不缴纳", "拖欠"),
    "工伤": ("因工负伤",),
    "驾照": ("机动车驾驶证",),
    "身份证": ("居民身份证",),
    "注会": ("注册会计师",),
    "打官司": ("诉讼",),
    "坐牢": ("有期徒刑",),
    "探矿": ("探矿权",),
    "采矿": ("采矿权",),
    "网约车": ("网络预约出租汽车",),
    "无人机": ("民用无人驾驶航空器",),
}


@dataclass(frozen=True)
class Rewrite:
    """一次改写的各层结果。``variants()`` 是拿去检索的候选集合（含原查询）。"""

    original: str
    normalized: str
    stripped: str
    expanded: tuple[str, ...]

    def variants(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([self.original, self.normalized, self.stripped, *self.expanded]))


def normalize(text: str) -> str:
    """第 1 层：去空白、全角转半角、去标点。与写入侧的规范化同口径。"""
    halfwidth = "".join(_FULLWIDTH.get(char, char) for char in text)
    return _PUNCT.sub("", _WS.sub("", halfwidth))


def strip_frames(text: str) -> str:
    """第 2 层：去掉疑问与口语框架词，只留实词。"""
    result = text
    for frame in _FRAMES:
        result = result.replace(frame, " ")
    return _WS.sub(" ", result).strip()


def expand(text: str) -> tuple[str, ...]:
    """第 3 层：把命中的简称/口语词换成规范用词，**一次换一处**，产出多个候选。

    一次换一处（而不是笛卡尔积）是为了让候选数随命中数线性增长——查询里出现 3 个简称时
    组合起来会有 8 个变体，收益却主要来自单点替换。
    """
    variants: list[str] = []
    for colloquial, formal_terms in _SYNONYMS.items():
        if colloquial not in text:
            continue
        for formal in formal_terms:
            variants.append(text.replace(colloquial, formal))
    return tuple(dict.fromkeys(variants))


def rewrite(query: str) -> Rewrite:
    normalized = normalize(query)
    stripped = strip_frames(normalized)
    expanded: list[str] = []
    for base in (normalized, stripped):
        if base:
            expanded.extend(expand(base))
    return Rewrite(
        original=query,
        normalized=normalized,
        stripped=stripped,
        expanded=tuple(dict.fromkeys(expanded)),
    )


def terms(query: str) -> list[str]:
    """把改写后的查询拆成检索词——按空白切。

    ``strip_frames`` 与 ``expand`` 都会留下空格，所以这里只要按空白切即可；没有空格的
    连续中文仍然是一个词（中文不分词是本项目既定的取舍，见 keyword.py）。
    """
    return [item for item in _WS.split(query) if item]
