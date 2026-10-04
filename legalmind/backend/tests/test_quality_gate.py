"""入库质量门禁的判定（设计 §7、§8.3、§20.3）：纯单元测试，不需要数据库。

``assess`` 把「来源登记、解析产物、版本挂接」当作硬要求，把「审核状态、版本标识、条款数」
当作降级项。这组测试把三态与每个原因码都钉住——门禁放宽会让不该进入证据范围的数据溜过去。
"""

from app.modules.legal_corpus.metadata import UNLABELLED_VERSION
from app.modules.legal_corpus.quality import (
    APPROVED,
    DEGRADED,
    FAILED,
    PASSED,
    REASON_LICENSE_MISSING,
    REASON_NO_PROVISIONS,
    REASON_NOT_LANDED,
    REASON_PARSE_EMPTY,
    REASON_PARSE_MISSING,
    REASON_REVIEW_PENDING,
    REASON_SOURCE_MISSING,
    REASON_VERSION_UNLABELLED,
    ArtifactFacts,
    Verdict,
    assess,
)


def _facts(**overrides) -> ArtifactFacts:
    base = {
        "filename": "中华人民共和国示例法_20260101.docx",
        "media_type": "application/pdf",
        "source_name": "国家法律法规数据库",
        "license_note": "官方渠道公开发布",
        "chunk_count": 3,
        "landed": True,
        "version_label": "2026年",
        "review_status": APPROVED,
        "provision_count": 3,
    }
    return ArtifactFacts(**{**base, **overrides})


def test_high_confidence_artifact_passes():
    assert assess(_facts()) == Verdict(status=PASSED, reasons=())


def test_missing_source_registration_fails():
    verdict = assess(_facts(source_name=None, license_note=None))
    assert verdict.status == FAILED
    assert REASON_SOURCE_MISSING in verdict.reasons


def test_empty_license_note_fails():
    # §20.3：空说明与没有说明等价；空白（含全角空格）也算空
    for note in ("", "   ", "\u3000"):
        verdict = assess(_facts(license_note=note))
        assert verdict.status == FAILED
        assert REASON_LICENSE_MISSING in verdict.reasons


def test_artifact_without_parse_revision_fails():
    verdict = assess(_facts(chunk_count=None))
    assert verdict.status == FAILED
    assert REASON_PARSE_MISSING in verdict.reasons


def test_artifact_parsed_to_nothing_fails():
    # 纯图像扫描件解析出 0 字符：进了库也没有可用证据
    verdict = assess(_facts(chunk_count=0))
    assert verdict.status == FAILED
    assert REASON_PARSE_EMPTY in verdict.reasons


def test_artifact_not_linked_to_a_version_fails():
    verdict = assess(
        _facts(landed=False, version_label=None, review_status=None, provision_count=0)
    )
    assert verdict.status == FAILED
    assert REASON_NOT_LANDED in verdict.reasons


def test_pending_review_is_degraded():
    verdict = assess(_facts(review_status="pending"))
    assert verdict.status == DEGRADED
    assert verdict.reasons == (REASON_REVIEW_PENDING,)


def test_unlabelled_version_is_degraded():
    verdict = assess(_facts(version_label=UNLABELLED_VERSION))
    assert verdict.status == DEGRADED
    assert REASON_VERSION_UNLABELLED in verdict.reasons


def test_version_without_provisions_is_degraded():
    verdict = assess(_facts(provision_count=0))
    assert verdict.status == DEGRADED
    assert REASON_NO_PROVISIONS in verdict.reasons


def test_degraded_reasons_accumulate():
    verdict = assess(_facts(review_status="pending", provision_count=0))
    assert verdict.status == DEGRADED
    assert set(verdict.reasons) == {REASON_REVIEW_PENDING, REASON_NO_PROVISIONS}


def test_hard_failure_hides_degraded_reasons():
    # 硬要求不过时不再看降级项：failed 的原因要能一眼看清，不混入次要信息
    verdict = assess(_facts(source_name=None, review_status="pending", provision_count=0))
    assert verdict == Verdict(status=FAILED, reasons=(REASON_SOURCE_MISSING,))


def test_failed_verdict_is_not_ok():
    assert assess(_facts(chunk_count=0)).ok is False
    assert assess(_facts()).ok is True
