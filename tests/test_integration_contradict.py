"""真机实证 bug 的回归测试：

链路缺陷：Python 算错 CRT 得 803 却自报 contradict 反驳正确的推理 423；
原链路 contradict 直接仲裁且 coordinator 证据闸门强行采信 Python 错值 → 输出错答。
修复后：contradict → playoff 代回复算戳破假证据；coordinator 证据闸门在
playoff decisive 后豁免。

用单元级断言（不依赖 solving 子图并行扇出的 mock 时序）。
"""
import unittest

from nodes.coordinator_node import _evidence_override
from nodes.cross_validator_node import cross_validator_node
from utils.deps import MockClient


def _crt_state():
    """现场：推理 423（对），Python 803（错）+ 自报 contradict。"""
    return {
        "problem": "求满足 x≡2(mod 3), x≡3(mod 5), x≡2(mod 7) 的最小正整数。",
        "question_mode": "computation",
        "category": "离散数学",
        "reasoning_result": {"answer": "423", "steps": [{"step_num": 1, "description": "x"}]},
        "python_code": "# 错误CRT",
        "python_output": {
            "success": True, "answer": "803",
            "stdout": "验证状态: FAIL\n验证证据: 推理候选 423 不满足\n最终答案: 803",
            "evidence_status": "contradict", "evidence_summary": "反驳423",
            "contradictions": ["423 不满足"],
        },
        "reconciliation_round": 0,
    }


class _Cfg:
    """最小 config：注入 deps（含 time_budget=None）。"""

    def __init__(self):
        from utils.deps import Deps
        deps = Deps(client=MockClient([]), skills_loader=None,
                    mcp_client=None, token_budget=None, time_budget=None)
        self._c = {"configurable": {"deps": deps}}

    def get(self, k, default=None):
        return self._c.get(k, default)

    def __getitem__(self, k):
        return self._c[k]


class TestContradictRoutesToPlayoff(unittest.TestCase):
    def test_contradict_goes_to_playoff_not_arbiter(self):
        state = _crt_state()
        out = cross_validator_node(state, _Cfg())
        # 修复前：contradict 直接 mismatch_arbitrating（绕过 playoff）
        # 修复后：contradict → playoff 代回复算裁决
        self.assertEqual(out["next_node"], "playoff")
        self.assertEqual(out["validation_status"], "contradict_playoff")
        self.assertEqual(out["validation_details"]["routing_reason"],
                         "python_evidence_contradiction_deterministic_playoff")

    def test_contradict_playoff_only_once(self):
        # 已真实裁决过（playoff_status 非 skipped）→ 不再进 playoff，防循环
        # V2.6.1：判重依据从 playoff_trace 非空改为 playoff_status（与真实
        # playoff_node 行为一致：真实裁决会同时写入 status 与 trace）。
        state = _crt_state()
        state["playoff_trace"] = [{"status": "indecisive"}]
        state["playoff_status"] = "indecisive"
        out = cross_validator_node(state, _Cfg())
        self.assertNotEqual(out["next_node"], "playoff")

    def test_contradict_playoff_skipped_can_reroute(self):
        # V2.6.1 修复：playoff 被 skipped（预算不足/候选不足）留下的 trace
        # 不算"已裁决"，下次仍可路由 playoff——playoff 节点会重新评估预算，
        # 仍不足时安全降级到仲裁，不会死循环（旧实现把"跳过"当成"跑过"）。
        state = _crt_state()
        state["playoff_trace"] = [{"status": "skipped", "reason": "unaffordable"}]
        state["playoff_status"] = "skipped"
        out = cross_validator_node(state, _Cfg())
        self.assertEqual(out["next_node"], "playoff")


class TestEvidenceOverrideExemption(unittest.TestCase):
    def test_playoff_decisive_exempts_override(self):
        # playoff 已裁决支持推理 423 → 证据闸门不得再用 Python 假证据覆盖
        state = _crt_state()
        state["playoff_status"] = "decisive"
        state["playoff_answer"] = "423"
        validated, note = _evidence_override(state, "423")
        self.assertEqual(validated, "423")  # 不被覆盖成 803
        self.assertEqual(note, "")

    def test_no_playoff_still_overrides(self):
        # 无 playoff 裁决时，证据闸门保持原行为（真实的 Python 对时仍生效）
        state = _crt_state()
        validated, note = _evidence_override(state, "423")
        self.assertEqual(validated, "803")  # 原证据优先逻辑
        self.assertEqual(note, "evidence_priority_override")

    def test_playoff_indecisive_exempts_override(self):
        # V2.6.2：playoff 僵局（两候选都无法区分）时不再盲信 Python 自报
        # contradict——假证据形态的残留风险：僵局下换票只是抛硬币。
        state = _crt_state()
        state["playoff_status"] = "indecisive"
        state["playoff_trace"] = [{"status": "indecisive", "verdict": "INCONCLUSIVE"}]
        validated, note = _evidence_override(state, "423")
        self.assertEqual(validated, "423")  # 不被覆盖成 803
        self.assertEqual(note, "")

    def test_playoff_neither_exempts_override(self):
        # V2.6.2：双证伪（两候选都被驳回）时更不该换到同样被证伪的 Python 侧
        state = _crt_state()
        state["playoff_status"] = "neither"
        validated, note = _evidence_override(state, "423")
        self.assertEqual(validated, "423")
        self.assertEqual(note, "")

    def test_playoff_skipped_keeps_override(self):
        # 跳过态（没跑成）保持原证据优先行为——没有裁决可尊重
        state = _crt_state()
        state["playoff_status"] = "skipped"
        validated, note = _evidence_override(state, "423")
        self.assertEqual(validated, "803")
        self.assertEqual(note, "evidence_priority_override")


if __name__ == "__main__":
    unittest.main()
