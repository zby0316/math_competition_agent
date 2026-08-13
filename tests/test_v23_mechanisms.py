# -*- coding: utf-8 -*-
"""V2.3 机制测试：hard_plan（初解质量）+ proof_self_critique（M12 证明自检）。"""
import unittest


class _FakeDeps:
    logger = None
    time_budget = None


class _MockClient:
    def __init__(self, replies: dict | str):
        self.replies = replies if isinstance(replies, dict) else {"default": replies}
        self.calls = 0

    def chat(self, *a, **k):
        self.calls += 1
        # 第一次调用是自检（返回 JSON），第二次是修复（返回完整证明）
        if self.calls == 1:
            return self.replies.get("critique", '{"sound": true, "gap": "ok"}')
        return self.replies.get("fix", "证明：修复后的完整证明……因此结论成立，证毕。")


class TestHardPlan(unittest.TestCase):
    def test_hard_plan_trigger(self):
        from utils.hard_plan import hard_plan_hint, wants_plan
        self.assertTrue(wants_plan("computation", 0.5, "hard"))
        self.assertTrue(wants_plan("proof", None, "very_hard"))
        self.assertTrue(wants_plan("proof", 0.4, ""))  # 低置信触发
        self.assertFalse(wants_plan("choice", 0.9, "hard"))  # 客观题不触发
        self.assertFalse(wants_plan("computation", 0.9, "easy"))
        self.assertTrue("目标锚定" in hard_plan_hint("proof", 0.5, "hard"))

    def test_hard_plan_objective_skip(self):
        from utils.hard_plan import hard_plan_hint
        self.assertEqual(hard_plan_hint("true_false", 0.3, "hard"), "")


class TestProofSelfCritique(unittest.TestCase):
    def _client(self, critique_reply):
        return _MockClient({"critique": critique_reply,
                            "fix": "证明：设 f(t)=e^t，由中值定理得 e^x-1=x e^ξ，且 e^ξ>1，因此 e^x>1+x，证毕。"})

    def test_sound_true_keeps_original(self):
        from utils.proof_deepener import proof_self_critique
        client = self._client('{"sound": true, "gap": "ok"}')
        proof = "证明：……因此结论成立，证毕。"
        out = proof_self_critique(client, _FakeDeps(), "证明题", proof)
        self.assertEqual(out, proof)

    def test_sound_false_triggers_fix(self):
        from utils.proof_deepener import proof_self_critique
        client = self._client('{"sound": false, "gap": "中值定理适用条件未说明"}')
        proof = "证明：由中值定理得结论成立。"
        out = proof_self_critique(client, _FakeDeps(), "证明题", proof)
        self.assertIn("证毕", out)
        self.assertGreater(len(out), len(proof))

    def test_disabled_by_config(self):
        import utils.proof_deepener as pd
        old = pd.CONFIG.get("enable_proof_self_critique", True)
        pd.CONFIG["enable_proof_self_critique"] = False
        try:
            from utils.proof_deepener import proof_self_critique
            proof = "原证明"
            out = proof_self_critique(_MockClient("{}"), _FakeDeps(), "题", proof)
            self.assertEqual(out, proof)
        finally:
            pd.CONFIG["enable_proof_self_critique"] = old


if __name__ == "__main__":
    unittest.main()
