"""python_executor 客户端：仅允许可终止的子进程执行路径。"""
from exec_server.executor import execute_python as _exec


class ExecClient:
    """调用可终止的子进程执行路径；失败保持证据不确定而不是执行 ``exec``。"""

    def __init__(self, mcp_session=None):
        # mcp_session 仅为与计划中的接口保持一致而保留；进程内模式会忽略它。
        self.mcp_session = mcp_session

    def execute(self, code: str, timeout: int = 30) -> dict:
        try:
            result = _exec(code, timeout)
            result["execution_backend"] = "mcp"
            return result
        except Exception as exc:
            return {"success": False, "stdout": "", "stderr": str(exc), "answer": None,
                    "execution_time": 0.0, "execution_backend": "subprocess_unavailable"}
