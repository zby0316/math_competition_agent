"""多路径采样投票：等价分组 + 加权投票 + 共识分级。

移植自 83.93 分队（xiaoliujun-team/math-agent 的 WeightedVoter）机制，等价判定
复用本仓 AnswerCompare 的符号/数值/文本等价管线（比对方的 normalize 更严格）。
与对方的差异（刻意保守）：
  * 弱共识/冲突不产出胜者——返回 None 让调用方回退原有选择管线（仲裁等），
    保证"关闭多路径"与"多路径但未达成共识"两种情况的最终行为完全一致；
  * Python 确定性证据有否决权：python 成功且与胜出组不等价时不锁定，回退
    原管线（原管线对 python 矛盾的仲裁/对账行为原样保留）。
"""
from utils.answer_compare import AnswerCompare
from utils.answer_pull import looks_incomplete_answer
from utils.cot_trim import is_placeholder_answer
from utils.question_profile import is_objective_mode, normalize_objective_answer
from config import CONFIG


def _usable(answer, max_chars):
    a = (answer or "").strip()
    if not a or len(a) > max_chars:
        return ""
    if is_placeholder_answer(a) or looks_incomplete_answer(a):
        return ""
    return a


def _equivalent(a, b, question_mode):
    """两答案是否等价。客观题走归一化文本比对（选项字母/空位文本），
    计算题走符号/数值/文本等价管线。"""
    if is_objective_mode(question_mode):
        na = normalize_objective_answer(a, question_mode) or a
        nb = normalize_objective_answer(b, question_mode) or b
        ka = "".join(str(na).upper().split())
        kb = "".join(str(nb).upper().split())
        return bool(ka) and ka == kb
    detail = AnswerCompare._match_computation_detailed(a, b)
    return bool(detail.get("is_match"))


def vote(paths, python_answer="", python_ok=False, question_mode="computation"):
    """对多条推理路径的答案做等价分组加权投票。

    paths: [{"answer": str, ...}, ...]——每条成功路径一个条目（权重 1.0）。
    python_answer / python_ok：确定性验证证据，成功时作为独立候选参与
    分组（权重 1.0），并在与胜出组不等价时行使否决权。

    返回 None（无法达成共识）或 dict：
        winner_answer / consensus(strong|moderate) / winner_ratio / groups
    """
    mp = CONFIG.get("multi_path", {}) or {}
    strong = float(mp.get("strong_consensus_ratio", 0.67))
    moderate = float(mp.get("moderate_consensus_ratio", 0.5))
    max_chars = int(mp.get("max_vote_answer_chars", 2000))

    entries = []  # (answer, weight)
    for p in paths or []:
        a = _usable(p.get("answer", ""), max_chars)
        if a:
            entries.append([a, 1.0])
    if python_ok:
        pa = _usable(python_answer, max_chars)
        if pa:
            entries.append([pa, 1.0])
    if len(entries) < 2:
        return None

    # 等价分组（传递闭包：A~B 且 B~C 则同组）。等价判定可能抛异常
    # （sympy 对畸形输入），失败视作"不等价"而不是中断投票。
    groups = []  # [{"answer": 代表答案, "weight": 组权重}]
    for a, w in entries:
        placed = False
        for g in groups:
            try:
                if _equivalent(a, g["answer"], question_mode):
                    g["weight"] += w
                    placed = True
                    break
            except Exception:  # noqa: BLE001 - 等价判定失败按不等价处理
                continue
        if not placed:
            groups.append({"answer": a, "weight": w})
    if not groups:
        return None

    groups.sort(key=lambda g: g["weight"], reverse=True)
    total = sum(g["weight"] for g in groups)
    best = groups[0]
    ratio = best["weight"] / total if total > 0 else 0.0
    if ratio >= strong:
        consensus = "strong"
    elif ratio > moderate and len(groups) == 2:
        consensus = "moderate"
    else:
        return None  # 弱共识/冲突：回退原选择管线

    # Python 确定性证据否决权：验证成功且不与胜出组等价时，不锁答案，
    # 交由原管线处理（矛盾证据会路由到对账/仲裁，行为与旧版一致）。
    if python_ok:
        pa = _usable(python_answer, max_chars)
        if pa:
            try:
                if not _equivalent(pa, best["answer"], question_mode):
                    return None
            except Exception:  # noqa: BLE001
                return None

    return {
        "winner_answer": best["answer"],
        "consensus": consensus,
        "winner_ratio": round(ratio, 4),
        "groups": [{"answer": g["answer"], "weight": g["weight"]} for g in groups],
    }
