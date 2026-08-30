"""节点墙钟上限，按单题截止时间收敛。

以前是死代码：没有任何地方调用 `run_with_timeout`，所以 `CONFIG["node_timeouts"]`
形同虚设，所有节点都没有上界。这正是某道题曾在嵌套传输重试里卡住、耗掉
1307s 也没人叫停的原因。

现在两层配合：
  * CONFIG 里的 ceiling 接住单个节点的真正挂起；
  * TimeBudget.timeout_for() 把这个 ceiling 压到单题 20 分钟截止时间还允许的
    范围，迟到的节点不至于拖垮整轮。

被取消的 future 所在线程实际上杀不掉，所以超时的调用会一直在后台跑到套接字
超时触发。这可以接受：数量受节点数限制，而替代方案是整个平台截止时间泡汤。
重要的是*图*继续前进并仍能输出答案。
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Callable, TypeVar

from config import CONFIG

T = TypeVar("T")


class NodeTimeoutError(TimeoutError):
    pass


def get_node_timeout(node_name: str, default: int | None = None) -> int | None:
    # 每次调用都现读 CONFIG，而不是导入时快照，这样测试与运行时对
    # CONFIG["node_timeouts"] 的覆盖才能生效。
    return CONFIG["node_timeouts"].get(node_name, default)


def resolve_timeout(node_name: str, time_budget=None, default: int | None = None):
    """节点的实际上限：配置值按截止时间收敛。"""
    ceiling = get_node_timeout(node_name, default)
    if time_budget is None:
        return ceiling
    return time_budget.timeout_for(ceiling)


def run_with_timeout(func: Callable[[], T], timeout: int | float | None) -> T:
    if timeout is None:
        return func()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(func)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            future.cancel()
            raise NodeTimeoutError(f"operation timed out after {timeout:.0f}s") from exc
    finally:
        # 绝不在仍运行的 worker 上阻塞：shutdown(wait=True) 会重新引入本守卫
        # 要防止的卡顿。
        executor.shutdown(wait=False)
