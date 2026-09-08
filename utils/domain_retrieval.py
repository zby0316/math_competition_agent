"""对 18 个数学分类的 skill.md 摘要建立 TF-IDF 相似度索引。

默认（降级）实现：TF-IDF 字符 n-gram 余弦相似度（本地，不调 API）。
以后若接入 embedding API，只需替换 encode()/_build()——接口保持不变。
"""
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


class DomainRetrievalIndex:
    def __init__(self, skills_loader):
        self.skills_loader = skills_loader
        self._vectorizer = None
        self._category_vectors = None  # scipy sparse (n_cats, n_features)
        self._categories = None
        self._build()

    @staticmethod
    def _extract_summary(skill_doc: str, max_chars: int = 1500) -> str:
        """标题 + 各级小标题 + 短行（核心概念），总长不超过 max_chars。"""
        lines = skill_doc.splitlines()
        summary = []
        chars = 0
        for line in lines:
            s = line.strip()
            if not s:
                continue
            # 收录：小标题、引用块、加粗定义、非表格的短行
            if (s.startswith("#") or s.startswith(">")
                    or s.startswith("- **")
                    or (1 < len(s) <= 60 and not s.startswith("|") and not s.startswith("-"))):
                summary.append(s)
                chars += len(s)
                if chars >= max_chars:
                    break
        return "\n".join(summary) if summary else skill_doc[:max_chars]

    def _build(self):
        cats = self.skills_loader.categories
        self._categories = cats
        summaries = [self._extract_summary(self.skills_loader.get_skill_document(c)) for c in cats]
        # char_wb 的 2-4 gram 无需分词器即可处理中文文本
        self._vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4))
        self._category_vectors = self._vectorizer.fit_transform(summaries)

    def encode(self, text: str):
        return self._vectorizer.transform([text])

    def get_category_vector(self, category: str):
        idx = self._categories.index(category)
        return self._category_vectors[idx]

    def rank_all(self, problem: str, top_k: int = 5):
        """按与题面的 TF-IDF 余弦相似度返回 Top-k 分类（覆盖全部 18 类）。"""
        qv = self.encode(problem)
        sims = cosine_similarity(qv, self._category_vectors).flatten()
        ranked = sorted(zip(self._categories, [float(s) for s in sims]), key=lambda x: x[1], reverse=True)
        return ranked[:top_k]

    def rerank(self, problem: str, candidates, top_k: int = 3):
        """按 TF-IDF 相似度对候选列表 [(name, score), ...] 重排。"""
        qv = self.encode(problem)
        scored = []
        for name, _ in candidates:
            idx = self._categories.index(name)
            sim = float(cosine_similarity(qv, self._category_vectors[idx]).flatten()[0])
            scored.append((name, sim))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]
