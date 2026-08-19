import logging
from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class Deps:
    client: Any
    skills_loader: Any = None
    mcp_client: Any = None
    token_budget: Any = None
    #: Per-problem wall-clock budget (utils.time_budget.TimeBudget). Optional so
    #: unit tests can omit it; every consumer treats None as "unbounded".
    time_budget: Any = None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("math_agent"))

def get_deps(config: dict) -> "Deps":
    return config["configurable"]["deps"]

class MockClient:
    """Test client that returns preset responses in order.

    并发图（solving 子图 fan_out）下共享顺序响应存在竞态（reasoning 与
    python 节点并发消费 pop(0) 时序不定）。为此支持**关键词路由**：
    ``route(keyword, response)`` 注册的特征词命中 prompt 时优先生效；
    未命中才按 pop(0) 顺序兜底（兼容既有单线程测试）。
    """
    def __init__(self, responses=None):
        self.responses = list(responses) if responses else []
        self.routes = {}
        self.calls = []
    def route(self, keyword, response):
        self.routes[keyword] = response
        return self
    def chat(self, messages, temperature=0.2, max_tokens=4096):
        self.calls.append({"messages": messages, "temperature": temperature, "max_tokens": max_tokens})
        prompt = " ".join(
            str(m.get("content") or "") for m in messages if isinstance(m, dict))
        for keyword, response in self.routes.items():
            if keyword and keyword in prompt:
                return response
        if self.responses:
            return self.responses.pop(0)
        return "mock response"
