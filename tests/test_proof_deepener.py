# -*- coding: utf-8 -*-
"""V2.1 M7 ProofDeepener 测试。

覆盖：证明题识别 / 结构检查（三段式缺失检测）/ 补强路径（mock LLM）。
"""
import unittest

from utils.proof_deepener import (is_proof_question, proof_structure_check)


class _FakeDeps:
    logger = None
    time_budget = None


class _MockClient:
    def __init__(self, reply: str):
        self.reply = reply

    def chat(self, *a, **k):
        return self.reply


class TestProofDeepener(unittest.TestCase):

    def test_is_proof_question(self):
        self.assertTrue(is_proof_question("证明：e^x > 1+x 对一切 x>0 成立"))
        self.assertTrue(is_proof_question("求证：素数阶群是循环群"))
        self.assertTrue(is_proof_question("推导 Frenet 标架公式"))
        self.assertTrue(is_proof_question("计算定积分", ptype="proof"))  # ptype 优先
        self.assertFalse(is_proof_question("计算定积分"))
        self.assertFalse(is_proof_question("求矩阵的特征值"))

    def test_structure_check_complete(self):
        ans = ("证明：设 f(t)=e^t 在 [0,x] 连续。\n"
               "步骤1：由拉格朗日中值定理，存在 ξ∈(0,x) 使 e^x-1=x·e^ξ。\n"
               "步骤2：因 e^ξ>1 且 x>0，故 e^x-1>x。\n"
               "因此 e^x>1+x，证毕。")
        r = proof_structure_check(ans, "证明不等式")
        self.assertTrue(r["complete"])
        self.assertEqual(r["reasons"], [])

    def test_structure_check_missing_conclusion(self):
        # 缺显式结论 → incomplete（推理中间的"所以"不算结论）
        ans = ("设 f(t)=e^t，由中值定理存在 ξ 使 e^x-1=x·e^ξ，"
               "且 e^ξ>1，所以 e^x-1>x，于是 e^x>1+x")
        r = proof_structure_check(ans, "证明不等式")
        self.assertFalse(r["complete"])
        self.assertTrue(r["has_statement"])
        self.assertTrue(any("缺显式结论" in s for s in r["reasons"]))

    def test_structure_check_missing_steps(self):
        # 只有结论没有步骤链
        ans = "证毕：e^x>1+x。"
        r = proof_structure_check(ans, "证明不等式")
        self.assertFalse(r["complete"])
        self.assertTrue(any("缺关键步骤链" in s for s in r["reasons"]))

    def test_deepen_proof_refines(self):
        from utils.proof_deepener import deepen_proof
        client = _MockClient(
            "证明：设 f(t)=e^t。\n步骤1：中值定理得 e^x-1=x·e^ξ。\n"
            "步骤2：e^ξ>1。\n因此 e^x>1+x，证毕。")
        ans = "由中值定理 e^x-1=x·e^ξ，e^ξ>1，故 e^x>1+x"
        out = deepen_proof(client, _FakeDeps(), "证明 e^x>1+x", ans)
        self.assertIn("证毕", out)
        self.assertGreater(len(out), len(ans))

    def test_deepen_proof_keeps_original_on_failure(self):
        from utils.proof_deepener import deepen_proof
        # 补强结果退化（太短/无结论）→ 保留原答案
        client = _MockClient("嗯")
        ans = "由中值定理可得 e^x>1+x"
        out = deepen_proof(client, _FakeDeps(), "证明不等式", ans)
        self.assertEqual(out, ans)

    def test_deepen_proof_disabled_by_config(self):
        import utils.proof_deepener as pd
        old = pd.CONFIG.get("enable_proof_deepener", True)
        pd.CONFIG["enable_proof_deepener"] = False
        try:
            from utils.proof_deepener import deepen_proof
            client = _MockClient("补强内容")
            ans = "原答案"
            out = deepen_proof(client, _FakeDeps(), "题", ans)
            self.assertEqual(out, ans)
        finally:
            pd.CONFIG["enable_proof_deepener"] = old


if __name__ == "__main__":
    unittest.main()
