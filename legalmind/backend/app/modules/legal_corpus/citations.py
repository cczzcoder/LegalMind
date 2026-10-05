"""条款之间的引用抽取（设计 §8.4 的「引用」关系）。

图谱增强检索需要条款之间的关系边。**「引用」是唯一能确定性地从正文抽出来的一类**——条文自己写着
「依照本法第八十七条」「本条例第三十六条」，这是**文本事实**而不是模型推断，所以可以直接入库
（§8.4 要求「不得由模型自由生成关系边」）。其余类型——替代 / 改号 / 拆分 / 合并 / 上下位 /
定义与被定义——没有可依赖的来源，仍需人工确认，本模块不碰。

抽取规则（都来自真实语料的实测形态）：

- 「本法第X条」「本条例第X条」→ 同一法律内的引用；
- 「第X条至第Y条」→ 展开成区间内的**每一条**（实测商标法「本法第二十条至第二十二条」）；
- 「第X条、第Y条」→ 逐条；
- 「前条」→ 上一条（用 ``article_number`` 推导）；
- 「<法律简称>第X条」→ 跨法律引用，简称按**唯一包含关系**匹配已入库的法律名称
  （实测「药品管理法第四十二条」能对上《中华人民共和国药品管理法》）。

**窗口必须截断，否则会造出错边**：引用窗口在句末、书名号、以及**另一个法律名**处就停下——
实测「本法第五十条和《药品管理法》第四十二条的规定」里，后一个条号属于别的法律，
不截断就会被算成本法的内部引用。

**宁缺勿错**：不带「本法/本条例」也没有可解析法律名的裸引用不抽；条号规范化不出来不抽。
调用方拿到的是候选边，**目标条款是否真的存在由调用方去库里的 ``provision_identities`` 核对**。
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from app.modules.legal_corpus.structure import normalize_article_number

_WS = re.compile(r"[\s\u3000]+")
_CN = "一二三四五六七八九十百零〇两"
_LOCAL = ("本法", "本条例", "本规定", "本细则", "本实施细则")
_WINDOW_STOP = re.compile(r"[。；;]")
_WINDOW_LIMIT = 60
_REF = re.compile(rf"第([{_CN}0-9]{{1,8}})条(?:之([{_CN}0-9]{{1,3}}))?")
_PREVIOUS = "前条"
_PREFIX = "中华人民共和国"
_TITLE_OPEN = "《"


@dataclass(frozen=True)
class Citation:
    """一条引用。``target`` 是规范化条号（``"87"`` / ``"87之1"``）。

    ``alias`` 为 ``None`` 表示同法内部；否则是引用的法律简称（由调用方解析成具体法律）。
    ``evidence`` 是命中的原文片段，便于人工复核——边既然是从文本抽的，就要能指回文本。
    """

    target: str
    alias: str | None
    evidence: str


def _cut(window: str, markers: Iterable[str]) -> str:
    """把窗口截到句末 / 书名号 / 另一个法律名之前。"""
    window = _WINDOW_STOP.split(window)[0]
    for marker in (_TITLE_OPEN, *markers):
        position = window.find(marker)
        if position != -1:
            window = window[:position]
    return window


def _in_window(window: str, *, alias: str | None, prefix: str) -> list[Citation]:
    """把窗口里的引用读出来，含「第X条至第Y条」的区间展开。"""
    matches = list(_REF.finditer(window))
    results: list[Citation] = []
    index = 0
    while index < len(matches):
        match = matches[index]
        target = normalize_article_number(match.group(0))
        if target is None:
            index += 1
            continue
        following = matches[index + 1] if index + 1 < len(matches) else None
        if following is not None and "至" in window[match.end() : following.start()]:
            end = normalize_article_number(following.group(0))
            span = prefix + window[match.start() : following.end()]
            if end is not None and target.isdigit() and end.isdigit():
                results += [
                    Citation(target=str(number), alias=alias, evidence=span)
                    for number in range(int(target), int(end) + 1)
                ]
                index += 2
                continue
        results.append(Citation(target=target, alias=alias, evidence=prefix + match.group(0)))
        index += 1
    return results


def aliases_of(titles: Iterable[str]) -> dict[str, str]:
    """法律名称 → 引用里可能用的简称。只去掉「中华人民共和国」前缀，不猜别的。"""
    aliases: dict[str, str] = {}
    for title in titles:
        short = title.removeprefix(_PREFIX)
        aliases.setdefault(short, title)
    return aliases


def extract(
    text: str,
    *,
    article_number: str | None = None,
    titles: Iterable[str] = (),
) -> list[Citation]:
    """抽取条款文本里的引用；``article_number`` 用于解析「前条」，``titles`` 是已入库的法律名称。

    ``text`` 可以带空白（PDF 折行空格很常见），内部先去掉——**与写入、检索同一口径**。
    """
    flat = _WS.sub("", text or "")
    if not flat:
        return []

    aliases = aliases_of(titles)
    # 长的简称优先：否则「药品管理法」会先匹配掉「疫苗管理法」的一部分
    by_length = sorted(aliases, key=len, reverse=True)
    found: list[Citation] = []
    seen: set[tuple[str, str | None]] = set()

    def keep(citations: list[Citation]) -> None:
        for citation in citations:
            key = (citation.target, citation.alias)
            if key not in seen:
                seen.add(key)
                found.append(citation)

    for qualifier in _LOCAL:
        start = 0
        while (position := flat.find(qualifier, start)) != -1:
            start = position + len(qualifier)
            window = _cut(flat[start : start + _WINDOW_LIMIT], by_length)
            keep(_in_window(window, alias=None, prefix=qualifier))

    # 「前条」= 上一条；条号是 "87" 或 "87之1"，只有纯数字形态才能减一
    if _PREVIOUS in flat and article_number and article_number.isdigit():
        previous = int(article_number) - 1
        if previous >= 1:
            keep([Citation(target=str(previous), alias=None, evidence=_PREVIOUS)])

    for alias in by_length:
        start = 0
        while (position := flat.find(alias, start)) != -1:
            start = position + len(alias)
            # 同一位置有**更长**的法律名匹配时交给更长的那个：否则「药品管理法」会命中
            # 「药品管理法实施条例」的前几个字，造出一条指向错法律的边。
            # 注意只跟更长的比——把更短的也算进来会把长的那个自己也跳过，两边都不抽。
            if any(
                flat.startswith(other, position) for other in by_length if len(other) > len(alias)
            ):
                continue
            window = _cut(flat[start : start + _WINDOW_LIMIT], by_length)
            keep(_in_window(window, alias=alias, prefix=alias))
    return found
