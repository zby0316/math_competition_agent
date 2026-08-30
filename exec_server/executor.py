"""Python 代码执行器：为数学解题提供隔离子进程执行环境。

`execute_python` 是执行主入口（子进程隔离）；`_build_mcp_server` 将执行器封装成
MCP server 供独立启动。进程内调用方直接 import `execute_python` 即可；
`_exec_in_process` 是没有子进程可用时的受限本地回退。
"""
import os
import re
import sys
import time
import tempfile
import subprocess
from typing import Optional

# 输出上限：LLM 生成的代码可能打印海量数据（如完整矩阵、大枚举），capture_output
# 会把全部内容读进内存。这里把 stdout/stderr 重定向到临时文件，读完只保留上限字节，
# 既防内存被打爆，又保留"最终答案:"标记的抽取。
_STDOUT_LIMIT = 64 * 1024  # 64 KB
_STDERR_LIMIT = 8 * 1024   # 8 KB


def _extract_answer_from_output(stdout: Optional[str]) -> Optional[str]:
    """取"最终答案"标记之后的答案文本；标记缺失返回 None。

    缺失标记时返回 None 是有意为之：Python prompt 要求程序以
    `print("最终答案:", answer)` 收尾，标记缺失说明程序没走到自己的结论——通常
    是生成中途被截断。旧回退会改用 stdout 的最后一行，曾把一行中间计算日志
    （"k=6: gcd of b^(2^k)-1 for odd b up to 49 is 256 = 2^8"）当成候选答案送进
    仲裁。未完成的程序什么也没验证，显式返回 None 让调用方重试，而不是把日志
    行当答案计分。
    """
    if not stdout:
        return None
    # 取最后一个"最终答案:"标记到 stdout 末尾的全部内容——SymPy Matrix/子群列表等
    # 多行输出必须完整保留（单行截断曾导致 final_response 只剩 "(Matrix([" 碎片）。
    markers = list(re.finditer(r"最终答案[：:][ \t]*", stdout))
    if not markers:
        return None
    answer = stdout[markers[-1].end():].strip()
    # 容错解码（errors="replace"）在截断处产生的 U+FFFD 只可能是残缺垃圾，
    # 从首尾剥掉，避免污染答案匹配。
    answer = answer.strip("\ufffd").strip()
    return answer if answer else None


def _read_bounded(path: str, limit: int) -> str:
    """从临时文件读取最多 limit 字节，超过则截断并追加省略标记。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            data = data[:limit] + "\n...[输出已截断]..."
        return data
    except Exception:
        return ""


def execute_python(code: str, timeout: int = 30) -> dict:
    """extension tool: run Python code in an isolated subprocess.

    Returns {success, stdout, stderr, answer, execution_time}.
    """
    temp_file = None
    stdout_file = None
    stderr_file = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(code)
            temp_file = f.name
        # 输出重定向到临时文件（而非内存管道），从源头避免海量输出吃爆内存。
        stdout_fd = tempfile.NamedTemporaryFile(mode="w+", suffix=".out", delete=False, encoding="utf-8")
        stderr_fd = tempfile.NamedTemporaryFile(mode="w+", suffix=".err", delete=False, encoding="utf-8")
        stdout_file = stdout_fd.name
        stderr_file = stderr_fd.name
        stdout_fd.close()
        stderr_fd.close()
        start = time.time()
        # Minimal env: PATH (find python), HOME (~/.local site-packages for sympy/numpy),
        # LANG/LC_ALL (UTF-8 for Chinese output). No [FUNC] leaked.
        # PYTHONIOENCODING/PYTHONUTF8：Windows 上 LANG/LC_ALL 无效，子进程写管道默认用
        # ANSI 代码页——在 GBK 机器上与父进程的 UTF-8 解码必然错配，显式钉死 UTF-8。
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        with open(stdout_file, "wb") as so, open(stderr_file, "wb") as se:
            result = subprocess.run(
                [sys.executable, temp_file],
                stdout=so, stderr=se, timeout=timeout, env=env,
            )
        execution_time = time.time() - start
        stdout = _read_bounded(stdout_file, _STDOUT_LIMIT)
        stderr = _read_bounded(stderr_file, _STDERR_LIMIT)
        return {
            "success": result.returncode == 0,
            "stdout": stdout,
            "stderr": stderr,
            "answer": _extract_answer_from_output(stdout),
            "execution_time": execution_time,
        }
    except subprocess.TimeoutExpired as e:
        # kill 后 subprocess.run 会把已回收的部分输出塞进异常对象；超时前打印的
        # "最终答案:"（如 LLM 代码先出答案再陷入死循环/长绘图）从这里救回。
        partial_stdout = ""
        if stdout_file and os.path.exists(stdout_file):
            partial_stdout = _read_bounded(stdout_file, _STDOUT_LIMIT)
        elif e.stdout is not None:
            partial_stdout = e.stdout if isinstance(e.stdout, str) else (
                (e.stdout or b"").decode("utf-8", errors="replace") if e.stdout else "")
        return {"success": False, "stdout": partial_stdout,
                "stderr": f"代码执行超时（>{timeout}秒）",
                "answer": _extract_answer_from_output(partial_stdout),
                "execution_time": float(timeout)}
    except Exception as e:
        return {"success": False, "stdout": "", "stderr": str(e),
                "answer": None, "execution_time": 0.0}
    finally:
        for p in (temp_file, stdout_file, stderr_file):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except Exception:
                    pass


# 受限 builtins 白名单：in-process 回退只暴露纯计算与基础类型操作，
# 禁止 os/subprocess/open/eval/exec/__import__ 等，杜绝读取任意文件或逃逸沙箱。
_SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "divmod": divmod, "enumerate": enumerate, "filter": filter, "float": float,
    "int": int, "len": len, "list": list, "map": map, "max": max, "min": min,
    "pow": pow, "print": print, "range": range, "reversed": reversed,
    "round": round, "set": set, "slice": slice, "sorted": sorted, "str": str,
    "sum": sum, "tuple": tuple, "zip": zip,
}


def _exec_in_process(code: str, timeout: int = 30) -> dict:
    """Fallback: exec code in a restricted in-process namespace (no subprocess).

    仅作为子进程不可用时的最后兜底。namespace 使用受限 builtins 白名单
    （不含 os/subprocess/open/eval/exec/__import__），线程超时无法硬杀，
    因此只用于极简纯计算，且不暴露任何文件系统或进程能力。
    """
    import io
    import contextlib
    import threading

    namespace = {"__builtins__": _SAFE_BUILTINS}
    for mod in ("sympy", "numpy", "math", "scipy"):
        try:
            namespace[mod] = __import__(mod)
        except Exception:
            pass

    buf = io.StringIO()
    done = {"result": None}

    def _run():
        try:
            with contextlib.redirect_stdout(buf):
                exec(code, namespace)
            done["result"] = ("ok", buf.getvalue(), "")
        except Exception as e:
            done["result"] = ("err", buf.getvalue(), str(e))

    t = threading.Thread(target=_run, daemon=True)
    start = time.time()
    t.start()
    t.join(timeout)
    elapsed = time.time() - start
    if t.is_alive():
        return {"success": False, "stdout": buf.getvalue()[: _STDOUT_LIMIT],
                "stderr": f"代码执行超时（>{timeout}秒）",
                "answer": _extract_answer_from_output(buf.getvalue()),
                "execution_time": float(timeout)}
    status, stdout, stderr = done["result"]
    return {"success": status == "ok", "stdout": stdout[:_STDOUT_LIMIT], "stderr": stderr,
            "answer": _extract_answer_from_output(stdout), "execution_time": elapsed}


# ---- extension server (standalone use) ----
def _build_mcp_server():
    from extension.server.fastmcp import FastMCP
    server = FastMCP("python_executor")

    @server.tool()
    def execute_python_tool(code: str, timeout: int = 30) -> dict:
        """在隔离子进程中执行一段Python代码，返回stdout/stderr及抽取的最终答案。"""
        return execute_python(code, timeout)

    return server


try:
    mcp_server = _build_mcp_server()
except Exception:
    mcp_server = None


if __name__ == "__main__":
    if mcp_server is not None:
        mcp_server.run()
    else:
        print("FastMCP server unavailable; execute_python still callable as a function.")
