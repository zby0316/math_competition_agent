# -*- coding: utf-8 -*-
"""V2.5 收益点1：final_response 关键步骤不再是"空壳"（判分口径对齐）。

官方铁律："推导题应保留支撑结论所需的关键步骤"。旧 _condense_step 只输出
"步骤N：标题（定理名）"，把 description 里的实际计算式（$|A|=\\lfloor
120/2\\rfloor=60$）全丢了，且模型标题带 ** 残留时输出"xxx**"空壳。
修复后：标题 + 关键计算式，让推导过程真正呈现给官方 judger。
"""
import unittest

from utils.answer_formatter import _condense_step, build_steps_block


class TestCondenseStepKeepsCalculation(unittest.TestCase):
    """V2.5：关键步骤必须含支撑结论的计算式，而非空标题壳。"""

    def test_markdown_star_residue_stripped(self):
        """标题带 ** 残留时不再输出"xxx**"空壳。"""
        step = {"step_num": 1, "description": "定义集合与总个数**  \n使用定义：设 $S = 120$。"}
        out = _condense_step(step)
        self.assertNotIn("**", out, f"标题残留 ** 未剥除: {out!r}")

    def test_calc_line_extracted(self):
        """含 '=' 的关键计算式被提取进关键步骤。"""
        step = {"step_num": 2, "description":
                "计算各单个集合的大小**  \n使用公式：$|A| = \\lfloor n/k \\rfloor$。  \n"
                "计算过程：\n- $|A| = \\lfloor 120/2 \\rfloor = 60$\n"
                "- $|B| = \\lfloor 120/3 \\rfloor = 40$"}
        out = _condense_step(step)
        self.assertIn("$|A|", out, f"关键计算式未被提取: {out!r}")
        self.assertIn("60", out, f"计算值 60 未出现: {out!r}")

    def test_fallback_to_theorem_when_no_calc(self):
        """无计算式时退回定理名（旧逻辑仍可用）。"""
        step = {"step_num": 1, "description":
                "应用费马小定理  \n使用定理：p 为素数时 a^p ≡ a (mod p)。"}
        out = _condense_step(step)
        self.assertIn("步骤1", out)
        # 至少标题存在，不是空
        self.assertGreater(len(out), 5)

    def test_build_steps_block_has_substance(self):
        """build_steps_block 输出的关键步骤含实质内容（非空壳）。"""
        steps = [
            {"step_num": 1, "description":
             "定义集合**  \n使用定义：设 $S = \\{1,2,\\dots,120\\}$，则 $|S| = 120$。"},
            {"step_num": 2, "description":
             "计算**  \n计算过程：\n- $|A| = \\lfloor 120/2 \\rfloor = 60$"},
        ]
        block = build_steps_block(steps)
        self.assertIn("$|S| = 120$", block)
        self.assertIn("60", block)


if __name__ == "__main__":
    unittest.main()
