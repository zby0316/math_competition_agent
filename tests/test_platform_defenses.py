"""平台 0 分防线回归测试（移植自启元真机教训）。

覆盖：sys.path 自举（importlib 路径加载形态）、exec 无 __file__ 沙箱形态、
solve 签名全兼容、client 响应全形态归一化、chat 签名探测降级。
"""
import importlib.util
import os
import sys
import unittest

from utils.response_normalize import chat_compatible, normalize_chat_response


class TestResponseNormalize(unittest.TestCase):
    def test_all_shapes(self):
        self.assertEqual(normalize_chat_response("abc"), "abc")
        self.assertEqual(normalize_chat_response(b"abc"), "abc")
        self.assertEqual(normalize_chat_response(None), "")
        # OpenAI choices dict
        self.assertEqual(
            normalize_chat_response({"choices": [{"message": {"content": "hi"}}]}), "hi")
        # content blocks
        self.assertEqual(
            normalize_chat_response({"choices": [{"message": {
                "content": [{"type": "text", "text": "he"}, {"type": "text", "text": "llo"}]}}]}),
            "hello")
        # 顶层 blocks / bytes blocks
        self.assertEqual(normalize_chat_response([{"text": "x"}, "y"]), "xy")
        self.assertEqual(normalize_chat_response([b"ab"]), "ab")
        # 嵌套 message
        self.assertEqual(normalize_chat_response({"content": {"content": "nested"}}), "nested")

    def test_object_shape(self):
        class AgentMessage:
            content = [{"type": "text", "text": "obj"}]
        self.assertEqual(normalize_chat_response(AgentMessage()), "obj")


class _KwargsOnlyClient:
    def chat(self, messages=None, temperature=0.2, max_tokens=2048):
        return "ok-kwargs"


class _PositionalOnlyClient:
    def chat(self, messages, temperature, max_tokens):
        return "ok-pos"


class _MessagesOnlyClient:
    def chat(self, messages):
        return "ok-msg"


class TestChatCompatible(unittest.TestCase):
    def test_signature_probing(self):
        self.assertEqual(chat_compatible(_KwargsOnlyClient(), [], 0.2, 10), "ok-kwargs")
        self.assertEqual(chat_compatible(_PositionalOnlyClient(), [], 0.2, 10), "ok-pos")
        self.assertEqual(chat_compatible(_MessagesOnlyClient(), [], 0.2, 10), "ok-msg")

    def test_mode_cached(self):
        from utils.response_normalize import _CHAT_MODE_CACHE
        client = _MessagesOnlyClient()
        chat_compatible(client, [], 0.2, 10)
        self.assertEqual(_CHAT_MODE_CACHE.get(client), "messages_only")
        # 缓存命中直接成功
        self.assertEqual(chat_compatible(client, [], 0.2, 10), "ok-msg")
        # 合规：缓存不得写到 client 实例上（官方要求不修改 client 属性）
        self.assertFalse(hasattr(client, "_veritas_chat_mode"))


class TestSolveSignatureTolerance(unittest.TestCase):
    def test_solve_single_arg_and_aliases(self):
        from user_agent import ReasoningAgent
        from utils.deps import MockClient
        agent = ReasoningAgent(client=MockClient(["x" * 10]))
        for call in (
            lambda: agent.solve("1+1=?"),
            lambda: agent.solve("1+1=?", {"idx": 1}),
            lambda: agent.solve("1+1=?", metadata={"idx": 1}),
            lambda: agent("1+1=?"),
            lambda: agent.run("1+1=?"),
        ):
            out = call()
            self.assertIsInstance(out, dict)
            self.assertIsInstance(out.get("final_response"), str)
            self.assertTrue(out["final_response"].strip())
            import json
            json.dumps(out, ensure_ascii=False)


class TestSandboxLoadingShapes(unittest.TestCase):
    """平台加载器形态门禁：按路径 importlib 加载 + exec 无 __file__ 沙箱加载。"""

    def _load_by_path(self, add_to_syspath: bool):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "user_agent.py")
        spec = importlib.util.spec_from_file_location("ua_shape_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_load_by_path_without_syspath(self):
        module = self._load_by_path(add_to_syspath=False)
        self.assertIn(module._BACKEND, ("graph", "fallback"))
        agent = module.ReasoningAgent(client=None)
        out = agent.solve("1+1=?")
        self.assertTrue(out["final_response"].strip())

    def test_exec_without___file__(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "user_agent.py")
        source = open(path, encoding="utf-8").read()
        namespace = {"__name__": "ua_exec_sandbox"}  # 故意不设 __file__
        exec(compile(source, path, "exec"), namespace)  # 不得抛异常
        agent = namespace["ReasoningAgent"](client=None)
        out = agent.solve("1+1=?")
        self.assertTrue(out["final_response"].strip())

    def test_harshest_exec_no_file_foreign_cwd(self):
        """最严苛形态（启元 0 分根因实证）：exec 无 __file__ 且 CWD 不在仓库目录。

        此时 sys.path 自举双候选全失效，核心图必然加载失败，唯一生机是
        fallback——而 fallback 必须完全自包含（不得 import 任何项目模块），
        否则 fallback 里 import utils 同样失败、兜底形同虚设。
        """
        import tempfile
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "user_agent.py")
        repo = os.path.dirname(path)
        source = open(path, encoding="utf-8").read()

        class _FakeClient:
            def chat(self, messages=None, temperature=0.2, max_tokens=2048):
                return "计算得 8*9=72。\n最终答案：72"

        saved_cwd = os.getcwd()
        saved_path = list(sys.path)
        saved_modules = {m: sys.modules[m] for m in list(sys.modules)
                         if m == "user_agent" or m.startswith(
                             ("nodes", "utils", "state", "graph"))
                         or m in ("config", "langgraph_math_agent", "llm_client")}
        # importlib 路径缓存：chdir 后 sys.path 里的相对条目（'.'）仍命中
        # chdir 前解析的绝对路径缓存——必须清掉，否则 exec 时 from
        # langgraph_math_agent import ... 会命中 ROOT 的缓存而误判 graph。
        try:
            import importlib
            importlib.invalidate_caches()
            importlib._bootstrap_external._path_importer_cache.clear()  # noqa: SLF001
        except Exception:  # noqa: BLE001 - 清理失败不致命，后续 exec 兜底
            pass
        try:
            os.chdir(tempfile.mkdtemp())
            if repo in sys.path:
                sys.path.remove(repo)
            for m in saved_modules:
                sys.modules.pop(m, None)
            namespace = {"__name__": "ua_harshest"}  # 不设 __file__
            exec(compile(source, path, "exec"), namespace)
            self.assertEqual(namespace.get("_BACKEND"), "fallback")
            agent = namespace["ReasoningAgent"](client=_FakeClient())
            out = agent.solve("计算 8*9 的值。", {"idx": 0})
            # fallback 必须兜出真答案（自包含归一化），不是兜底文案
            self.assertIn("72", out["final_response"])
        finally:
            os.chdir(saved_cwd)
            sys.path[:] = saved_path
            sys.modules.update(saved_modules)


if __name__ == "__main__":
    unittest.main()
