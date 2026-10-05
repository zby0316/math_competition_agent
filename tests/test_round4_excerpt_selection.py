from utils.domain_excerpt import (
    exclude_markdown_sections,
    select_skill_excerpt_with_diagnostics,
)
from utils.domain_loader import DomainLoader


def test_chinese_numbered_chapters_reach_all_runtime_windows():
    cases = [
        ("高等代数", "求矩阵的特征值并讨论对角化条件", "特征值与对角化"),
        ("数学分析", "讨论函数项级数一致收敛，并核对逐项求导的条件", "函数项级数与一致收敛"),
        ("拓扑学", "讨论商拓扑，使用商映射原像核对开集", "Sorgenfrey 与商拓扑"),
    ]
    loader = DomainLoader()
    for category, problem, expected in cases:
        document = loader.get_skill_document(category)
        for limit in (1600, 2200, 3000):
            result = select_skill_excerpt_with_diagnostics(document, problem, limit)
            assert len(result.text) <= limit
            assert any(expected in heading for heading in result.selected_headings)


def test_fenced_heading_is_not_a_section_boundary_or_trace_heading():
    document = """# 手册
## 一、真实章节
真实内容。
```markdown
## 伪章节
```
## 二、目标章节
商拓扑的定义与原像判定。
"""
    result = select_skill_excerpt_with_diagnostics(document, "商拓扑原像", 200)
    assert "## 二、目标章节" in result.text
    assert "## 伪章节" not in result.selected_headings


def test_quarantined_child_section_is_removed_without_removing_siblings():
    document = """## 一、竞赛内容
### 方法卡片
保留通用方法。
### 固定答案
不得进入运行提示。
## 二、其他内容
保留其他章节。
"""
    filtered, excluded = exclude_markdown_sections(document, ["### 固定答案"])
    assert excluded == ("### 固定答案",)
    assert "保留通用方法" in filtered
    assert "不得进入运行提示" not in filtered
    assert "保留其他章节" in filtered


def test_source_catalog_exposes_quarantined_headings():
    headings = DomainLoader().quarantined_headings("非基础及进阶课程")
    assert any("包好函数题" in heading for heading in headings)
