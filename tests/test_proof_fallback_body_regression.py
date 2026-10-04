"""回归：协调器节点失败/超时的降级路径，证明题必须带上证明正文。

背景
----
同一个处境的降级有两条路径，行为此前不一致：

  * ``nodes/assemble_step.py``（协调器 **LLM** 不可用）——已调用 ``build_proof_body`` 补正文；
  * ``utils/node_guard.py``（协调器 **节点**失败或超时）——此前只输出 ``结论：X``。

证明题的答卷主体就是论证过程：只给结论、不给证明，判分器看到的是一个**无依据的
断言**。而 ``build_proof_body`` 是确定性拼装、不花任何 LLM 时间，其 docstring 明确
写着「降级路径同样负担得起」——所以这是漏调用，不是设计。

实测（修复前该路径）：``结论：该映射连续。``
实测（修复后）：       ``结论：该映射连续。\n\n证明过程：\n…``
"""

from utils.node_guard import _fallback_for_node

PROOF_BODY = (
    "证明：对任意 ε>0，由一致有界性取 δ=ε/2。\n"
    "当 |x-y|<δ 时，|f(x)-f(y)| ≤ |x-y| < ε，故 f 连续。"
)
ERROR = {"node": "coordinator", "message": "timeout"}


def _state(ptype, answer="该映射连续。"):
    return {
        "validated_answer": answer,
        "reasoning_result": {"answer": answer, "analysis": "由定义出发，需验证三个条件。",
                             "proof_body": PROOF_BODY, "steps": []},
        "validation_details": {"problem_type": ptype},
    }


def test_proof_fallback_includes_proof_body():
    final = _fallback_for_node("coordinator", _state("proof"), ERROR)["final_response"]
    assert final.startswith("结论：该映射连续。")
    assert "证明过程：" in final
    assert "一致有界性" in final


def test_computation_fallback_is_unchanged():
    final = _fallback_for_node("coordinator", _state("computation", "42"),
                               ERROR)["final_response"]
    assert final == "最终答案：42"


def test_proof_fallback_without_reasoning_products_stays_bare():
    state = _state("proof")
    state["reasoning_result"] = {"answer": "该映射连续。"}
    final = _fallback_for_node("coordinator", state, ERROR)["final_response"]
    assert final == "结论：该映射连续。"


def test_no_answer_still_uses_generic_error():
    state = {"validated_answer": "", "reasoning_result": {},
             "reasoning_raw_response": "", "validation_details": {"problem_type": "proof"}}
    result = _fallback_for_node("coordinator", state, ERROR)
    assert result["fallback_source"] == "generic_error"
    assert result["final_response"].strip()
