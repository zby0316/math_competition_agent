# V2.5（git HEAD d396205）功能缺陷审计报告

- 审计对象：`C:\Users\34707\Desktop\math_competition_agent (1) - 副本`（LangGraph 多智能体数学竞赛求解器）
- 审计时间：以本报告落盘为准；代码未做任何修改
- 测试结果：`python -m pytest tests/ -q` → **154 passed, 30 subtests passed（全部通过，46.82s）**，仅 mpmath `bitcount` DeprecationWarning 与 requests/urllib3 版本提示（非功能性）
- 严重度口径：P0 致命（全题丢分/崩溃） / P1 高危（特定题类系统性丢分） / P2 中危（边缘情况） / P3 低危（潜在/防御性缺陷）

---

## 0. 并发模型结论（回答"并发 3 是进程还是线程"）

仓库自带的本地评测入口 `main.py`（第 115-121 行）**单进程、单 `ReasoningAgent` 实例、`asyncio.to_thread` 起 8 个线程共享该实例**；`utils/paper_pacer.py` 用进程级单例（`PaperPacer.get_instance`）、`user_agent.py` 用类级缓存（`ReasoningAgent._contract_probe_cache`）跨题共享，说明整个代码库就是按"**单进程多线程共用同一 agent 实例**"设计的。官方"并发 3"大概率同为多线程共享实例（或 3 进程但每进程独立实例，代码对两者都应安全）。

线程安全总体达标：`TokenBudget`/`TimeBudget`/`PaperPacer` 均带 `threading.Lock`；每个问题在 `MathAgentGraph.run` 内新建 `TimeBudget`/`TokenBudget`（`langgraph_math_agent.py` 96-100 行），题间隔离良好。但存在若干无锁懒初始化与超时僵尸线程问题（见 P3-3、P3-6、§7）。

---

## 1. P1 高危（特定题类系统性丢分）

### P1-1　客观题 `validated_answer` 丢弃 revote / judge_confirm 的修正结果，改判功能实际失效

**文件:行号**：`nodes/cross_validator_node.py:219`、`248`、`424-431`

**代码摘录**：
```python
# L219（复核投票分歧时换票）：
candidate = vote.get("adopted") or candidate
# L248（判断题双向确认反向时改判）：
candidate = jc.get("final_word") or candidate
...
# L424-431（最终落盘却从 reasoning_result 取原值，candidate 被丢弃）：
if match_result.get("method") == "objective_direct":
    validated_answer = normalize_objective_answer(
        reasoning_result.get("answer", ""), question_mode
    )
```

**触发场景**：客观题（choice/true_false/fill）走快速路径时，`candidate` 在 L191 初始化后可能被两处修改：① `_objective_revote` 分歧且换票方空位覆盖更全时（L219）；② `judge_confirm` 双向确认反向改判时（L248）。但最终 `validated_answer` 一律取 `reasoning_result.get("answer")` 的原值——**所有对 `candidate` 的修正都不会进入最终答案**。

**影响**：
- `utils/judge_confirm.py` 的"判断题双向确认改判"（针对 Intern-S2 系统性"否"偏向设计的旗舰机制）**功能死亡**：确认轮反向、温度 0 重解得到的正确判断被丢弃，仍输出首答（大概率是错的"否"）。
- 填空题复核投票"分歧时保留更完整候选"同样失效。
- 两条路径白花 ~30-60s LLM 调用与预算，只留下 trace 痕迹。

**修复建议**：L424-431 改为使用局部 `candidate`（它已含 revote/judge_confirm 修正）；或把修正写回 `reasoning_result` 再取。例如：
```python
if match_result.get("method") == "objective_direct":
    validated_answer = normalize_objective_answer(candidate, question_mode)
```

---

### P1-2　sympy 事前求解（V2.5 新功能）短路 LLM 时"答非所问"，整题以错答案高置信出厂

**文件:行号**：`nodes/reasoning_agent_node.py:350-394`（`_sympy_pre_solve`）；`utils/sympy_pre_solver.py:41-59`、`130-146`

**代码摘录**：
```python
# reasoning_agent_node.py L363：只排除 客观题/hard/多问项
if is_multi_ask(str(state.get("problem") or "")):
    return None
...
whole = (result or {}).get("whole_answer", "")
if not whole:
    return None
# 直接把解集当最终答案返回，跳过整个 LLM 推理
parsed = {... "answer": whole, "answer_source": "sympy_pre_solve"}
```
```python
# sympy_pre_solver.py L134-145：方程分支把"解集"当作 whole_answer
left, right = part.split("=", 1)
...
answer = "，".join(f"{variable.group(1)}={item}" for item in result)
hints.append({"operation": "equation_solve", "result": answer, "whole_goal": True})
```

**触发场景**：`sympy` 对任意含"方程/求解/积分/极限/导数"关键词、难度非 hard、非多问项的题，只要解析出一个 `$...$` 或裸等式片段就返回**解集/原式结果**作为最终答案。例如：
- "求方程 x²−5x+6=0 的**两根之和**" → 输出 `x=2，x=3`（应为 5）
- "求 x³−x=0 的**实数根的个数**" → 输出 `x=-1，x=0，x=1`（应为 3）
- "求 2x+3=7 的解" 类纯求解题无碍，但一切"解集→题面所问量"的投影关系都被忽略

`_sympy_pre_solve` 只排除了 `is_multi_ask`（依赖"并/及/也"等连接词正则），"两根之和/较大根/最小正整数解/根的个数"均不命中。难度由分类器 LLM 给出，大量 easy/medium 题会进入该路径；一旦命中，**确定性计算以高置信（`answer_source=sympy_pre_solve`）出厂，且 Python 分支若同样按解集理解则两路"一致地错"**，cross_validator 判 match 直接放行。

**影响**：V2.5 的核心提速特性在"解集非所求"题类上会系统性输出错误答案；且因为无 LLM 复核，比原先 LLM 可能答对的情况更差。

**修复建议**（任选）：
1. 只对**题面本身就是求解型**（"求方程…的解/求 x"）且无任何附加投影词（"的/之和/个数/较大/最小/个数"等）的题启用 `whole_answer` 短路；或
2. 把 `whole_answer` 降级为 `hints` 注入提示词而非直接作答案（保留提速的同时让 LLM 判断所问目标）；或
3. 对 `_congruence`/`equation_solve` 分支增加"题面问的正是该量"的强正则校验。

---

## 2. P2 中危（边缘情况丢分/错误）

### P2-1　`is_noise_answer` 把阶乘答案 "5!" 当作噪声丢弃

**文件:行号**：`utils/answer_cleanliness.py:97`

**代码摘录**：
```python
# 疑问/感叹收尾：结论不以问句结束。（允许内部含 '?' 的合法记号，如 "P(X>1)"）
if re.search(r"[？?！!]\s*$", s):
    return True
```
实测：`is_noise_answer("5!") == True`、`is_noise_answer("3!") == True`、`is_noise_answer("正确！") == True`。

**触发场景**：模型在"## 最终答案"下写出阶乘记法（如排列组合题答案 `5!`、`3!·2!` 的单行答案，或判断句带感叹号"正确！"）。`_distill_answer`（`reasoning_agent_node.py:201`）→ `_reject_bad_answer`（L109-117）→ `is_noise_answer` 判 True → 答案被置空，随后走无谓的 prefill 重试/压缩重试，或直接以空答案进交叉验证。

**影响**：阶乘形式答案是竞赛常出现的合法答案形态，会被系统性丢弃/触发昂贵的重试；"正确！"等带感叹号的判断题答案同样被吞。

**修复建议**：在 `is_noise_answer` 的感叹号检查前先排除纯数学形态，例如：
```python
if re.search(r"[？?]\s*$", s):          # 只保留问号；感叹号单独处理
    return True
if re.search(r"!\s*$", s) and not re.fullmatch(r"[-+]?[\d\w!*/+\-^().\s]+", s):
    return True
```

---

### P2-2　`_extremum_satisfied` 把纯数值极值答案判为契约残缺，触发无谓的 Critic 修复轮

**文件:行号**：`utils/answer_contract.py:48-65`（配合 `nodes/critic_node.py:134`、`coordinator_node.py:659`）

**代码摘录**：
```python
def _extremum_satisfied(answer):
    text = answer or ""
    if not _NUMERIC_OR_SYMBOLIC_RE.search(text):
        return False
    if re.search(r"(?:最大值|最小值|最值|极大值|极小值|最大|最小)[^。\n]{0,12}(?:为|是|=|等于)", text):
        return True
    # 否则要求至少两个独立量：极值点（如 a=1）与极值本身
    return len(re.findall(r"=", text)) >= 2 or answer_part_count(text) >= 2
```

**触发场景**：题面含"最大值/最小值"（`required_components` L133-139 几乎对所有极值题都会追加 `extremum_value` 契约），而答案是**单值**（"5"、"3/4"）。单值不满足上面的任何分支 → `missing_components` 报缺 → Critic `merge_with_deterministic` 判 `incomplete` → 进入 reconciliation 定向修复轮（浪费一次完整 solving 重跑，~500-900s），且重跑产出的新答案若与"5"不同，可能覆盖正确原值。

**影响**：大量"求 f(x) 的最大值"这类题的正确单值答案被判定残缺，白烧预算并有换错风险；对 1200s 单题硬限下的完成率是直接伤害。

**修复建议**：对"最大值/最小值"契约放宽——当题面没有"极值点+极值"双问特征（`required_components` 只在问"最大值是多少"时不必强制双量）时，单值可判满足；或把 `_extremum_satisfied` 改为"单值 + 题面无 '求…时/对应的…' 结构"即视为满足。

---

### P2-3　`_sample_vote_reasoning` 对 hard 题几乎无法凑齐 3 票，且单票可吃掉整题预算

**文件:行号**：`nodes/reasoning_agent_node.py:784-822`（触发条件 `_should_sample_vote` L764-781）

**代码摘录**：
```python
for t in temps:
    if clock and clock.remaining_hard() < 60:
        break
    out = _reasoning_agent_impl({**state, "sample_temperature": t}, config)  # 完整推理，含重试/压缩通道
```
hard 难度 soft_total=1200、reserve=300 → 软窗口只有 ~900s；单次 `_reasoning_agent_impl`（max_tokens 16384 + 可能的重试/压缩重试）在奥赛题上实测可到 500-900s。第 1 票跑完软预算即尽，第 2/3 票的 `chat_prefilled` 立即抛 `DeadlineExceeded` 快速失败 → `len(samples) < 2` → 回退单票。**"hard 题多温度投票"在它最该生效的题上实际不生效**；而在中等耗时题上，3 票可能吃掉后续 Python/cross_validator 的时间。

**影响**：机制近乎死代码（不产生 3 票多数决）；对耗时的 hard 题，首票失败后第 2 票算白给的隐藏重试，但把整题预算压到临界，增加无答案出厂风险。

**修复建议**：投票前先估时：`if clock and clock.remaining() < 3 * last_attempt_cost(clock, "reasoning"): return _reasoning_agent_impl(state, config)`（直接走单次主路径）；或把投票限定为 prefill 短答案采样（如 V2.9 思路）而非完整推理。

---

### P2-4　`_objective_revote` 取**第一个** "答案：" 标签，模型纠错后的第二行答案被忽略

**文件:行号**：`nodes/cross_validator_node.py:148`

**代码摘录**：
```python
m = _re.search(r"答案\s*[：:]\s*(.+)", str(raw or ""))
revote = normalize_objective_answer(m.group(1).strip() if m else "", question_mode)
```

**触发场景**：复核投票的回复若形如"答案：A\n（核对后）答案：B"（模型中途自我纠正），`re.search` 只取第一处，纠错后的 B 被丢弃；若模型先写"答案：不确定"再写"答案：B"，取到的是不可用值 → 复核直接判 `agree=None`，浪费一次 45s 调用。

**影响**：低置信复核投票对"模型会自我纠正"的回复失效（与其在 `_strip_answer_wrappers`/`extract_answer_fallback_with_source` 中"取最后一个标签"的口径不一致）。

**修复建议**：改用 `list(re.finditer(...))[-1]` 取最后一个匹配（与 `problem_profile._strip_answer_wrappers` 保持一致）。

---

## 3. P3 低危（潜在/防御性缺陷）

### P3-1　`budget.is_tight` / `clock.fast_path` 缺括号，绑定方法恒为真

**文件:行号**：`nodes/python_agent_node.py:182`、`309`；`nodes/reasoning_agent_node.py:406`

**代码摘录**：
```python
max_attempts = 1 if budget and budget.is_tight else CONFIG["max_retries_per_node"]   # is_tight 是方法
...
if clock and clock.fast_path:          # fast_path 是方法 → 恒真
    max_attempts = 1
```
实测 `TokenBudget.is_tight`、`TimeBudget.fast_path` 均为方法对象，`bool(...)` 恒 True。**后果**：只要 `token_budget`/`time_budget` 存在（生产环境必然存在），两个节点的 `max_attempts` 恒被压到 1。当前因 `CONFIG["max_retries_per_node"]=1` 而**无实际行为差异**（被配置掩盖），但一旦有人调高重试数，Python 分支的重试会被静默永久禁用（Reasoning 分支 L636 用的是正确的 `clock.fast_path()`，两分支行为将不一致）。

**修复建议**：`budget.is_tight()`、`clock.fast_path()` 补括号（一行修复）。

---

### P3-2　`answer_text.lower not in {"none", "null"}` 恒真，防"无结论"守卫失效

**文件:行号**：`nodes/python_agent_node.py:465`

**代码摘录**：
```python
if repaired_output.get("success") and answer_text \
        and answer_text.lower not in {"none", "null"}:
```
`answer_text.lower` 是绑定方法，永远不等于字符串 `"none"`/`"null"`（实测恒 True）。**后果**：确定性补打印修复若真产出 `answer="none"`，会被当作有效答案进入 `finalize`；该守卫形同虚设。

**修复建议**：`answer_text.lower() not in {"none", "null"}`。

---

### P3-3　节点超时/化简限时使用非 daemon 线程且不回收，僵尸线程累积

**文件:行号**：`utils/timeout_control.py:45-59`（`run_with_timeout`）；`utils/answer_matcher.py:286-302`（`_run_limited`）

**代码摘录**：
```python
executor = ThreadPoolExecutor(max_workers=1)
try:
    future = executor.submit(fn)
    return future.result(timeout=...)
finally:
    executor.shutdown(wait=False)   # 超时后线程继续跑，直到 socket/sympy 自行结束
```
每次调用新建一个线程池；超时后线程（非 daemon）继续运行——`sp.simplify` 卡死的线程永不退出，配合并发 3 与多轮重试，进程线程数可缓慢累积（`ThreadPoolExecutor` 默认线程非 daemon，还会阻塞进程退出）。文档已承认该权衡，属已知设计，但建议限制：超时后不等待即可，但应尽量复用单例线程池或标记 daemon。

---

### P3-4　`error_handler` 的 coordinator 兜底直接倾倒 `raw[-2000:]`，未过洁净度门

**文件:行号**：`utils/error_handler.py:136`

**代码摘录**：
```python
if raw:
    final = f"未能完成完整推导，以下为已获得的部分结果：\n{raw[-2000:]}"
```
正常路径的 `_partial_response`（`coordinator_node.py:74-106`）对每级候选都过 `is_noise_answer`/`looks_incomplete_answer` 门（评委报告模式 B 的碎片治理），但 `node_wrapper` 异常兜底直接倾倒原始响应尾部 2000 字符——**"哪里出错了？啊！"、"s_j." 类碎片可由此路径出厂**。当前该路径仅在 coordinator 节点抛异常时触发（coordinator 内部已捕获自身 LLM 异常，触发概率低），但仍违反自身"宁可给通用失败说明也不给不可判分噪声"的原则。

**修复建议**：复用 `coordinator_node._partial_response`（或对 `raw[-2000:]` 过 `is_noise_answer` 门，不过则回退 `"最终答案：未能求解"`）。

---

### P3-5　`llm_retry._continue_truncated` 续写拼接可能重复尾部

**文件:行号**：`utils/llm_retry.py:200-204`

**代码摘录**：
```python
stitched = stitch(tail, completion)
current = (current + stitched[len(tail):]
           if stitched.startswith(tail) else current + stitched)
```
当 backend 忽略 assistant 种子、返回独立完整回复时，`stitch` 返回 `seed + body`（`prefill.py:61`），`stitched.startswith(tail)` 为真（seed 即 tail），拼接正确；但当 `stitch` 因"echo with leading wrapper"路径返回 `body[body.index(seed):]` 而该段不从 `tail` 开头时（wrapper 在 seed 之前），走 `current + stitched` 会把 tail 重复一遍，污染续写结果。截断补救通道输出被污染 → 下游解析出含重复尾部的答案。

**修复建议**：`else` 分支改为 `current + stitched[len(tail):] if stitched.endswith(completion) else current`（去重后拼接），或对 `current + stitched` 做 tail 去重。

---

### P3-6　`PaperPacer` 单例无生产环境 `reset()`，跨评测轮次残留

**文件:行号**：`utils/paper_pacer.py:74-85`；`langgraph_math_agent.py:104-117`；`user_agent.py`

**代码摘录**：
```python
@classmethod
def get_instance(cls):
    with cls._lock:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
```
生产代码（`langgraph_math_agent.py`）只 `get_instance()` 从未 `reset()`；`reset()` 仅在测试中出现。若评测平台在**同一进程内连续跑多卷**（或本地调试多次 `main.py` 于同一进程），`_done`/`_start` 从上一卷延续，后续题软预算会被错误收紧/放松。同样，`ReasoningAgent._contract_probe_cache` 类级缓存跨卷残留。

**修复建议**：在 `MathAgentGraph.run` 或 `solve` 入口按"卷"重置 pacer（如检测 `idx==0` 或显式 `PaperPacer.reset()` 接口由平台每卷调用一次）。

---

### P3-7　`derivation_conflict` 条件句可致假阳性"推导矛盾"

**文件:行号**：`utils/derivation_conflict.py:54-62`（`_TAIL_OK` 含空格）

**代码摘录**：
```python
after = line[m.end():]
if after and after[0] not in _TAIL_OK:
    continue
# _TAIL_OK = set("，,。．；;\n)） ")
```
"代入 x=3 得…"（`after=" 得…"`，首字符为空格 ∈ `_TAIL_OK`）与另一行"代入 x=5 得…"会被记为同一变量两个取值 → `detect_derivation_conflict` 报矛盾 → Critic 升级为 `calc_error` 触发修复轮（`critic_node.py:143-147`）。分段/条件推导文本（"当 x=3 时"…"当 x=5 时"）是合法推导，不应判矛盾。

**修复建议**：`_TAIL_OK` 去掉空格，或把 `after` 剥掉前导空白后再判定结尾词（"得/时/则"等续写词同样排除）。

---

### P3-8　`numeric_crosscheck` 多值答案只取第一个值 → 误报 contradict

**文件:行号**：`utils/numeric_crosscheck.py:112-117`

**代码摘录**：
```python
if "=" in t:
    t = t.rsplit("=", 1)[1].strip()
t = re.split(r"[，。；;：]", t)[0].strip()   # 多值答案只取第一个
```
候选 "x=1；x=2"（双解）vs Python "x=2" → 取 cand=1、py=2 → mismatch → `python_agent_node.finalize`（L282-297）把 `evidence_status` 强制降级为 `contradict` → 触发 playoff/仲裁，把本来"共享解 x=2"的两路判成冲突，白费预算且可能改选错误一方。

**修复建议**：多值答案任一分量与 Python 值相等即判 match；或对含 "；/，" 分隔的多值候选直接返回 inconclusive。

---

### P3-9　`numeric_crosscheck` 的 `sp.simplify` 无超时

**文件:行号**：`utils/numeric_crosscheck.py:156`

`sp.simplify(cand_expr - py_expr)` 与 `answer_matcher` 的 `_simplify_limited`（带 8s 超时）不同，此处无时限；病态表达式可卡住 Python 节点内部（虽受 node_wrapper 1100s 上限兜底，但会烧掉整题时间）。

**修复建议**：复用 `answer_matcher._run_limited` 加超时。

---

### P3-10　`_extract_answer_from_output` 取"最终答案:"之后**全部** stdout，尾部解释行会污染答案

**文件:行号**：`mcp_servers/python_executor/server.py:82-88`

```python
markers = list(re.finditer(r"最终答案[：:][ \t]*", stdout))
answer = stdout[markers[-1].end():].strip()
```
若模型代码在 `print("最终答案:", answer)` 后又打印了任何行（如 `print("done")`、`print(验证状态...)`），`answer` 字段会带上这些行，污染与推理候选的比对（概率低，但契约只要求"最终答案"是最后一行，未强制）。

**修复建议**：取到 `\n` 为止（单行截断），或在执行器侧对 answer 做 `splitlines()[0]`（多行 Matrix/子群列表输出是刻意保留的，需用"下一个空行/标记"切分而非硬切）。

---

### P3-11　`classify_question_mode` 判断题与"并证明"的边界误判

**文件:行号**：`utils/problem_profile.py:79-80`

"判断…并证明"类题被归入 `true_false` 快速路径（跳过证明体），判分若按"结论+必要过程"（§6.2）会给 partial。同类还有 `_PROOF_RE` 的宽匹配把含 "show that" 的题干误归 proof。属题型画像的固有误差，风险可控但值得记录。

---

### P3-12　`_looks_truncated` 弱信号对短上限调用误判（低频）

**文件:行号**：`utils/llm_retry.py:239-250`

`len(s) < max_tokens * 1.2` 才进入弱信号分支；objective_reasoning 上限 4096，中文答案 5000 字符（≈5000 token 计费）会被判"写满"触发一次 45s 续写，属良性但可避免的开销。非缺陷，仅记录。

---

## 4. 各节点/模块按任务清单的专项结论

1. **reasoning_agent_node（795 行实为 859 行）**：采样轮次/温度/retry 均有界（`max_attempts` 循环 + `can_afford_retry` + 压缩重试熔断），**无死循环**；预算耗尽时返回"空答案 dict"（`answer=""`）而非崩溃，下游有完整兜底链。主要问题见 P1-2（sympy 短路）、P2-3（投票机制）、P3-1（is_tight 括号）。
2. **classifier_node**：三级兜底（prefill → plain → 关键词/TF-IDF）完备，`_resolve_category` 保证返回值 ∈ 18 个真实文件夹名（防 FileNotFoundError）；difficulty 解析失败返回 None 只影响软预算，安全。
3. **cross_validator_node**：状态机（match/mismatch/uncertain/contradict + playoff/reconciliation/arbiter 路由）有界、无死循环；历史 `validation_history` 每轮追加（reducer 合并）。主要问题见 P1-1、P2-4。
4. **python_agent_node**：代码生成→执行→修复闭环有界；执行超时由 `subprocess.run(timeout=60)` 处理并抢救部分 stdout；`"最终答案:"` 标记解析在 server 侧与 `stdout_miner` 双层。主要问题见 P3-1、P3-2。
5. **coordinator_node**：LLM 输出解析失败/异常时保底出 `validated_answer`（`f"最终答案：{validated}"`）或 `_partial_response`/应急直答，**不会空手而归**；prompt 模板占位符与 `.format` 参数一一对应（`prompt_templates.py:116-188` vs `coordinator_node.py:372-380`）。见 P3-4。
6. **semantic_arbiter/playoff/critic/reconciliation**：决策逻辑均有界；仲裁 nonce 校验、playoff `playoff_trace` 防重复、critic 修复轮 `_MAX_CRITIC_REPAIR_ROUNDS=1`、reconciliation `reconciliation_round_limit`（fast_path 时=1）均正确；未见死循环路径。
7. **sympy_pre_solver**：数值精度方面无 nsimplify/浮点问题（sympy 精确符号计算，`_format` 直接 str）；异常全捕获（`pre_solve` 内 try/except 包住 `hints_for`）；**多解漏解**风险见 P1-2（`_congruence` 在 `divisor != 1` 时返回 None 是正确规避；`_solve_equation` 返回全解集但可能"解集≠所问"）。
8. **answer_matcher / answer_formatter / answer_extractor / structured_answer**：LaTeX 变体/分数小数/绝对值/区间均有多层归一化（`_latex_to_expr_text`、`_const_product_to_expr`、`_strip_function_label`、`compare_structured_answers`），未见系统性缺陷；边界问题见 P2-1、P2-2、P3-7、P3-8。
9. **llm_retry**：外层 `llm_max_retries=1` + 内层 client 自旋 ≤3 → 单调用最多 3 个请求（重试风暴已被配置掐死）；退避 `backoff_factor**(attempt-1)` + 限流特征 ≥10s 长退避；`_affordable(observed)` 用实测成本定价，符合设计。见 P3-5、P3-12。
10. **paper_pacer / time_budget / timeout_control**：`remaining()`（soft−reserve−elapsed）与 `remaining_hard()`（total−elapsed）区分正确，soft/hard 无混淆；`apply_difficulty_profile` 只收紧不放宽；`timeout_for` 把节点上限钳到剩余硬时限。见 P3-3、P3-6。
11. **error_handler / hard_fallback / conclusion_salvage**：异常兜底不吐空答案（最坏 `"最终答案：未能求解"`）；hard_fallback 只对放弃型答案触发一次；salvage 有结论词+洁净度双门。见 P3-4。
12. **线程安全**：见 §0 与 §7。无 P0 级竞态；无锁的懒初始化（SkillsLoader 缓存、`_SYMPY_SOLVER`、类级 probe 缓存）均为幂等双写，无数据损坏；僵尸线程与共享 client 并发调用是主要残留风险。

---

## 5. 测试结果

```
python -m pytest tests/ -q
154 passed, 7 warnings, 30 subtests passed in 46.82s
```
- 失败：0。警告：mpmath `bitcount` DeprecationWarning（依赖版本）、requests/urllib3 版本提示，均非功能性问题。
- 说明：测试覆盖（含 `test_v23_*`、`test_v24_*`、`test_integration_*`、`test_stress_faults` 等）未覆盖本次报告中的 P1-1（`validated_answer` 取值路径）、P1-2（sympy 短路语义）、P2-1（阶乘噪声）、P3-2（lower 无括号）——这些缺陷在单测层面不可见，需真实推理链路/集成用例才能暴露。

---

## 6. 修复优先级建议

| 优先级 | 缺陷 | 工作量 |
|---|---|---|
| 最高 | P1-1 cross_validator `validated_answer` 取 `candidate` | ~3 行 |
| 最高 | P1-2 sympy 短路加"所问目标"校验 | ~20 行 |
| 高 | P2-1 阶乘噪声例外 | ~3 行 |
| 高 | P2-2 极值单值契约放宽 | ~5 行 |
| 中 | P2-3 投票预算护栏、P2-4 取最后标签 | ~10 行 |
| 低 | P3-1 补括号、P3-2 补 `()`、P3-3~P3-12 | 各 1-15 行 |
