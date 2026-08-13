# -*- coding: utf-8 -*-
"""V2.3 M13（题面覆盖检查）+ M14（难题兜底）测试。"""
import unittest


class _FakeDeps:
    logger = None
    time_budget = None


class _SimpleClient:
    """单回复 mock：每次 chat 返回同一内容。"""
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    def chat(self, *a, **k):
        self.calls += 1
        return self.reply


class _SeqClient:
    """序列 mock：按调用次数依次返回 replies。"""
    def __init__(self, replies: list):
        self.replies = replies
        self.calls = 0

    def chat(self, *a, **k):
        idx = min(self.calls, len(self.replies) - 1)
        self.calls += 1
        return self.replies[idx]


class TestM13Coverage(unittest.TestCase):
    def test_coverage_missing_triggers_fix(self):
        from utils.proof_deepener import proof_self_critique
        client = _SeqClient([
            '{"sound": true, "gap": "ok", "coverage": false, "missing": "漏答几何意义"}',
            "证明：设 X 为实向量空间，由 Hahn-Banach 定理得存在线性泛函延拓。几何意义：该泛函可由一个超平面分离两个凸集来表示。因此定理得证，证毕。",
        ])
        proof = "证明：Hahn-Banach 定理…证毕。"
        out = proof_self_critique(client, _FakeDeps(), "证明Hahn-Banach定理并说明几何意义", proof)
        self.assertIn("几何意义", out)

    def test_coverage_ok_keeps_original(self):
        from utils.proof_deepener import proof_self_critique
        client = _SimpleClient('{"sound": true, "gap": "ok", "coverage": true, "missing": "ok"}')
        proof = "证明：…证毕。"
        out = proof_self_critique(client, _FakeDeps(), "证明题", proof)
        self.assertEqual(out, proof)

    def test_sound_false_triggers_fix(self):
        from utils.proof_deepener import proof_self_critique
        client = _SeqClient([
            '{"sound": false, "gap": "中值定理适用条件未说明", "coverage": true, "missing": "ok"}',
            "证明：设函数在闭区间连续且开区间可导，则由中值定理得存在中间点使得结论成立。因此原命题得证，证毕。",
        ])
        proof = "证明：由中值定理得结论成立。"
        out = proof_self_critique(client, _FakeDeps(), "证明题", proof)
        self.assertIn("证毕", out)


class TestM14Fallback(unittest.TestCase):
    def test_looks_like_give_up(self):
        from utils.hard_fallback import looks_like_give_up
        self.assertTrue(looks_like_give_up("无法完成"))
        self.assertTrue(looks_like_give_up("最终答案：无法确定，缺少关键条件"))
        self.assertTrue(looks_like_give_up("无法给出完整答案"))
        self.assertFalse(looks_like_give_up("最终答案：x=3，证明如下……证毕。"))
        self.assertFalse(looks_like_give_up("结论：命题成立。"))

    def test_partial_fallback_produces_answer(self):
        from utils.hard_fallback import partial_fallback
        client = _SimpleClient(
            "1. 特殊情形：n=2 时同色三元组必存在，可直接验证；2. 思路框架：用染色论证逐步扩大；3. 部分结论：猜想成立。")
        out = partial_fallback(client, _FakeDeps(), "证明 Van der Waerden W(2,3)=9", "无法完成")
        self.assertNotIn("无法完成", out)
        self.assertGreater(len(out), 30)

    def test_partial_fallback_keeps_if_not_give_up(self):
        from utils.hard_fallback import partial_fallback
        out = partial_fallback(_SimpleClient("x"), _FakeDeps(), "题", "最终答案：42。")
        self.assertEqual(out, "最终答案：42。")

    def test_disabled_by_config(self):
        import utils.hard_fallback as hf
        old = hf.CONFIG.get("enable_hard_fallback", True)
        hf.CONFIG["enable_hard_fallback"] = False
        try:
            out = hf.partial_fallback(_SimpleClient("x"), _FakeDeps(), "题", "无法完成")
            self.assertEqual(out, "无法完成")
        finally:
            hf.CONFIG["enable_hard_fallback"] = old


if __name__ == "__main__":
    unittest.main()
