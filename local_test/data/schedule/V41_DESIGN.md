# V4.1：规则可判定题代码接管（L1 层）设计文档

日期：2026-08-25
分支：v33（本地，不推送）
状态：已实现 + 回放验证 + 单元测试 25 通过

## 1. 背景与动机

### 1.1 数据事实（本地回测）
- Omni-MATH 4428 题中 **2816 题（64%）是"规则可判定"**（Omni-MATH-Rule 论文
  定义：答案形态为闭式数值/简单表达式，可被程序化判定器校验）。
- 我们 4 轮共用的 321 题中 182 题（57%）规则可判定；**当前错题本 58 题中
  34 题（59%）规则可判定**，且 34 题的答案 100% 是闭式（16 纯数值 +
  18 LaTeX 表达式，无一文本答案）。
- → 这些题是"代码精确计算 + 确定性对拍"的理想目标。

### 1.2 失败模式（round3 实证）
round3 错题本 58 题：42 题计算-数值型（72%），验证状态 34 题
uncertain_arbitrating（59%）——**LLM 仲裁选错候选是主要失败模式**。

### 1.3 合规约束
- 官方测试环境**无 rule 文件、无调度标注** → `is_rule_judgeable` 返回 None →
  全部接管逻辑不触发，行为与 V4.0 完全一致（零风险）。
- rule 文件只用于判定"答案形态是闭式"，**绝不读取 answer 字段**；求解器
  state 不含官方答案（run_tests 只把 answer 放进判分 record）。

## 2. 回放校准过程（关键实证）

对 round3 的 34 道规则可判定错题做了三轮离线回放，逐轮修正设计：

| 版本 | 接管规则 | 结果 | 结论 |
|---|---|---|---|
| v1 | 双闭式数值等价→锁 python | **6/6 全错**（#0 '2'≠'3'、#103 '5/9'≠'2/3'…） | ❌ 推理与 Python 同源（都是 397B 生成），会"一致地错"——等价≠正确 |
| v2 | 推理非闭式+python 闭式+success→锁 | 12 次触发 **9 次选错**（#13/#15/#18/#34/#35/#42/#72/#76/#104） | ❌ 模型写的代码本身就算错，"单份代码闭式"不是正确性证据 |
| v3（定稿） | 未 match 的规则可判定题 + python 闭式+support → **强制第二独立代码**，双代码等价才锁 | 救回 4（#2/#14/#27/#66），16 题 py 错不锁（不更差），3 题同源同错（能力边界），11 题 py 不可用 | ✅ 唯一可靠确定性证据 = **两份独立方法代码等价**（AIMO2 路线） |

### 关键教训
1. **同源陷阱**：推理与 Python 都是同一个 397B 生成的，可能沿同一个错误
   方向推导/编码 → 双闭式"一致"经常是"一致地错"。
2. **单代码不可信**：模型写的 Python 代码本身就有 ~75% 的错误率（9/12），
   仅"执行成功 + 自检 PASS"不足信。
3. **独立方法双验证**：不同方法的代码独立地算错且结果一致的概率极低，
   是规则可判定题唯一可靠的确定性证据。

## 3. V4.1 实现

### 3.1 `utils/rule_judge.py`（新增）
- `is_rule_judgeable(problem)`：rule 文件 problem 文本匹配 → True/False/None
  （None = 无文件或未收录，调用方按 False 处理）
- `state_is_rule_judgeable(state)`：调度端标注（round_manager 写入的
  rule_judgeable 字段）优先，回退文本匹配
- `is_closed_form_answer(text)`：复用 AnswerMatcher 的 LaTeX→sympy 解析管线，
  判定"可解析 + 无自由变量"（含**文本层规模预检**：`2014!` 大阶乘在解析前
  拒绝——sympify 会真算天文数字卡死 7s+）
- `answers_numerically_equivalent(a, b)`：复用 `AnswerMatcher._sympy_equivalent`
  三级机制（符号化简→数值→采样），带规模守卫

### 3.2 `nodes/cross_validator_node.py`
- `_rule_judgeable_wants_double_verify(state, config, python_output, reasoning)`
  → bool：规则可判定 + computation/fill + python 闭式 + evidence_status==support
  + 非伪造 + 非 skipped
- `_second_python_check(state, config, require_playoff=True)`：新增参数放宽
  playoff 前提（V4.1 路径 require_playoff=False）
- mismatch/uncertain 分支：仲裁前先触发规则可判定双验证，双代码等价 →
  `rule_judgeable_double_python_locked` 锁定（answer_locked=True,
  verify_gate=rule_judgeable_takeover）

### 3.3 `nodes/python_agent_node.py`
- `_wants_independent_solve`：规则可判定题一律强制独立求解（不注入推理候选）
- 独立求解模式追加**闭式输出条款**：必须用 sympy/Fraction 精确计算并输出
  闭式（禁止浮点近似），为下游对拍提供可解析候选

### 3.4 `local_test/round_manager.py`
- `load_rule_judgeable_set()` + 每轮输出记录加 `rule_judgeable` 字段（调度端
  标注，免运行时匹配）

### 3.5 `local_test/run_tests.py`
- metadata 注入 `rule_judgeable`（求解器 state 可读；answer 依旧只进判分）

### 3.6 测试
- `tests/test_rule_judgeable.py`：25 用例（闭式判定/等价对拍/接管决策/
  规模守卫），全通过
- 既有 217 用例无回归

## 4. 预期收益（round3 仿真）

- **救回 4/34 规则可判定错题**（#2 #14 #27 #66）= **+3.6 分/轮**
  （round3 48.2% → ~51.8%）
- 16 题 py 错：第二代码不一致 → 不锁 → 走原链路，**不更差**
- 3 题 match 出厂同源同错（#0 #96 #104）：V4.1 不触发，需第 3 层 SC 多采样
- 11 题 python 不可用（空/失败/非闭式）：V4.1 不触发
- 成本：每轮 ~20 次额外第二代码调用（每次 ~30-60s），受 remaining_hard>300s
  预算闸门保护

## 5. 后续（三层计划进度）

- [x] L1：规则可判定代码接管（本版本）
- [ ] L2：表达式/符号答案题专门化（答案带变量如 k!/d^r：符号化简对拍、
      特殊化求解提示）
- [ ] L3：规则可判定题的 SC 多采样（3-5 份独立求解投票，AIMO2 早停）——
      针对"同源同错"3 题与"py 不可用"11 题
- [ ] 判分侧：rule 文件可用于**确定性判分**（数值等价优先于 LLM 判分）
