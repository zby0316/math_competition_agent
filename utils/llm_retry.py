"""Transport retry, bounded by the problem deadline rather than by attempt count.

Why the count alone is not enough: the platform injects its own client, and
`InternChatClient` already retries 3× internally at a 120s socket timeout. Three
outer attempts over that is up to 9 requests ≈ 1080s spent inside a single node —
enough to blow the 20-minute per-problem limit on its own, which is what the
2026-07-29 judge run's Q3 (1307s) actually spent its time on. We cannot lower the
inner count (it belongs to the platform's client), so instead every attempt must
ask the clock for permission first.
"""

import logging
import re
import time
from typing import Dict, List

from config import CONFIG
from utils.prefill import prefill_messages, stitch
from utils.response_normalize import (
    chat_compatible, detect_truncation, normalize_chat_response)


def thinking_mode_flag():
    """V2.6 截断根治：thinking_mode=False 确定性关"深度思考"（长 CoT）。

    截断根因是 CoT 计入 max_tokens、难题上吃光预算导致答案没写完。V2.4 用
    prefill 变相关 CoT 拿 59.82 最佳分（对比 V1 开 CoT 53.53），证明数学场景
    关 CoT 反而更好。官方原生 thinking_mode=False 比 prefill hack 更可靠。
    返回 False（关 CoT）或 None（不传，保持默认深度思考）；由 config 开关控制。
    """
    return False if CONFIG.get("enable_thinking_mode_off") else None


class DeadlineExceeded(RuntimeError):
    """Raised when the problem's time budget cannot fund another attempt."""


class LLMRetryWrapper:
    def __init__(
        self,
        client,
        max_retries: int = 3,
        backoff_factor: float = 2.0,
        logger=None,
        time_budget=None,
        expected_call_seconds: float | None = None,
        reserve_margin_s: float | None = None,
    ):
        self.client = client
        self.max_retries = max(1, max_retries)
        self.backoff_factor = backoff_factor
        self.logger = logger or logging.getLogger("math_agent")
        self.time_budget = time_budget
        self.expected_call_seconds = expected_call_seconds
        # reserve_margin_s: 允许本次调用动用 reserve（硬上限前的预留时间），但必须
        # 给后续的答案落盘留出 margin 秒。评委报告：64 道零分题里绝大多数
        # `arbiter=skipped(0.0s)` —— 软预算耗尽后仲裁被整体跳过，恰是最需要复核的
        # 场景。仲裁的 prefill 调用只要 ~10s，reserve(300s) 划出固定配额是净收益。
        self.reserve_margin_s = reserve_margin_s

    def _now(self) -> float:
        """Read the budget's own clock, so measured cost and remaining time agree.

        Mixing `time.monotonic()` here with the budget's clock made the observed
        duration meaningless whenever the two differed, which silently disabled the
        adaptive check that is supposed to stop a doomed retry.
        """
        now = getattr(self.time_budget, "now", None)
        return now() if callable(now) else time.monotonic()

    def _affordable(self, observed: float | None = None) -> bool:
        """Can the clock fund another attempt?

        `observed` is how long the previous attempt actually took. It matters: a
        transport failure that burned 363s (the client's own 3 x 120s retries) will
        almost certainly burn another 363s under the same load, so estimating the
        retry at the optimistic `expected_call_seconds` would let us start a call we
        cannot finish. Measured cost always wins over the estimate.
        """
        if not self.time_budget:
            return True
        estimate = self.expected_call_seconds
        if observed is not None:
            estimate = observed if estimate is None else max(estimate, observed)
        if self.reserve_margin_s is not None:
            # 预留配额模式：软预算已尽也允许，只要硬上限前还能容下
            # 本次调用 + margin。remaining_hard 不足时照样拒绝。
            window = self.time_budget.remaining_hard() - float(self.reserve_margin_s)
            return window >= (estimate if estimate is not None else 1.0)
        if estimate is None:
            return not self.time_budget.expired()
        return self.time_budget.can_afford(estimate)

    def chat(
        self,
        messages: List[Dict],
        temperature: float = 0.2,
        max_tokens: int = 4096,
        label: str = "llm",
        thinking_mode=None,
    ) -> str:
        if not self._affordable():
            need = (f"~{self.expected_call_seconds:.0f}s"
                    if self.expected_call_seconds is not None else "any time at all")
            raise DeadlineExceeded(
                f"{label}: {self.time_budget.remaining():.0f}s left, need {need}"
            )

        last_error = None
        for attempt in range(1, self.max_retries + 1):
            started = self._now()
            try:
                # VeritasMath 平台防线：签名探测（kwargs→位置参→仅messages）+
                # 响应归一化（str/bytes/choices/content blocks/对象形态 → str）。
                # 官方 client 形态见 utils/response_normalize.py 头注的 0 分教训。
                raw = chat_compatible(
                    self.client, messages, temperature, max_tokens, thinking_mode)
                result = normalize_chat_response(raw)
                if self.time_budget:
                    self.time_budget.record(label, self._now() - started)
                # 截断补救：finish_reason=length 时答案没写完，用已生成内容
                # prefill 续写补全（V2.4 官方 truncated=91 的根因是截断答案被
                # 当作完整答案返回）。只补一次，时间/预算不足则跳过。
                # 双通道检测：① finish_reason=length（官方 client 返回 dict 时
                # 可见）；② 启发式（client 只返回字符串时 finish_reason 不可见，
                # 用"写满 max_tokens + 结尾明显未完成"判定——真机实证本地 client
                # 返回纯 content 丢弃 finish_reason，仅靠①会漏检）。
                if result and result.strip() and (
                        detect_truncation(raw)
                        or _looks_truncated(result, max_tokens)):
                    # V2.6 优化：答案章节已完整写出（其后还出现别的章节）时，
                    # 截断只切到验证点/尾部——跳过续写，省 ~45-90s/次（官方
                    # V2.5 轮 203 次截断中相当比例属于此类）。答案未完整才续写。
                    if not _answer_section_complete(result):
                        result = self._continue_truncated(
                            messages, result, temperature, label, thinking_mode)
                return result
            except Exception as exc:  # noqa: BLE001 - retry transient transport failures.
                last_error = exc
                observed = self._now() - started
                if self.time_budget:
                    self.time_budget.record(f"{label}:failed", observed)
                if attempt >= self.max_retries:
                    break
                # A retry is only worth buying if the clock can still fund a call
                # that costs what the last one cost. Otherwise fail fast so the
                # caller reaches its fallback while there is time to emit it.
                if not self._affordable(observed):
                    self.logger.warning(
                        "%s: abandoning retry %s/%s after %.0fs, time budget cannot "
                        "fund another attempt (%s)",
                        label, attempt, self.max_retries, observed, exc,
                    )
                    break
                delay = 0 if self.backoff_factor == 0 else self.backoff_factor ** (attempt - 1)
                # V2 M6 限流感知退避：官方 client 的限流/配额错误（429、配额耗尽
                # -20081 等）说明"当前请求不该立即重发"，短退避只会连续撞墙浪费
                # 时间预算。检测到限流特征时用更长退避（≥10s），并降级重试次数
                # 语义——留给后续节点的时间比盲目重试更值钱。
                err_text = f"{exc}".lower()
                if any(k in err_text for k in ("429", "rate limit", "rate_limit",
                                               "配额", "quota", "tokens", "-20081",
                                               "busy", "overloaded", "too many")):
                    delay = max(delay, 10.0 + 5.0 * attempt)
                    self.logger.warning(
                        "%s: 检测到限流/配额特征(attempt %s/%s)，退避 %.0fs: %s",
                        label, attempt, self.max_retries, delay, exc)
                else:
                    self.logger.warning(
                        "LLM call failed on attempt %s/%s: %s", attempt, self.max_retries, exc
                    )
                if delay > 0:
                    time.sleep(delay)
        raise last_error

    def _continue_truncated(self, messages, result, temperature, label, thinking_mode=None):
        """finish_reason=length 时，用已生成内容 prefill 续写补全（最多 N 轮）。

        克制设计（区别于 V2.8 上调 token 反而 truncated 366 的教训）：
        - 续写用 prefill（assistant 前缀）抑制 CoT，不重新触发思考；
        - 只用结尾片段作前缀（超长前缀在慢 API 下会拖到 800s+ 逼近单题硬限）；
        - 预算不足（remaining_hard 或 can_afford 不满足）直接放弃，不冒险。
        - V2.6 从"只续一次"升级为"续到完整，最多 N 轮"（N=truncation_continue_max_rounds）：
          单次 2048 token 续写不够时（超长证明/多问项），循环续写直到完整或达到上限。
        """
        max_rounds = int(CONFIG.get("truncation_continue_max_rounds", 2))
        continue_tokens = CONFIG.get("truncation_continue_tokens", 2048)
        current = result
        try:
            for _round in range(max_rounds):
                if self.time_budget:
                    # 慢 API 下一次续写估 ~45s，硬上限前留 60s 余量
                    if getattr(self.time_budget, "remaining_hard", lambda: 0)() < 105:
                        return current
                    if not self._affordable():
                        return current
                tail = current[-120:] if len(current) > 120 else current
                raw2 = chat_compatible(
                    self.client, prefill_messages(messages, tail),
                    temperature, continue_tokens, thinking_mode)
                completion = normalize_chat_response(raw2)
                if not completion or not completion.strip():
                    return current
                stitched = stitch(tail, completion)
                # 续写产物去掉前缀种子后拼回原答案；若续写异常（空/退化）保守回退
                if stitched and stitched != tail:
                    current = (current + stitched[len(tail):]
                               if stitched.startswith(tail) else current + stitched)
                else:
                    return current
                # 续写又写满（还需续写）则继续下一轮，否则认为已补全。
                if not (detect_truncation(raw2)
                        or _looks_truncated(completion, continue_tokens)):
                    return current
            return current
        except Exception as exc:  # noqa: BLE001 - 续写失败保守返回原答案
            if self.logger:
                self.logger.warning("%s: truncation continue failed: %s", label, exc)
            return current


def _answer_section_complete(text: str) -> bool:
    """答案章节是否已完整写出（V2.6 截断续写决策用）。

    判据："## 最终答案" 之后还出现了别的 "## " 章节——说明答案章节已闭合，
    截断只发生在答案之后的验证点/尾部，续写收益≈0（省 45-90s/次）。
    答案章节缺失、或它是最后一个章节（可能被切在答案内部）→ False，照常续写。
    """
    m = re.search(r"## 最终答案\s+(.+?)(?=##|$)", text or "", re.DOTALL)
    if not m or not m.group(1).strip():
        return False
    return bool(re.search(r"## ", text[m.end():]))


def _looks_truncated(text: str, max_tokens: int) -> bool:
    """finish_reason 不可见时的截断启发式（真机实证：本地 client 只返回 content
    字符串，丢弃 finish_reason，仅靠 detect_truncation 会漏检）。

    判据分两层（宁可误判补写——续写从结尾继续、主体答案保留，漏判代价是残缺 0 分）：
    - 强信号（不依赖写满判据，命中即判截断）：未闭合 LaTeX（$ 奇数、{/} 不配对）、
      结尾反斜杠命令残片——正常答案几乎不会出现，出现即大概率截断。
    - 弱信号（依赖写满判据，即输出达到 max_tokens 量级）：结尾未完成标点
      （冒号/逗号/等号/空括号）、枚举空项、写满 1.5 倍以上。
    """
    if not isinstance(text, str) or max_tokens <= 0:
        return False
    s = text.rstrip()
    # —— 强信号：未闭合 LaTeX / 命令残片（不依赖写满判据）——
    if s.count("$") % 2 == 1:
        return True
    if s.count("{") > s.count("}"):
        return True
    if re.search(r"\\[a-zA-Z]+$", s):  # 结尾反斜杠命令残片（如 "\\alpha" 无闭合）
        return True
    # —— 弱信号：写满判据是前提 ——
    if len(s) < max_tokens * 1.2:
        return False
    tail = s[-60:]
    if re.search(r"[-:：,，、=＝]\s*$", tail):
        return True
    if re.search(r"[（(]\s*$", tail):
        return True
    if re.search(r"\\[a-zA-Z]*$", tail):
        return True
    if re.search(r"^\s*(?:\d+[.、)]|[-*•])\s*$", tail[-3:].lstrip()):  # 枚举项后无内容
        return True
    return len(s) >= max_tokens * 1.5



def chat_with_retry(
    client,
    messages,
    temperature=0.2,
    max_tokens=4096,
    logger=None,
    time_budget=None,
    expected_call_seconds=None,
    label="llm",
    thinking_mode=None,
):
    return LLMRetryWrapper(
        client,
        max_retries=CONFIG["llm_max_retries"],
        backoff_factor=CONFIG["backoff_factor"],
        logger=logger,
        time_budget=time_budget,
        expected_call_seconds=expected_call_seconds,
    ).chat(
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        label=label,
        thinking_mode=thinking_mode,
    )


def chat_prefilled(
    client,
    messages,
    prefix,
    temperature=0.1,
    max_tokens=96,
    logger=None,
    time_budget=None,
    expected_call_seconds=None,
    label="llm_prefill",
    max_retries=1,
    reserve_margin_s=None,
    thinking_mode=None,
):
    """One prefilled round trip, returning the stitched assistant text.

    Defaults to a single attempt: a prefilled call is the cheap fast path, and
    every caller has a full-budget non-prefill fallback behind it, so spending
    retries here would buy latency without buying reliability.

    ``reserve_margin_s`` 非 None 时，本调用允许动用 reserve（软预算已尽仍可
    执行），仅要求硬上限前剩余 ≥ 调用估时 + margin。用于仲裁保底与应急直答。

    ``thinking_mode``（V2.6）：官方原生关深度思考开关，透传给 chat_compatible。
    """
    wrapper = LLMRetryWrapper(
        client,
        max_retries=max_retries,
        backoff_factor=CONFIG["backoff_factor"],
        logger=logger,
        time_budget=time_budget,
        expected_call_seconds=expected_call_seconds,
        reserve_margin_s=reserve_margin_s,
    )
    raw = wrapper.chat(
        messages=prefill_messages(messages, prefix),
        temperature=temperature,
        max_tokens=max_tokens,
        label=label,
        thinking_mode=thinking_mode,
    )
    return stitch(prefix, raw)
