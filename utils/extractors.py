"""答案提取器 (多层回退策略)"""
import re


def extract_final_answer(text: str) -> str:
    """从LLM输出中提取最终答案 — 多层降级"""
    if not text:
        return ""

    # 1. ANSWER: 标记 (最高优先级)
    m = re.search(r'ANSWER\s*[：:]\s*(.+?)(?:\n|$)', text, re.IGNORECASE)
    if m:
        ans = m.group(1).strip()
        if ans and len(ans) < 500:
            return _clean_answer(ans)

    # 2. 最终答案/答案是 标记
    for pat in [
        r'最终答案[：:]\s*(.+?)(?:\n|$)',
        r'答案是[：:]\s*(.+?)(?:\n|$)',
        r'答案为[：:]\s*(.+?)(?:\n|$)',
        r'结果为[：:]\s*(.+?)(?:\n|$)',
    ]:
        m = re.search(pat, text)
        if m:
            ans = m.group(1).strip()
            if ans and len(ans) < 300:
                return _clean_answer(ans)

    # 3. LaTeX boxed
    m = re.search(r'\\boxed\{([^}]+)\}', text)
    if m:
        return _clean_answer(m.group(1).strip())

    # 4. 证毕/结论
    for pat in [
        r'■\s*证毕[。，]?\s*(.+?)(?:\n|$)',
        r'结论[：:]\s*(.+?)(?:\n|$)',
        r'综上[所]*述[，,]\s*(.+?)(?:\n|$)',
    ]:
        m = re.search(pat, text)
        if m:
            ans = m.group(1).strip()
            if ans and len(ans) < 200:
                return _clean_answer(ans)

    # 5. 末行提取 (非过长行)
    lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
    for line in reversed(lines):
        # 过滤纯步骤编号/工具标注行
        if re.match(r'^(步骤|Step|\d+[\.\)]|【工具)', line):
            continue
        if 1 < len(line) < 200 and not line.startswith('#'):
            return _clean_answer(line)

    # 6. 最后数字兜底
    nums = re.findall(r'[+-]?\d+(?:\.\d+)?', text)
    if nums:
        return nums[-1]

    return "未能提取答案"


def _clean_answer(ans: str) -> str:
    """清洗答案"""
    # 去除首尾标点
    ans = ans.strip().strip('，,。.：:；;、 ')
    # 去除LaTeX包装
    ans = re.sub(r'^\\boxed\{(.+)\}$', r'\1', ans)
    # 去除过多空格
    ans = re.sub(r'\s+', ' ', ans)
    return ans
