"""Offline bilingual routing checks for the round-four retrieval layer."""

import pytest

from utils.domain_loader import DomainLoader


CASES = [
    ("概率论", "随机变量服从正态分布", "Find the normal distribution of a random variable"),
    ("随机过程", "研究马尔可夫链的停时", "Analyze a Brownian motion stopping time"),
    ("数学分析", "证明函数列一致收敛", "Use the Fourier transform on a metric space"),
    ("统计推断", "求最大似然估计与置信区间", "Construct a confidence interval by maximum likelihood"),
    ("复分析", "计算留数并讨论全纯性", "Apply the residue theorem to a holomorphic function"),
    ("抽象代数", "证明群同态的核是正规子群", "Use Galois theory for a group homomorphism"),
    ("高等代数", "求矩阵的特征值和特征多项式", "Find the eigenvalue of the characteristic polynomial"),
    ("常微分方程", "解常微分方程的初值问题", "Solve an ordinary differential equation initial value problem"),
    ("偏微分方程", "求热方程的边值问题", "Solve a partial differential equation boundary value problem"),
    ("泛函分析", "设 X 为 Banach 空间并考虑有界线性泛函", "Prove weak convergence in a Banach space"),
    ("拓扑学", "证明紧致 Hausdorff 空间同胚", "Determine whether a compact space is Hausdorff"),
    ("微分几何", "计算黎曼流形上的测地线与曲率", "Find a geodesic using Gaussian curvature"),
    ("数值分析", "分析有限差分格式的截断误差", "Estimate the condition number of a finite difference scheme"),
    ("测度积分", "证明 Lebesgue 积分的支配收敛", "Show a measurable function has a Lebesgue integral"),
    ("运筹学", "用线性规划求网络最大流", "Solve an operations research network flow problem"),
    ("离散数学", "用生成函数解决图论计数", "Use graph theory and a generating function"),
    ("非基础及进阶课程", "在外接圆中利用垂心求角", "Use the circumcircle and orthocenter in Euclidean geometry"),
    ("线性回归", "最小二乘回归存在异方差", "Diagnose heteroscedasticity in ordinary least squares"),
]


@pytest.mark.parametrize("category, chinese, english", CASES)
def test_bilingual_keyword_candidates_include_expected_category(category, chinese, english):
    loader = DomainLoader()
    for problem in (chinese, english):
        ranked = [name for name, _score in loader.find_candidate_categories(problem, top_k=3)]
        assert category in ranked


@pytest.mark.parametrize("category, _chinese, english", CASES)
def test_bilingual_retrieval_query_has_bounded_expected_anchor(category, _chinese, english):
    loader = DomainLoader()
    expanded = loader.expand_retrieval_query(english)
    anchors = expanded.partition("[local-domain-anchors] ")[2].split()
    assert category in anchors
    assert len(anchors) <= 3
    assert len(" ".join(anchors)) <= 80
    ranked = [name for name, _score in loader.get_embedding_index().rank_all(english, top_k=3)]
    assert category in ranked
