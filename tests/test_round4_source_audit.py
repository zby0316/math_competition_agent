from pathlib import Path

from utils.domain_excerpt import select_skill_excerpt_with_diagnostics
from utils.domain_loader import DomainLoader
from utils.knowledge_audit import audit_knowledge


ROOT = Path(__file__).resolve().parents[1] / "knowledge"


def test_audit_finds_answer_derived_material_and_catalog_covers_targeted_entries():
    findings = audit_knowledge(ROOT)
    assert any("参考答案重写" in finding["text"] for finding in findings)
    assert any("标准答案" in finding["text"] for finding in findings)


def test_catalogued_quarantine_heading_is_reported():
    findings = audit_knowledge(ROOT)
    assert findings
    assert all(item["catalogued"] for item in findings)


def test_candidate_excerpt_excludes_known_answer_derived_section():
    loader = DomainLoader()
    result = select_skill_excerpt_with_diagnostics(
        loader.get_skill_document("非基础及进阶课程"),
        "包好函数的参数 c 是多少？",
        2200,
        excluded_headings=loader.quarantined_headings("非基础及进阶课程"),
    )
    assert "标准答案 $c=4$" not in result.text
    assert any("包好函数题" in heading for heading in result.excluded_headings)
