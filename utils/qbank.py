"""公开题库检索：相似竞赛题 + 解答作为"参考示例"注入提示词。

数据源：qbank/olympiad_refs.jsonl.gz（AI-MO/NuminaMath-CoT 公开数据集的
olympiads 子集，由 tools/build_qbank.py 构建）。检索索引在运行期一次性构建
（HashingVectorizer 文档矩阵，~20s/进程），不随仓库分发——规避托管平台
单文件大小限制，且成本摊销在整卷题目上可忽略。

设计要点：
  * 默认关（CONFIG["qbank_enabled"]=False）——关闭时本模块完全不被触碰，
    行为与 69.64 版本树逐字节一致；
  * 检索是字符 n-gram hashing + 余弦相似度，纯本地、确定性、零 API 调用；
  * 参考块带反锚定说明：相似题不是本题，参数/量词/边界不同则结论不可迁移，
    只允许借鉴方法并在本题参数下重算——这是与"标准答案入包"的合规分界线。

合规边界：只含公开竞赛题与公开解答，不含任何官方评测题/标准答案/判分口径。
"""
from __future__ import annotations

import gzip
import json
import threading
from pathlib import Path

from config import CONFIG

_BANK_DIR = Path(__file__).resolve().parent.parent / "qbank"
_BANK_FILE = _BANK_DIR / "olympiad_refs.jsonl.gz"

_lock = threading.Lock()
_singleton: "QBank | None" = None


class QBank:
    """进程级单例：条目列表 + 预计算索引，全部惰性加载。"""

    def __init__(self):
        self._entries = None          # list[dict] {"src","p","s"}
        self._matrix = None           # scipy csr (L2 归一，运行期构建)
        self._vectorizer = None       # HashingVectorizer

    # ---- 加载 ----

    @property
    def entries(self) -> list[dict]:
        if self._entries is None:
            rows = []
            with gzip.open(_BANK_FILE, "rt", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
            self._entries = rows
        return self._entries

    def _ensure_vectorizer(self):
        if self._vectorizer is None:
            from sklearn.feature_extraction.text import HashingVectorizer
            self._vectorizer = HashingVectorizer(
                n_features=2 ** 18, analyzer="char_wb", ngram_range=(2, 4),
                alternate_sign=False, norm="l2",
            )
        return self._vectorizer

    @property
    def matrix(self):
        """运行期一次性构建文档矩阵（~20s/进程），之后查询 ~1s。

        不随仓库分发预计算索引：gitcode 单文件大小限制会拒绝 100MB+ 文件，
        而运行期构建成本摊销在整卷 112 题上可忽略。
        """
        if self._matrix is None:
            vec = self._ensure_vectorizer()
            self._matrix = vec.transform([e["p"] for e in self.entries])
        return self._matrix

    # ---- 检索 ----

    def search(self, problem: str, top_k: int = 2):
        """返回 [(score, entry), ...]，score 为余弦相似度（0-1 量级）。"""
        from sklearn.metrics.pairwise import cosine_similarity
        vec = self._ensure_vectorizer()
        query = vec.transform([problem or ""])
        mat = self.matrix
        sims = cosine_similarity(query, mat).flatten()
        order = sims.argsort()[::-1][:max(0, int(top_k))]
        entries = self.entries
        return [(float(sims[i]), entries[i]) for i in order if float(sims[i]) > 0.0]


def get_qbank() -> "QBank | None":
    """开关开启且文件齐备时返回单例；否则 None（默认关 → 永远 None）。"""
    global _singleton
    if not CONFIG.get("qbank_enabled"):
        return None
    if _singleton is None:
        with _lock:
            if _singleton is None:
                if not _BANK_FILE.exists():
                    return None
                _singleton = QBank()
    return _singleton


_ANTI_ANCHOR = (
    "参考示例（来自公开数学竞赛题库的相似题与解答）：\n\n"
    "⚠️ 这些是**相似题，不是本题**。相似度高只说明措辞接近，不说明结论相同。"
    "使用规则：\n"
    "1. 先逐项核对差异：参数数值（个数/上界/模数）、奇偶性、谁做选择、目标是"
    "最大化还是最小化、约束方向（≤ 还是 <）、\"所有解\"还是\"一个解\"。\n"
    "2. 只要有一项不同，示例的结论就不可迁移——最多借鉴它的方法与技巧，"
    "且必须在本题参数下从头重算，并把结果代回本题题面验证。\n"
    "3. 核对后不能确认两题严格同构的，完全忽略示例，独立求解。"
)

_PYTHON_ANTI_ANCHOR = (
    "参考题面（来自公开数学竞赛题库的相似题，仅用于验证方法）：\n\n"
    "⚠️ 相似题不是本题。它的解答只用于理解可用算法/验证手段，参数与结论都"
    "不可直接搬用；验证代码必须完全按本题题面编写。"
)


def build_reference_block(problem: str, top_k: int, max_chars: int,
                          mode: str = "reasoning") -> tuple[str, list[dict]]:
    """构造参考示例块；返回 (文本, 命中元数据)。空文本表示无命中/未开启。

    mode="reasoning" 注入推理提示（题+解答）；mode="python" 注入验证代码
    生成提示（只给题面，不给解答结论，防止验证代码照抄参考答案）。
    """
    qb = get_qbank()
    if qb is None:
        return "", []
    try:
        hits = qb.search(problem, top_k)
    except Exception:
        return "", []
    if not hits:
        return "", []
    parts = [_PYTHON_ANTI_ANCHOR if mode == "python" else _ANTI_ANCHOR]
    meta = []
    budget = max(200, int(max_chars))
    for i, (score, entry) in enumerate(hits, 1):
        p = str(entry.get("p") or "").strip()
        s = str(entry.get("s") or "").strip()
        if mode == "python":
            body = (f"【示例{i}｜公开题库｜相似度 {score:.2f}】\n"
                    f"题目：{p[:budget]}")
        else:
            body = (f"【示例{i}｜公开题库｜相似度 {score:.2f}】\n"
                    f"题目：{p[:max(200, budget // 2)]}\n"
                    f"解答：{s[:budget]}")
        meta.append({"score": round(score, 4), "src": entry.get("src", ""),
                     "problem_chars": len(entry.get("p") or ""),
                     "solution_chars": len(entry.get("s") or "")})
        parts.append(body)
    return "\n\n".join(parts), meta
