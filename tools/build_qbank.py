"""构建公开题库检索库：从 NuminaMath-CoT（公开数据集）抽取 olympiads 子集。

来源：AI-MO/NuminaMath-CoT（HuggingFace 公开数据集，MIT/Apache 许可），
本脚本只做两件事：过滤 source=olympiads、清洗截断、打包成运行时可直接加载的
gzip JSONL（检索索引在运行期由 utils/qbank.py 一次性构建，不随仓库分发）。

合规边界（重要）：
  * 只收录公开竞赛题与公开解答（IMO/APMO/AMC/AIME/各国奥赛等公开档案）；
  * 不收录任何官方评测题、标准答案、判分口径（评测集私有闭源，无从获取也不获取）；
  * 检索结果以"相似题参考"注入提示词，只允许借鉴方法、禁止直接搬答案
    （反锚定说明见 utils/qbank.py 的 build_reference_block）。

用法：
    python tools/build_qbank.py --parquet_dir .numina_dl --out qbank
"""
import argparse
import gzip
import hashlib
import json
import re
from pathlib import Path

import pyarrow.parquet as pq
from sklearn.feature_extraction.text import HashingVectorizer

#: 每题解答的入库截断（参考材料不需要全文；截断也压低包体）。
MAX_PROBLEM_CHARS = 1200
MAX_SOLUTION_CHARS = 1600

#: 检索索引参数（与 utils/qbank.py 保持一致——运行期查询必须用同一组参数）。
N_FEATURES = 2 ** 18
NGRAM_RANGE = (2, 4)
ANALYZER = "char_wb"

_WS_RE = re.compile(r"[ \t\u3000]+")
_LATEX_JUNK_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")  # markdown 图片


def _clean(text: str, cap: int) -> str:
    if not text:
        return ""
    text = _LATEX_JUNK_RE.sub("", str(text))
    text = _WS_RE.sub(" ", text)
    return text.strip()[:cap]


def extract_olympiads(parquet_dir: Path, log=print) -> list[dict]:
    """按行组流式过滤 source=olympiads，返回 [{src, p, s}, ...]。"""
    entries = []
    shards = sorted(parquet_dir.glob("train-*.parquet"))
    if not shards:
        raise SystemExit(f"no parquet shards under {parquet_dir}")
    for shard in shards:
        pf = pq.ParquetFile(shard)
        for rg in range(pf.num_row_groups):
            tbl = pf.read_row_group(rg, columns=["source", "problem", "solution"])
            sources = tbl.column("source").to_pylist()
            problems = tbl.column("problem").to_pylist()
            solutions = tbl.column("solution").to_pylist()
            for src, p, s in zip(sources, problems, solutions):
                if src != "olympiads":
                    continue
                cp, cs = _clean(p, MAX_PROBLEM_CHARS), _clean(s, MAX_SOLUTION_CHARS)
                if not cp or not cs:
                    continue
                entries.append({"src": "olympiads", "p": cp, "s": cs})
        log(f"{shard.name}: cumulative {len(entries)}")
    return entries


def dedupe(entries: list[dict]) -> list[dict]:
    """按题面文本去重（同一题出现在多个竞赛档案中很常见）。"""
    seen = {}
    for e in entries:
        key = e["p"]
        prev = seen.get(key)
        if prev is None:
            seen[key] = e
        elif len(e["s"]) > len(prev["s"]):
            seen[key] = e  # 保留解答更完整的一条
    return list(seen.values())


def write_bank(out_dir: Path, entries: list[dict], log=print) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    bank_path = out_dir / "olympiad_refs.jsonl.gz"
    with gzip.open(bank_path, "wt", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    log(f"bank: {bank_path} ({bank_path.stat().st_size / 1e6:.1f} MB, {len(entries)} entries)")
    return bank_path


def write_manifest(out_dir: Path, entries: list[dict], bank_path: Path, log=print) -> None:
    sha = hashlib.sha256()
    with open(bank_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha.update(chunk)
    manifest = {
        "built_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "source": "AI-MO/NuminaMath-CoT (public dataset), subset olympiads",
        "entries": len(entries),
        "bank_file": bank_path.name,
        "index_params": {"n_features": N_FEATURES, "analyzer": ANALYZER,
                         "ngram_range": list(NGRAM_RANGE), "norm": "l2",
                         "alternate_sign": False},
        "note": "检索索引在运行期构建（utils/qbank.py，~20s/进程），不随仓库分发",
        "bank_sha256": sha.hexdigest(),
    }
    manifest_path = out_dir / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    log(f"manifest: {manifest_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet_dir", default=".numina_dl")
    ap.add_argument("--out", default="qbank")
    args = ap.parse_args()
    entries = extract_olympiads(Path(args.parquet_dir))
    entries = dedupe(entries)
    out_dir = Path(args.out)
    bank_path = write_bank(out_dir, entries)
    write_manifest(out_dir, entries, bank_path)


if __name__ == "__main__":
    main()
