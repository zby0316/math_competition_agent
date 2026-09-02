"""python_executor MCP 服务器的客户端（进程内调用）+ 本地兜底。"""
from exec_server.executor import execute_python as _exec, _exec_in_process as _exec_in_proc


class ExecClient:
    """调用 execute_python（子进程路径 = MCP 工具），带进程内执行兜底。

    MCP 服务器的工具函数以进程内方式调用（无需 MCP 传输）；这样调用保持同步
    且并发安全（每次调用 = 独立子进程）。子进程路径抛异常时回退到进程内执行。
    """

    def __init__(self, mcp_session=None):
        # mcp_session 仅为与计划中的接口保持一致而保留；进程内模式会忽略它。
        self.mcp_session = mcp_session

    def execute(self, code: str, timeout: int = 30) -> dict:
        try:
            result = _exec(code, timeout)
            result["execution_backend"] = "mcp"
            return result
        except Exception:
            result = _exec_in_proc(code, timeout)
            result["execution_backend"] = "local_fallback"
            return result
