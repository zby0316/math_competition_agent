"""数据清洗工具 (参考 0.24 丰富清洗逻辑)"""
import re


def clean_math_text(text: str) -> str:
    """清洗数学文本中的常见噪声"""
    if not text:
        return ""

    # 统一换行
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    # 移除零宽字符
    text = re.sub(r'[\u200B\u200C\u200D\uFEFF]', '', text)

    # 修复不匹配的括号
    opens = text.count('(') + text.count('[') + text.count('{')
    closes = text.count(')') + text.count(']') + text.count('}')
    if opens > closes:
        text += ')' * (opens - closes)

    # 移除重复公式
    text = re.sub(r'(\$[^$]+\$)\s*\1', r'\1', text)

    # 移除末尾残缺句
    text = re.sub(r'\s*[=≈→+\-\\]\s*$', '', text)

    # 清理多余空格
    text = re.sub(r'  +', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)

    # 移除LLM编造的数值结果（如 "计算得 x=5" 后面跟的数值）
    text = re.sub(r'[，,。]\s*(?:计算|解?求?)得[：:]?\s*[^，。\n]*?(?:[=≈→]|\b\d)[^，。\n]*', '', text)

    return text.strip()


def sanitize_math_expr(expr: str) -> str:
    """标准化数学表达式，使其兼容SymPy"""
    r = {
        '^': '**', '×': '*', '·': '*', '÷': '/',
        'π': 'pi', 'θ': 'theta', 'α': 'alpha', 'β': 'beta', 'γ': 'gamma',
        'φ': 'phi', 'ω': 'omega', 'λ': 'lambda',
        '−': '-', '∞': 'oo',
    }
    for old, new in r.items():
        expr = expr.replace(old, new)
    return expr


def extract_math_expr(content: str) -> str:
    """从文本中提取可计算的数学表达式 (参考0.24 5策略提取)"""
    # 策略1: LaTeX公式
    m = re.search(r'\$\$(.+?)\$\$|\$(.+?)\$', content)
    if m:
        inner = (m.group(1) or m.group(2)).strip()
        if inner and re.search(r'[a-zA-Z0-9]', inner):
            return sanitize_math_expr(inner)

    # 策略2: 等号表达式
    m = re.search(r'[：:]\s*([a-zA-Z0-9_\+\-\*\/\^\(\),\.\s]+?)=\s*([a-zA-Z0-9_\+\-\*\/\^\(\),\.\s]+?)(?=[，。,.\n]|$)', content)
    if m:
        left = sanitize_math_expr(m.group(1).strip())
        right = sanitize_math_expr(m.group(2).strip())
        if len(left) < 60 and len(right) < 60:
            return f"{left}={right}"

    # 策略3: "解得/计算得" 后面
    m = re.search(r'(?:解得|计算得|可得|结果为|代入得)[：:]?\s*([a-zA-Z0-9_\+\-\*\/\^\(\),\.\s]+?)(?=[，。,.\n]|$)', content)
    if m:
        return sanitize_math_expr(m.group(1).strip())

    # 策略4: 纯数值表达式
    m = re.search(r'(\d+[\+\-\*\/\^\(\)]+\d+)', content)
    if m:
        return m.group(1).replace('^', '**')

    return ""


def extract_numeric_expression(text: str) -> str:
    """从AI分析文本提取纯数值表达式 (0.24)"""
    # 计算式：xxx
    m = re.search(r'(?:计算式|表达式)[：:]\s*([^。\n]+)', text)
    if m:
        expr = m.group(1).strip()
        if not re.search(r'[a-zA-Z]', expr):
            return expr.replace('^', '**')

    # 代入...得
    m = re.search(r'(?:代入|得到)[：:]?\s*([0-9\+\-\*\/\^\(\)\.\s]+)', text)
    if m:
        return m.group(1).strip().replace('^', '**')

    return ""


def clean_problem_text(text: str) -> str:
    """清洗题目文本"""
    # 移除编号前缀
    text = re.sub(r'^\d+[\.\)、]\s*', '', text.strip())
    # 移除多余空白
    text = re.sub(r'\s+', ' ', text)
    return text.strip()
