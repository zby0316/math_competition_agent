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

    V2.5（det_solver 移植后）：``route(keyword, [r1, r2, ...])`` 支持
    **每关键词队列**——同一关键词被多次命中时按注册顺序逐个弹出，弹尽后
    返回兜底字符串。用于消除多轮调用（采样投票/重算轮）与并发扇出的
    响应竞态（det_solver 的本地计算改变了线程相对时序，顺序响应已不可靠）。
    """
    def __init__(self, responses=None):
        self.responses = list(responses) if responses else []
        self.routes = {}
        self.calls = []
    def route(self, keyword, response):
        if isinstance(response, (list, tuple)):
            self.routes[keyword] = list(response)  # 每关键词队列
        else:
            self.routes[keyword] = response
        return self
    def chat(self, messages, temperature=0.2, max_tokens=4096):
        self.calls.append({"messages": messages, "temperature": temperature, "max_tokens": max_tokens})
        prompt = " ".join(
            str(m.get("content") or "") for m in messages if isinstance(m, dict))
        for keyword, response in self.routes.items():
            if keyword and keyword in prompt:
                if isinstance(response, list):
                    return response.pop(0) if response else "mock response"
                return response
        if self.responses:
            return self.responses.pop(0)
        return "mock response"
