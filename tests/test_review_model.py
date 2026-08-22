# -*- coding: utf-8 -*-
"""V3.3 第二 Intern-S 模型复核通道测试。

覆盖：
1. chat 是否接受 model 关键字（显式形参 / **kwargs / 不支持）的探测；
2. ModelOverrideClient：model 注入、结果透传、模式缓存、降级模式标记
   unsupported（防止"假装换了模型实为同模型"）；
3. resolve_review_client：路径 A（model 覆盖）激活、普通 MockClient 与
   无 env 时返回 None（静默降级）、主模型同名跳过、INTERN_REVIEW_MODEL
   缺失时路径 B 不触发（避免测试误触真实网络）；
4. 复核调用失败/配额用尽 → 回退主 client 同参数调用，结果可用；
5. chat_prefilled_review：prefix 拼接（stitch）语义不变。
"""
import os
import unittest
from unittest.mock import patch

from config import CONFIG
from utils.deps import Deps, MockClient
from utils.review_model import (
    ModelOverrideClient,
    ReviewClientUnsupported,
    _chat_accepts_model,
    _main_model_name,
    chat_prefilled_review,
    chat_with_retry_review,
    resolve_review_client,
    review_client,
)


class _KwargsClient:
    """接受任意关键字（含 model）的替身 client，记录调用。"""

    def __init__(self, model="intern-s2-preview", answers=None):
        self.model = model
        self.answers = list(answers) if answers else ["kw response"]
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": dict(kwargs)})
        if self.answers:
            return self.answers.pop(0)
        return "kw response"


class _ExplicitModelClient:
    """chat 带显式 model 形参的替身。"""

    def __init__(self):
        self.calls = []

    def chat(self, messages, temperature=None, max_tokens=None, model=None,
             thinking_mode=None):
        self.calls.append({"model": model, "temperature": temperature,
                           "max_tokens": max_tokens, "thinking_mode": thinking_mode})
        return "explicit ok"


class _NoModelClient:
    """chat 仅接受固定三参（无 model、无 **kwargs）。"""

    def chat(self, messages, temperature=0.2, max_tokens=4096):
        return "no model ok"


class _FailingClient:
    """复核调用必失败，用于验证回退。"""

    def chat(self, messages, **kwargs):
        raise RuntimeError("review backend down")


class TestChatAcceptsModel(unittest.TestCase):
    def test_explicit_model_param(self):
        self.assertTrue(_chat_accepts_model(_ExplicitModelClient()))

    def test_var_kwargs(self):
        self.assertTrue(_chat_accepts_model(_KwargsClient()))

    def test_fixed_signature_rejected(self):
        self.assertFalse(_chat_accepts_model(MockClient(["x"])))

    def test_none_client(self):
        self.assertFalse(_chat_accepts_model(None))


class TestModelOverrideClient(unittest.TestCase):
    def test_injects_model_and_forwards(self):
        inner = _ExplicitModelClient()
        wrapper = ModelOverrideClient(inner, "intern-s1-pro")
        result = wrapper.chat(
            [{"role": "user", "content": "q"}], temperature=0.1, max_tokens=96,
            thinking_mode=False)
        self.assertEqual(result, "explicit ok")
        self.assertEqual(inner.calls[-1]["model"], "intern-s1-pro")
        self.assertEqual(inner.calls[-1]["temperature"], 0.1)
        self.assertEqual(inner.calls[-1]["max_tokens"], 96)
        self.assertFalse(inner.calls[-1]["thinking_mode"])

    def test_kwargs_client_gets_model(self):
        inner = _KwargsClient()
        wrapper = ModelOverrideClient(inner, "intern-s1-pro")
        wrapper.chat([{"role": "user", "content": "q"}])
        self.assertEqual(inner.calls[-1]["kwargs"]["model"], "intern-s1-pro")

    def test_degrades_to_unsupported_when_model_not_accepted(self):
        # 底层 client 不接受 model：降级探测会走到 kwargs_nomodel，此时必须
        # 标记 unsupported 并抛 ReviewClientUnsupported，绝不"假装复核"。
        inner = _NoModelClient()
        wrapper = ModelOverrideClient(inner, "intern-s1-pro")
        with self.assertRaises(ReviewClientUnsupported):
            wrapper.chat([{"role": "user", "content": "q"}])
        self.assertTrue(wrapper.unsupported)
        # 后续调用快速失败（不再重探测）
        with self.assertRaises(ReviewClientUnsupported):
            wrapper.chat([{"role": "user", "content": "q"}])

    def test_mode_cached(self):
        inner = _ExplicitModelClient()
        wrapper = ModelOverrideClient(inner, "intern-s1-pro")
        wrapper.chat([{"role": "user", "content": "1"}])
        wrapper.chat([{"role": "user", "content": "2"}])
        self.assertEqual(wrapper._mode, "kwargs_model")
        self.assertEqual(len(inner.calls), 2)


class TestResolveReviewClient(unittest.TestCase):
    def _clear_env(self):
        for key in ("INTERN_REVIEW_MODEL", "INTERN_API_KEY"):
            os.environ.pop(key, None)

    def setUp(self):
        self._saved = {
            "enable": CONFIG.get("enable_review_model"),
            "model": CONFIG.get("review_model"),
            "max": CONFIG.get("review_max_calls_per_problem"),
        }
        CONFIG["enable_review_model"] = True
        CONFIG["review_model"] = "intern-s1-pro"
        CONFIG["review_max_calls_per_problem"] = 3
        self._clear_env()

    def tearDown(self):
        for key, value in self._saved.items():
            CONFIG[key] = value
        self._clear_env()

    def test_path_a_model_override(self):
        main = _KwargsClient(model="intern-s2-preview")
        rc = resolve_review_client(main)
        self.assertIsInstance(rc, ModelOverrideClient)
        self.assertEqual(rc.review_model_name, "intern-s1-pro")
        self.assertEqual(rc.review_budget.available(), 3)

    def test_plain_mock_client_without_env_degrades(self):
        # MockClient.chat 无 model/无 **kwargs，且未设 INTERN_REVIEW_MODEL：
        # 路径 A/B 均不成立 → None（与单模型行为一致）
        self.assertIsNone(resolve_review_client(MockClient(["x"])))

    def test_path_b_requires_explicit_env(self):
        # 只有 INTERN_API_KEY、没有 INTERN_REVIEW_MODEL：路径 B 不得自建
        # client（否则单元测试会在选手 shell 有 key 时误触真实网络）。
        os.environ["INTERN_API_KEY"] = "sk-test"
        self.assertIsNone(resolve_review_client(MockClient(["x"])))

    def test_same_model_skipped(self):
        main = _KwargsClient(model="intern-s1-pro")
        self.assertIsNone(resolve_review_client(main))

    def test_disabled_by_config(self):
        CONFIG["enable_review_model"] = False
        main = _KwargsClient()
        self.assertIsNone(resolve_review_client(main))

    def test_zero_budget_degrades(self):
        CONFIG["review_max_calls_per_problem"] = 0
        self.assertIsNone(resolve_review_client(_KwargsClient()))

    def test_resolution_never_raises(self):
        self.assertIsNone(resolve_review_client(None))
        self.assertIsNone(resolve_review_client(object()))


class TestMainModelName(unittest.TestCase):
    def test_reads_common_attrs(self):
        self.assertEqual(_main_model_name(_KwargsClient(model="intern-s2-preview")),
                         "intern-s2-preview")
        self.assertEqual(_main_model_name(object()), "")


class _DepsHelper:
    @staticmethod
    def build(main, rc=None):
        return Deps(client=main, review_client=rc)


class TestReviewCallFallback(unittest.TestCase):
    def _make_deps(self, main, rc):
        from utils.review_model import _ReviewBudget
        if rc is not None and getattr(rc, "review_budget", None) is None:
            rc.review_budget = _ReviewBudget(3)
            rc.review_model_name = "intern-s1-pro"
        return _DepsHelper.build(main, rc)

    def test_review_success_uses_review_answer(self):
        # 生产形态：路径 A 的 ModelOverrideClient 包着官方 client（模型在
        # 每次调用时注入）。复核成功时必须采用复核模型的答案，且主 client
        # 零调用。
        main = MockClient(["main answer"])
        inner = _ExplicitModelClient()
        rc = ModelOverrideClient(inner, "intern-s1-pro")
        deps = self._make_deps(main, rc)
        result = chat_with_retry_review(
            deps, [{"role": "user", "content": "q"}], label="arbiter_plain")
        self.assertEqual(result, "explicit ok")
        self.assertEqual(inner.calls[-1]["model"], "intern-s1-pro")
        self.assertEqual(len(main.calls), 0)

    def test_review_failure_falls_back_to_main(self):
        main = MockClient(["main answer"])
        rc = _FailingClient()
        deps = self._make_deps(main, rc)
        result = chat_with_retry_review(
            deps, [{"role": "user", "content": "q"}], label="arbiter_plain")
        self.assertEqual(result, "main answer")
        self.assertEqual(len(main.calls), 1)

    def test_no_review_client_uses_main(self):
        deps = _DepsHelper.build(MockClient(["main answer"]))
        result = chat_with_retry_review(
            deps, [{"role": "user", "content": "q"}], label="arbiter_plain")
        self.assertEqual(result, "main answer")

    def test_budget_exhausted_falls_back(self):
        from utils.review_model import _ReviewBudget
        main = MockClient(["main answer"])
        rc = _KwargsClient(answers=["review answer"])
        rc.review_budget = _ReviewBudget(1)
        rc.review_model_name = "intern-s1-pro"
        deps = _DepsHelper.build(main, rc)
        first = chat_with_retry_review(
            deps, [{"role": "user", "content": "q"}], label="arbiter_plain")
        second = chat_with_retry_review(
            deps, [{"role": "user", "content": "q"}], label="arbiter_plain")
        self.assertEqual(first, "review answer")
        self.assertEqual(second, "main answer")  # 配额用尽 → 主 client
        self.assertEqual(len(rc.calls), 1)

    def test_unsupported_review_falls_back_quickly(self):
        main = MockClient(["main answer"])
        rc = ModelOverrideClient(_NoModelClient(), "intern-s1-pro")
        deps = self._make_deps(main, rc)
        result = chat_with_retry_review(
            deps, [{"role": "user", "content": "q"}], label="arbiter_plain")
        self.assertEqual(result, "main answer")

    def test_prefill_review_stitches_prefix(self):
        # prefill 复核：review client 返回 "：C_ABC" 时，stitch 应还原出
        # "SELECT：C_ABC" 之类的完整前缀（此处用 "答案：" 前缀验证语义）。
        main = MockClient(["答案：main"])
        rc = _KwargsClient(answers=["：review"])
        deps = self._make_deps(main, rc)
        result = chat_prefilled_review(
            deps, [{"role": "user", "content": "q"}],
            prefix="答案", max_tokens=96, label="objective_revote")
        self.assertEqual(result, "答案：review")

    def test_prefill_review_failure_falls_back_to_main_prefill(self):
        main = MockClient(["答案：main"])
        rc = _FailingClient()
        deps = self._make_deps(main, rc)
        result = chat_prefilled_review(
            deps, [{"role": "user", "content": "q"}],
            prefix="答案", max_tokens=96, label="objective_revote")
        self.assertEqual(result, "答案：main")

    def test_review_client_helper_respects_budget(self):
        from utils.review_model import _ReviewBudget
        main = MockClient(["x"])
        rc = _KwargsClient()
        rc.review_budget = _ReviewBudget(0)
        deps = _DepsHelper.build(main, rc)
        self.assertIsNone(review_client(deps))  # 配额 0 → 视为不可用


if __name__ == "__main__":
    unittest.main()
