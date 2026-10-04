"""多原件择优的排序键（设计 §7、§8.3）：纯单元测试，不需要数据库。

排序规则是「效力状态 > 公布日期 > docx 优于 pdf > 导入时间」。**格式必须排在导入时间之前**
——同一来源对同一版本同时给出 docx 与 pdf 时，解析准确性更高的 docx 要稳定胜出，不能因为
pdf 后导入就翻盘。这条曾经不成立（导入时间排在格式之前，语料里 PDF 因此当上了主原件），
``test_docx_beats_a_later_imported_pdf`` 就是那条回归。
"""

from datetime import UTC, date, datetime

from app.modules.legal_corpus.service import _rank

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF = "application/pdf"

EARLY = datetime(2026, 1, 1, tzinfo=UTC)
LATE = datetime(2026, 6, 1, tzinfo=UTC)


def test_docx_beats_a_later_imported_pdf():
    """核心回归：pdf 后导入也不能翻盘。"""
    docx = _rank("effective", date(2026, 1, 1), DOCX, EARLY)
    pdf = _rank("effective", date(2026, 1, 1), PDF, LATE)
    assert docx < pdf


def test_status_dominates_format_and_import_time():
    """现行有效优先于已公布未生效，与格式和导入时间无关。"""
    effective_pdf = _rank("effective", None, PDF, EARLY)
    not_yet_docx = _rank("not_yet_effective", date(2030, 1, 1), DOCX, LATE)
    assert effective_pdf < not_yet_docx


def test_publication_date_dominates_format():
    """更晚公布的版本优先，即便它只是 pdf。"""
    newer_pdf = _rank("effective", date(2026, 5, 1), PDF, EARLY)
    older_docx = _rank("effective", date(2020, 5, 1), DOCX, LATE)
    assert newer_pdf < older_docx


def test_known_date_beats_unknown_date():
    known = _rank("effective", date(2026, 1, 1), PDF, EARLY)
    unknown = _rank("effective", None, DOCX, LATE)
    assert known < unknown


def test_import_time_breaks_ties_within_the_same_format():
    """同格式之间才轮到导入时间：重新导入的更正版胜出。"""
    later = _rank("effective", date(2026, 1, 1), PDF, LATE)
    earlier = _rank("effective", date(2026, 1, 1), PDF, EARLY)
    assert later < earlier


def test_repealed_sorts_before_unknown():
    repealed = _rank("repealed", None, PDF, EARLY)
    unknown = _rank("unknown", None, PDF, EARLY)
    assert repealed < unknown


def test_unranked_media_type_sorts_after_pdf():
    pdf = _rank("effective", None, PDF, EARLY)
    plain_text = _rank("effective", None, "text/plain", EARLY)
    assert pdf < plain_text
