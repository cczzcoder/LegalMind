"""生成质量评测的纯逻辑（设计 §9.5）。

**不加载生成模型、不连数据库**——`app/modules/evaluation/scoring.py` 就是为可测而抽出来的。
模型跑得怎么样由 `scripts/evaluate_answering.py` 报告；这里钉的是「打分本身对不对」，
以及**金标准文件本身合不合法**（它进了版本库，改坏了 CI 要拦得住）。
"""

import json
from pathlib import Path

from app.modules.evaluation import scoring

DATASET = (
    Path(__file__).resolve().parents[2] / "evaluations" / "datasets" / "generation_quality.json"
)


def _evidence(*items) -> list[dict]:
    return [
        {"law": law, "article": article, "text": text, "legal_status": status}
        for law, article, text, status in items
    ]


def test_extract_article_numbers_handles_both_notations():
    """模型可能写中文数字也可能写阿拉伯数字，两种都得认。"""
    assert scoring.extract_article_numbers("见《中华人民共和国劳动法》第一百条。") == ["100"]
    assert scoring.extract_article_numbers("见《中华人民共和国劳动法》第100条。") == ["100"]
    assert scoring.extract_article_numbers("第八十七条之一") == ["87之1"]
    # 「第一款」不是条号——抽出来会变成凭空引用
    assert scoring.extract_article_numbers("依据第一款、第二款") == []


def test_normalize_for_match_drops_folded_whitespace():
    """PDF 折行空格（实测「民用航 空器」）不处理掉，逐字子串比对就会漏。"""
    assert scoring.normalize_for_match("民用航 空器") == scoring.normalize_for_match("民用航空器")
    assert scoring.normalize_for_match("第五十条\u3000工资") == "第五十条工资"


def test_score_case_grounded_answer_meets_expectations():
    case = {
        "id": "g",
        "category": "grounded",
        "expect_citations": [["中华人民共和国劳动法", "50"]],
        "required_facts": ["工资应当以货币形式按月支付给劳动者本人"],
    }
    evidence = _evidence(
        (
            "中华人民共和国劳动法",
            "50",
            "第五十条　工资应当以货币形式按月支付给劳动者本人。",
            "unknown",
        ),
        ("中华人民共和国劳动法", "48", "第四十八条　国家实行最低工资保障制度。", "unknown"),
    )
    answer = (
        "不可以。依据《中华人民共和国劳动法》第五十条，工资应当以货币形式按月支付给劳动者本人。"
    )

    record = scoring.score_case(case, answer, evidence)
    assert record["citation_recall"] == 1.0
    assert record["fact_coverage"] == 1.0
    assert record["unwarranted_citations"] == []
    assert record["abstained"] is False
    assert record["abstain_correct"] is True


def test_score_case_flags_invented_article_number():
    """凭空引用是最该盯住的幻觉：模型把不存在的条号说成依据。"""
    case = {
        "id": "g",
        "category": "grounded",
        "expect_citations": [["中华人民共和国劳动法", "50"]],
        "required_facts": [],
    }
    evidence = _evidence(("中华人民共和国劳动法", "50", "第五十条　……", "unknown"))
    answer = "依据《中华人民共和国劳动法》第一百零一条，工资可以实物发放。"

    record = scoring.score_case(case, answer, evidence)
    assert record["unwarranted_citations"] == ["101"]
    assert record["citation_recall"] == 0.0


def test_score_case_detects_trap_rule_from_a_distractor():
    case = {
        "id": "g",
        "category": "grounded",
        "expect_citations": [["中华人民共和国劳动法", "50"]],
        "required_facts": [],
        "forbidden_facts": ["最低工资标准"],
    }
    evidence = _evidence(
        ("中华人民共和国劳动法", "50", "第五十条　工资应当以货币形式按月支付。", "unknown"),
        (
            "中华人民共和国劳动法",
            "48",
            "第四十八条　用人单位支付劳动者的工资不得低于当地最低工资标准。",
            "unknown",
        ),
    )

    record = scoring.score_case(case, "工资不得低于当地最低工资标准。", evidence)
    assert record["forbidden_hits"] == ["最低工资标准"]


def test_score_case_distinguishes_abstention_from_over_abstention():
    grounded = {
        "id": "g",
        "category": "grounded",
        "expect_citations": [["法", "1"]],
        "required_facts": [],
    }
    abstain = {"id": "a", "category": "abstain", "expect_abstain": True, "expect_citations": []}
    evidence = _evidence(("法", "1", "第一条　正文。", "unknown"))

    assert scoring.score_case(grounded, "依据不足。", evidence)["abstain_correct"] is False
    assert (
        scoring.score_case(abstain, "依据不足，没有检索到相关条文。", evidence)["abstain_correct"]
        is True
    )
    # 「本条未规定……」是正常表述，不该被当成拒答
    record = scoring.score_case(grounded, "第一条未规定该事项。", evidence)
    assert record["abstained"] is False


def test_score_case_flags_missing_effectiveness_notice():
    case = {
        "id": "st",
        "category": "status",
        "expect_citations": [["中华人民共和国监狱法", "2"]],
        "required_facts": ["监狱是国家的刑罚执行机关"],
        "status_flag_required": True,
    }
    evidence = _evidence(
        ("中华人民共和国监狱法", "2", "第二条　监狱是国家的刑罚执行机关。", "not_yet_effective")
    )

    quiet = scoring.score_case(case, "依据《监狱法》第二条，监狱是国家的刑罚执行机关。", evidence)
    assert quiet["status_flag_required"] is True
    assert quiet["status_flagged"] is False

    loud = scoring.score_case(
        case, "依据《监狱法》第二条（尚未生效），监狱是国家的刑罚执行机关。", evidence
    )
    assert loud["status_flagged"] is True


def test_summarise_reports_pass_and_fail_per_metric():
    thresholds = dict.fromkeys(scoring.THRESHOLD_KEYS, 0.5)
    records = [
        {
            "id": "g",
            "expect_abstain": False,
            "citation_recall": 1.0,
            "fact_coverage": 1.0,
            "unwarranted_citations": [],
            "forbidden_hits": [],
            "abstained": False,
            "abstain_correct": True,
            "status_flag_required": False,
            "status_flagged": False,
        },
        {
            "id": "a",
            "expect_abstain": True,
            "citation_recall": None,
            "fact_coverage": None,
            "unwarranted_citations": ["101"],
            "forbidden_hits": ["最低工资标准"],
            "abstained": False,
            "abstain_correct": False,
            "status_flag_required": False,
            "status_flagged": False,
        },
    ]

    summary = scoring.summarise(records, thresholds)
    assert summary["metrics"]["citation_recall"] == 1.0
    assert summary["metrics"]["no_fabrication_rate"] == 0.5
    assert summary["metrics"]["abstain_accuracy"] == 0.0
    assert summary["passed"] is False
    assert summary["checks"]["abstain_accuracy"]["ok"] is False
    assert summary["checks"]["citation_recall"]["ok"] is True


def test_validate_structure_catches_the_obvious_mistakes():
    dataset = {
        "thresholds": dict.fromkeys(scoring.THRESHOLD_KEYS, 1.0),
        "cases": [
            {
                "id": "x",
                "category": "grounded",
                "question": "问",
                "evidence": [["法", "1"]],
                # 期望引用不在证据里 —— 打分会永远算不中
                "expect_citations": [["法", "2"]],
                "required_facts": ["正文"],
            },
            {
                "id": "x",
                "category": "abstain",
                "question": "问",
                "evidence": [],
                "expect_abstain": True,
                "abstain_kind": "no_evidence",
                # 拒答用例不该有期望引用
                "expect_citations": [["法", "1"]],
            },
        ],
    }

    problems = scoring.validate_structure(dataset)
    joined = "\n".join(problems)
    assert "id 重复" in joined
    assert "expect_citations 不在 evidence 里" in joined
    assert "拒答用例不应有 expect_citations" in joined


def test_validate_against_corpus_requires_derivable_expectations():
    """「期望是推导的、可复核的」——事实必须能在条款正文里找到出处。"""
    dataset = {
        "cases": [
            {
                "id": "x",
                "evidence": [["法", "1"], ["法", "2"]],
                "expect_citations": [["法", "1"]],
                "required_facts": ["正文甲"],
                "forbidden_facts": ["干扰乙"],
            }
        ]
    }
    corpus = {
        ("法", "1"): {"text": "第一条　正文甲。", "legal_status": "unknown"},
        ("法", "2"): {"text": "第二条　干扰乙。", "legal_status": "unknown"},
    }
    assert scoring.validate_against_corpus(dataset, corpus) == []

    corpus[("法", "1")]["text"] = "第一条　换了个说法。"
    problems = scoring.validate_against_corpus(dataset, corpus)
    assert any("required_fact 不是期望条款正文的子串" in item for item in problems)


def test_validate_against_corpus_rejects_self_contradictory_traps():
    dataset = {
        "cases": [
            {
                "id": "x",
                "evidence": [["法", "1"]],
                "expect_citations": [["法", "1"]],
                "required_facts": [],
                # 陷阱必须真的在证据里、又不能出现在期望条款里，否则它不是陷阱
                "forbidden_facts": ["正文甲", "查无此句"],
            }
        ]
    }
    corpus = {("法", "1"): {"text": "第一条　正文甲。", "legal_status": "unknown"}}

    problems = scoring.validate_against_corpus(dataset, corpus)
    assert any("forbidden_fact 出现在期望条款里" in item for item in problems)
    assert any("forbidden_fact 不在任何证据里" in item for item in problems)


def test_committed_dataset_is_well_formed():
    """金标准进了版本库——改坏了要在这里拦住（与语料的比对在评测脚本里跑）。"""
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    assert scoring.validate_structure(dataset) == []
    assert len(dataset["cases"]) >= 20
    assert any(case.get("expect_abstain") for case in dataset["cases"])
    assert any(case.get("status_flag_required") for case in dataset["cases"])
