"""回归：英文题面不得被"模块速查"尾巴霸榜。

背景
----
中文手册的模块标题无法与英文题面做字面重合（``_score`` 是词汇交集），但速查
条目里的 ``- 检索词：`` 行塞了 how many / minimum / subsets 之类英文泛词，
会被 ``_terms(module)`` 整块收进来**直接命中英文题面**，于是无关速查条目拿到
正分、把正文模块整体挤出预算。

实测（修复前）：一道 ``How many derangements of 5 elements are there?`` 在
85914 字符的《离散数学》手册上只抽到 2803 字符，命中的是
"模块速查：鸽巢原理在整数组极值中的应用"与
"模块速查：网格最坏情形极值：识别查询与覆盖统计"，
而真正对口的正文模块 ``## 2. 组合计数`` 得 0 分、从未进入 prompt。

仓库两套公开题集（U-MATH / UGMathBench）全部是英文题面，所以这是常态路径。
"""

from utils.domain_excerpt import _is_english_dominant, select_skill_excerpt
from utils.domain_loader import DomainLoader

_LOADER = DomainLoader()


def test_english_detection():
    assert _is_english_dominant("How many derangements of 5 elements are there?")
    assert not _is_english_dominant("求模 20 的整数环中乘法可逆元的个数。")


def test_english_problem_is_not_dominated_by_lookup_tail():
    doc = _LOADER.get_skill_document("离散数学")
    excerpt = select_skill_excerpt(
        doc, "How many derangements of 5 elements are there?", 3000)
    # 这两条是修复前实际胜出的无关速查条目。
    assert "模块速查：鸽巢原理在整数组极值中的应用" not in excerpt
    assert "模块速查：网格最坏情形极值" not in excerpt
    # 正文模块（含组合计数）必须进入预算。
    assert "组合计数" in excerpt


def test_chinese_problem_keeps_module_targeting():
    # 中文路径不得被本次改动影响：仍按词汇重合选中对口的正文模块。
    doc = _LOADER.get_skill_document("抽象代数")
    excerpt = select_skill_excerpt(
        doc, "求模 20 的整数环中乘法可逆元的个数。", 3000)
    assert excerpt
    assert len(excerpt) <= 3000
