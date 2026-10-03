"""Extract historical evidence offline; stage attribution is bounded by trace coverage."""
import argparse
import hashlib
import json
from pathlib import Path

CASES = [
    (3, "remaining6", "附加商拓扑的开单点写反", "coordination_added", "推理给出原像非闭的有效论证，最终答卷新增错误开集表。"),
    (4, "pilot6", "摘要遗漏 epsilon 范围，分块说明遗漏初始层例外", "summary_and_coordination", "推理正文限定 epsilon∈(0,1)，最终摘要未保留该范围；每块存在零项还需初始层说明。"),
    (5, "remaining6", "反例尾部等式被写成全部项成立", "coordination_visible_origin_unknown", "最终反例未单独定义首项；推理可见步骤未包含完整反例，不能定位未保留原文的生成来源。"),
    (6, "pilot6", "不连续硬截断直接使用支配收敛", "reasoning_and_final", "历史首批报告确认推理及最终答卷均有核心漏洞。"),
    (6, "fixcheck6", "高度阈值被倒数阈值替代", "coordination_visible_origin_unknown", "最终写 n>1/epsilon，但事件计算要求 n>epsilon；两次推理结构失败，原文只保留节选。"),
    (8, "remaining6", "复数内积被直接取 liminf", "coordination_added", "可见推理取内积极限后与实数比较，协调新增 liminf 内积的未定义操作。"),
    (9, "pilot6", "复数坐标公式缺共轭或标量域声明", "coordination_visible_origin_unknown", "推理在反例前截断，最终给出坐标反例；不能排除未保留文本中的来源。"),
    (7, "remaining6", "Egorov 被通用一致收敛线索覆盖", "classification", "历史实际分类为数学分析，未加载测度手册。"),
    (10, "pilot6", "闭图像算子问题被上确界线索覆盖", "classification_already_fixed", "首批历史误分类已由 2162050 中的泛函强线索规则修复；此次保留控制回归。"),
]


def audit(history_root):
    rows = []
    for idx, run, issue, origin, note in CASES:
        path = history_root / f"proof_gap12_{run}_20261001" / f"{idx}.json"
        row = {"idx": idx, "run": run, "issue": issue, "origin": origin, "note": note,
               "source_path": str(path), "official_loss_source_verified": False}
        if not path.is_file():
            row.update(status="missing_artifact", origin="unknown")
        else:
            content = path.read_bytes()
            output = json.loads(content.decode("utf-8"))
            reasoning = next((entry.get("thinking", {}) for entry in output["trace"]
                              if entry.get("step") == "reasoning"), {})
            row.update(status="historical_artifact_read", source_sha256=hashlib.sha256(content).hexdigest(),
                       reasoning_visible=reasoning, final_response=output["final_response"],
                       classification=next((entry for entry in output["trace"] if entry.get("step") == "classification"), {}),
                       agent_version=next((entry for entry in output["trace"] if entry.get("step") == "agent_version"), {}))
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "outputs/round2_history_evidence.json")
    args = parser.parse_args()
    rows = audit(args.history_root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"policy": "strict_full_answer", "cases": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Read {sum(row['status'] == 'historical_artifact_read' for row in rows)}/{len(rows)} historical artifacts; no API calls")


if __name__ == "__main__":
    main()
