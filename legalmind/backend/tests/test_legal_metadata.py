"""法律元数据提取（设计 §5.1、§5.2、§7、§8.3）：纯单元测试，不需要数据库。

用例取自 16 份真实法律文本的实测形态：修正/修订/通过三类沿革子句、主席令与国务院令、
「自…起施行」的生效日期、PDF 折行把日期与关键词切成两块、印发通知式的标题、以及只能回退
文件名的两种情形。
"""

from datetime import date

import pytest

from app.modules.legal_corpus.metadata import (
    ADMINISTRATIVE_REGULATION,
    CONSTITUTION,
    DEPARTMENT_RULE,
    EFFECTIVE,
    JUDICIAL_INTERPRETATION,
    LAW,
    NOT_YET_EFFECTIVE,
    OTHER,
    REASON_TITLE_FROM_FILENAME,
    REASON_VERSION_FROM_FILENAME,
    REASON_VERSION_UNLABELLED,
    REASON_VERSION_WEAK_DATE,
    UNKNOWN_STATUS,
    extract_metadata,
    filename_title,
    normalize_title,
)

TODAY = date(2026, 10, 3)


def meta(filename: str, *blocks: str, today: date = TODAY):
    return extract_metadata(filename, list(blocks), today=today)


def test_correction_clause_takes_the_last_amendment():
    """多段沿革取最后一个子句：劳动法通过于 1994、2009 与 2018 两次修正，取 2018。"""
    result = meta(
        "中华人民共和国劳动法_20181229.docx",
        "中华人民共和国劳动法",
        "（1994年7月5日第八届全国人民代表大会常务委员会第八次会议通过　"
        "根据2009年8月27日第十一届全国人民代表大会常务委员会第十次会议"
        "《关于修改部分法律的决定》第一次修正　"
        "根据2018年12月29日第十三届全国人民代表大会常务委员会第七次会议"
        "《关于修改〈中华人民共和国劳动法〉的决定》第二次修正）",
    )

    assert result.title == "中华人民共和国劳动法"
    assert result.instrument_type == LAW
    assert result.issuing_body == "全国人民代表大会常务委员会"
    assert result.version_label == "2018年修正"
    assert result.promulgated_on == date(2018, 12, 29)
    # 修正文本没有「自…起施行」子句：生效日期不虚构，效力状态记 unknown
    assert result.effective_from is None
    assert result.legal_status == UNKNOWN_STATUS
    assert result.confident


def test_presidential_order_gives_number_effective_date_and_status():
    result = meta(
        "中华人民共和国律师法（2026年修正）.pdf",
        "中华人民共和国主席令",
        "第八十四号",
        "《全国人民代表大会常务委员会关于修改＜中华人民共和国律师法＞的决定》已由"
        "中华人民共和国第十四届全国人民代表大会常务委员会第二十四次会议于2026年8月28日"
        "通过，现予公布，自2026年9月1日起施行。",
        "中华人民共和国主席习近平",
        "2026年8月28日",
        "中华人民共和国律师法",
        "（1996年5月15日第八届全国人民代表大会常务委员会第十九次会议通过"
        "根据2026年8月28日第十四届全国人民代表大会常务委员会第二十四次会议"
        "《关于修改〈中华人民共和国律师法〉的决定》修正）",
    )

    assert result.title == "中华人民共和国律师法"
    assert result.document_number == "中华人民共和国主席令第八十四号"
    assert result.version_label == "2026年修正"
    assert result.promulgated_on == date(2026, 8, 28)
    assert result.effective_from == date(2026, 9, 1)
    # 今日 2026-10-03 已过生效日
    assert result.legal_status == EFFECTIVE
    assert result.confident


def test_future_effective_date_is_not_yet_effective():
    result = meta(
        "中华人民共和国医疗保障法（2026年）.pdf",
        "中华人民共和国主席令",
        "第八十号",
        "《中华人民共和国医疗保障法》已由中华人民共和国第十四届全国人民代表大会常务委员会"
        "第二十四次会议于2026年8月28日通过，现予公布，自2027年1月1日起施行。",
        "中华人民共和国医疗保障法",
        "（2026年8月28日第十四届全国人民代表大会常务委员会第二十四次会议通过）",
    )

    assert result.version_label == "2026年"
    assert result.effective_from == date(2027, 1, 1)
    assert result.legal_status == NOT_YET_EFFECTIVE


def test_pdf_line_wrapping_does_not_split_the_date():
    """PDF 折行把「2026年」与「6月26日」切成两块，提取须在去空白文本上进行。"""
    result = meta(
        "中华人民共和国商标法（2026年修订）.pdf",
        "中华人民共和国商标法",
        "（1982年8月23日第五届全国人民代表大会常务委员会第二十四次会",
        "议通过根据1993年2月22日第七届全国人民代表大会常务委员会第三",
        "十次会议《关于修改〈中华人民共和国商标法〉的决定》第一次修正根据2019年",
        "4月23日第十三届全国人民代表大会常务委员会第十次会议《关于修",
        "改〈中华人民共和国建筑法〉等八部法律的决定》第四次修正2026年",
        "6月26日第十四届全国人民代表大会常务委员会第二十三次会议修订",
        "）",
    )

    assert result.version_label == "2026年修订"
    assert result.promulgated_on == date(2026, 6, 26)
    assert result.confident


def test_state_council_order_uses_the_latest_number():
    result = meta(
        "中华人民共和国药品管理法实施条例_20260116.docx",
        "中华人民共和国药品管理法实施条例",
        "（2002年8月4日中华人民共和国国务院令第360号公布　"
        "根据2016年2月6日《国务院关于修改部分行政法规的决定》第一次修订　"
        "根据2019年3月2日《国务院关于修改部分行政法规的决定》第二次修订　"
        "根据2024年12月6日《国务院关于修改部分行政法规的决定》第三次修订　"
        "根据2026年1月16日中华人民共和国国务院令第828号第四次修订）",
    )

    assert result.instrument_type == ADMINISTRATIVE_REGULATION
    assert result.issuing_body == "国务院"
    assert result.document_number == "国务院令第828号"
    assert result.version_label == "2026年修订"


def test_title_from_book_quotes_in_a_printed_notice():
    """印发通知式文件：标题藏在《》里，制定机关是通知的落款单位。"""
    result = meta(
        "人工智能科技伦理审查与服务办法（试行）.pdf",
        "工业和信息化部等十部门关于印发《人工智能科技伦理审查与服务办法（试行）》的通知",
        "2026年3月20日",
    )

    assert result.title == "人工智能科技伦理审查与服务办法（试行）"
    assert result.instrument_type == DEPARTMENT_RULE
    assert result.issuing_body == "工业和信息化部等十部门"
    # 没有通过/修正/修订子句，只能取前言末个日期：弱证据，交人工审核
    assert result.version_label == "2026年"
    assert result.low_confidence_reasons == (REASON_VERSION_WEAK_DATE,)
    assert not result.confident


def test_version_falls_back_to_filename_date():
    result = meta(
        "中华人民共和国矿产资源法实施条例_20260515.docx",
        "中华人民共和国矿产资源法实施条例",
    )

    assert result.title == "中华人民共和国矿产资源法实施条例"
    assert result.version_label == "2026-05-15"
    assert result.promulgated_on == date(2026, 5, 15)
    assert result.low_confidence_reasons == (REASON_VERSION_FROM_FILENAME,)
    assert not result.confident


def test_unlabelled_version_when_nothing_is_available():
    result = meta("中华人民共和国示例办法.docx", "中华人民共和国示例办法")

    assert result.version_label == "未标注版本"
    assert result.promulgated_on is None
    assert result.low_confidence_reasons == (REASON_VERSION_UNLABELLED,)
    assert not result.confident


def test_title_falls_back_to_filename_when_preamble_has_none():
    result = meta(
        "中华人民共和国示例法_20260101.docx",
        "（2026年1月1日第十四届全国人民代表大会常务委员会第一次会议通过）",
    )

    assert result.title == "中华人民共和国示例法"
    assert REASON_TITLE_FROM_FILENAME in result.low_confidence_reasons
    assert not result.confident


@pytest.mark.parametrize(
    ("filename", "title", "expected"),
    [
        ("中华人民共和国宪法（2018年修正）.pdf", "中华人民共和国宪法", CONSTITUTION),
        ("中华人民共和国生态环境法典_20260312.docx", "中华人民共和国生态环境法典", LAW),
        ("中华人民共和国商标法_20260626.docx", "中华人民共和国商标法", LAW),
        (
            "中华人民共和国矿产资源法实施条例_20260515.docx",
            "中华人民共和国矿产资源法实施条例",
            ADMINISTRATIVE_REGULATION,
        ),
        (
            "人工智能科技伦理审查与服务办法（试行）.pdf",
            "人工智能科技伦理审查与服务办法（试行）",
            DEPARTMENT_RULE,
        ),
        (
            "最高人民法院关于审理示例案件的解释.txt",
            "最高人民法院关于审理示例案件的解释",
            JUDICIAL_INTERPRETATION,
        ),
        ("某某工作规程.docx", "某某工作规程", OTHER),
    ],
)
def test_instrument_type_inference(filename, title, expected):
    result = meta(filename, title)
    assert result.title == title
    assert result.instrument_type == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("《中华人民共和国商标法》", "中华人民共和国商标法"),
        ("中华人民共和国宪法（2018年修正文本）", "中华人民共和国宪法"),
        ("中华人民共和国医疗保障法（2026年）", "中华人民共和国医疗保障法"),
        # 「（试行）」是名称的一部分，不能剥离
        ("人工智能科技伦理审查与服务办法（试行）", "人工智能科技伦理审查与服务办法（试行）"),
        ("中华人民共和国　劳动法", "中华人民共和国劳动法"),
    ],
)
def test_normalize_title(raw, expected):
    assert normalize_title(raw) == expected


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("中华人民共和国劳动法_20181229.docx", "中华人民共和国劳动法"),
        ("中华人民共和国商标法（2026年修订）.pdf", "中华人民共和国商标法"),
        ("中华人民共和国宪法（2018年修正文本）_20180311.docx", "中华人民共和国宪法"),
        ("人工智能科技伦理审查与服务办法（试行）.pdf", "人工智能科技伦理审查与服务办法（试行）"),
    ],
)
def test_filename_title(filename, expected):
    assert filename_title(filename) == expected


def test_jurisdiction_defaults_to_china():
    result = meta("中华人民共和国商标法_20260626.docx", "中华人民共和国商标法")
    assert result.jurisdiction == "中国"
