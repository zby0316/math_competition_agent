"""助手预填解码：真正能压掉这个模型延迟的唯一杠杆。

曾于 intern-s2-preview-397b 上以相同提示实测：

  classification 普通   max_tokens=8192 → 40.4s, 1969 个补全 token
  classification 预填   max_tokens=64   →  0.9s,   12 个补全 token  (58×)
  arbitration    普通   max_tokens=8192 → 70.2s, ~2000 个补全 token
  arbitration    预填   max_tokens=64   →  0.8s,    4 个补全 token  (140×)

机理：在助手回合种下答案的开头字符，把模型带进续写模式，它就不会再打开
`reasoning_content` 块。精度不受影响——预填分类 4/4 全对、预填仲裁 3/3，
其中包含一对对抗样本：*漂亮*的答案是错的，*朴素*的才是对的。

对比最初方案——截断 `max_tokens`：因为 CoT 先计费，小上限会在思考中途被截断，
答案根本不会输出。在 64/128/256/512 各种上限下，
11 次探针调用全只返回残缺的 `Thinking Process:`。预填是去掉 CoT 而不是一刀
切掉，所以它可以放心配上很小的预算。

兼容性：这只是在最后追加一条 assistant 消息，因此能走平台注入的
`client.chat(messages=...)`，无需改客户端。不同后端对尾部 assistant 回合的
处理不一样——续写、原样回显、或忽略——所以 `stitch` 接受三种形态，调用方
对直接拒绝预填的后端保留非预填兜底。
"""

from __future__ import annotations


def prefill_messages(messages: list[dict], prefix: str) -> list[dict]:
    """追加压制推理的助手种子回合。"""
    return list(messages) + [{"role": "assistant", "content": prefix}]


def stitch(prefix: str, completion: str) -> str:
    """从种子和返回内容重建完整的助手文本。

    处理三种已观测到的后端行为：
      * 续写   —— 完成文本接在 `prefix` 之后 → 直接拼接
      * 回显   —— 完成文本已重新陈述 `prefix` → 原样使用
      * 忽略   —— 完成文本是独立完整答案 → 原样使用

    仅靠前缀匹配区分回显与续写不可靠（续写也可能恰好以相同字符开头），所以
    只要完成文本在开头附近*包含*前缀，也算匹配。
    """
    body = completion if isinstance(completion, str) else str(completion or "")
    seed = prefix if isinstance(prefix, str) else str(prefix or "")
    if not seed:
        return body
    stripped = body.lstrip()
    if stripped.startswith(seed):
        return stripped
    # 回显前面带了包装文本（如多余换行或代码围栏）。
    head = body[: len(seed) + 40]
    if seed in head:
        return body[body.index(seed):]
    return seed + body
