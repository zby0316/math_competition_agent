# 竞品深度拆解：MathAgent-S1 推理与验证机制（官方评测 71 分）

> 拆解对象：`C:\Users\34707\Desktop\71分\MathAgent-S1-New`
> 核心源码：`nodes/reason_step.py`（734 行）、`nodes/verify_step.py`（424 行）、`utils/llm_retry_guard.py`、`utils/verdict_evidence.py`、`utils/verdict_authenticity.py`、`utils/verdict_salvage.py`、`utils/output_miner.py`、`exec_server/executor.py`、`utils/exec_client.py`，以及支撑文件 `utils/prompt_bank.py`、`utils/answer_pull.py`、`utils/answer_sanitize.py`、`utils/structured_reply.py`、`utils/question_profile.py`、`utils/wall_clock.py`、`utils/guard_ceiling.py`、`utils/seed_prompt.py`、`utils/retry_pricing.py`、`utils/domain_excerpt.py`、`nodes/judge_step.py`、`graph/solve_branch.py`、`config.py`。
> 模型：`intern-s2-preview-397b`（推理模型，`reasoning_content` 与 `content` 合并计入 `max_tokens`，~55 tok/s）。
> 平台硬约束：单题 20 分钟（1200s）墙钟上限，官方 cap=8192（评测平台对单次调用 max_tokens 的硬顶）。

---

## 0. 一句话总览

这套 71 分的核心不是"更强的提示词"，而是一套**围绕截断、超时与幻觉三大敌人构建的防御性流水线**：
答案前置（结论速览）+ 小额度首轮（8192，主动触发截断）+ prefill 压缩重试（抑制私有 CoT 的"断点续写"）
+ 双路并行（LLM 推理 ∥ Python/SymPy 验证）+ 反伪造证据分级（support/contradict/inconclusive）+ 墙钟预算驱动的重试定价。
推理与验证共享同一套"可负担性审计 + 分级熔断"骨架，验证侧的亮点在于**让代码自己说话、且只信真实算出来的东西**。

---

## 1. 推理提示词结构

### 1.1 主提示：五章节契约（`utils/prompt_bank.py:29-95` `REASONING_PROMPT`）

主提示由 `.format()` 注入三个占位符（`reason_step.py:408-415`）：

```python
base_prompt = REASONING_PROMPT.format(
    category=category,
    skill_document=select_skill_excerpt(skill_doc, problem, 3000),   # 主题选摘，不是裸切片
    problem=problem)
base_prompt += mode_instruction(question_mode)        # 题型后缀（客观题快速路径）
base_prompt += structure_instruction(problem)          # 题面结构画像（全部解/极值/端点/序号）
coverage_clause = answer_coverage_clause(problem)      # 压缩重试专用的"答案覆盖度"条款
```

**五个 Markdown 章节，顺序即设计**（`prompt_bank.py:58-84`）：

1. `## 结论速览` —— **必须放在最前**，内容是 `\boxed{[最终结论的一句话压缩版]}`；
2. `## 问题分析` —— 题型、关键信息、定理，**歧义口径在此注明**；
3. `## 详细解题步骤` —— `步骤N：[描述] / 使用公式/定理 / 计算过程` 三段式；
4. `## 最终答案` —— 明确最终结果；
5. `## 关键验证点` —— 自检清单。

**硬性输出纪律**（`prompt_bank.py:31-36`）：

- "直接输出下面要求的五个 Markdown 章节（'## 结论速览' 必须放在最前）"；
- "不要输出 Thinking Process、思考过程、推理草稿或任何格式外文字"；
- "'## 最终答案' 后必须给出真实、明确的最终结论；不得输出 [Result]、*、占位符或空答案"；
- "不得编造题目或技能文档中没有明确给出的数据"。

**8 条格式契约**（`prompt_bank.py:86-94`）逐条针对失分形态：

1. 步骤完整、逻辑清晰；
2. LaTeX 用 `$` 包裹；
3. 计算过程详细；
4. **多问项逐项列出**（"f_Z(z) = ..."、"P(Z>1) = 1/2"），不得只给一问；
5. **过程量/表格字段逐字段列出**（均值、平方和、标准误、检验统计量、临界值、ANOVA 表）；
6. **假设检验六要素**：原假设/备择假设、检验统计量值、临界值（或 p 值）、比较、拒绝/不拒绝 H₀ 判断、结合题面的实际结论；
7. **置信区间双端点 `[a, b]`**；β 与功效的拒绝域写法；
8. **枚举/分类题列全对象并给总数**；运输问题给全部基变量与总运费；矩阵题写全 X、XᵀX、Xᵀy。

### 1.2 题面口径检查清单（`prompt_bank.py:38-50`，10 条先验核验）

逐字读题、`between a and b` 按开区间、端点可达性（≤ 推出的界在严格不等式下取不到 → 答案 K+1 并写出等号不成立的那一步）、序号对齐（第 k 大先列 k=1,2,3）、对称因子只乘一次（最小实例回代标定）、不可能性先证伪（小规模构造）、值域即代数结构（F₂/Zₘ 内聚合）、单/双尾、算子作用空间（内蕴度量）、答案类型与题面问法一致（问集合答集合）、最简形式。**这些不是通用废话，是逐条从实测错题提炼的差一/漏支/口径错误防治**。

### 1.3 "答案前置"如何实现（本轮提分核心）

三层机制叠加，**目标是"截断也不丢答案"**：

1. **提示层**（`prompt_bank.py:58-63`）：明确要求"先在脑中完成全部推导，再把最终结论用 `\boxed{...}` 单独写在最前面的 '## 结论速览' 章节……然后才展开后面的四章节；'## 最终答案' 与 '## 结论速览' 必须完全一致"。即**模型先承诺答案、再展开推导**，答案位于输出流的头部；
2. **解析层**（`reason_step.py:47-52`）：四章节解析拿不到答案时，回退读 `## 结论速览` 节，并打 `answer_source="quick_conclusion"` 来源标记，与正式答案区分；
3. **重试层**（`reason_step.py:312`）：压缩重试的 assistant 种子直接是 `_COMPRESSED_PREFILL = "## 结论速览\n\\boxed{"`——续写模式下一上来就写答案，8192/16384 token 全部落在可见章节上（详见 §4）。

### 1.4 客观题专用提示（`reason_step.py:417-488`）

选择题/判断题/填空题走完全不同的短提示（`objective_prompt`）：

- `domain_guard`（`reason_step.py:422-437`）：抽象代数域扩张/分裂域题注入**可机械执行的判据**（列全部根 → 分裂域=Q(实根, ζ_n) → 塔次数乘积 → Galois 核对），把"要检查"变成"照着算"；
- `answer_shape` 按题型生成：填空"按空位顺序写全部结果，多空用分号分隔"；判断"只写：正确 或 错误"；选择"列出全部正确选项字母，措辞不精确的选项一律不选"；
- `needs_derivation` 判定（`reason_step.py:463-466`，正则匹配"分裂域|扩张次数|维数|秩|阶数|收敛半径…"）→ 需要实算的客观题改为"**先算后答**"三行契约（核对/答案/依据），其余用两行契约（答案/依据，先承诺后核对）——注释明确记录了"先写下 Q(∜5,i);8 再补依据"的教训；
- 空位数由代码侧数好写进提示（`reason_step.py:440-451`，`count_blanks`），防止"Count blanks: 1"式元指令回显与三空只答零空。

### 1.5 技能文档主题选摘（`utils/domain_excerpt.py`）

`select_skill_excerpt(doc, problem, limit)` 替代裸 `[:3000]` 切片：按 `## 模块` 标题切分文档，以题面术语重合度打分（标题双倍计分、博弈/数论主题线索加成、`- 检索词：` 行子串匹配加成），**得分最高模块优先拿预算**。文档注释记录了真实事故：非基础及进阶课程长到 8949 字符后，数论（offset 3404）与博弈论（offset 5369）模块超出 3000 字符窗口，博弈题拿到的是 Lebesgue 测度。

---

## 2. 多轮推理

### 2.1 调用次数与参数（`reason_step.py:396-734`）

- **上限**：`max_attempts = 1 if budget and budget.is_tight() else CONFIG["max_retries_per_node"]`（`reason_step.py:401`），`max_retries_per_node=2`（`config.py:3`）；`fast_path()`（剩余 < 300s）时强制 `max_attempts=1`（`reason_step.py:605-606`）。
- **温度**：全程固定 `CONFIG["temperatures"]["reasoning"] = 0.8`（`config.py:20`），客观题 0.2、Python 0.6。**没有温度递增策略**——多样性靠的是完全不同形态的重试（压缩/线索复用），不是温度抖动。
- **token**：首轮 `max_tokens["reasoning"] = 8192`（`config.py:47`）；压缩重试 `reasoning_compressed = 16384`（`config.py:48`）；客观题 4096。
- **墙钟**：首轮单次调用有 550s 硬上限（`_FIRST_ATTEMPT_TIMEOUT_S`，`reason_step.py:306, 622-637`），`run_with_timeout` 掐断后**就地转入压缩重试**，而不是让节点级 wrapper（1100s）掐死整条分支。

### 2.2 三种失败 → 三种重试（`reason_step.py:695-732`）

核心分类：**Token 耗尽与格式失误必须分开处理**（`reason_step.py:695-700` 注释原文）。

| 失败类型 | 判定 | 重试方式 |
|---|---|---|
| 格式失误（无章节/缺章节） | `_is_complete` 为假且未判耗尽 | 普通重问，带格式提醒（`reason_step.py:730-732`） |
| Token 耗尽（`finish_reason=="length"` 或长响应无章节） | `_looks_token_exhausted`（`reason_step.py:255-287`） | **prefill 压缩重试**（`_compressed_reasoning_retry`），不占普通名额，按硬时限余量定价 |
| 首轮超时 / 传输故障 | `NodeTimeoutError` / 异常 | 同样转压缩重试（`reason_step.py:659-682`），失败原因区分"超时被切断"/"网络故障" |

**`_looks_token_exhausted` 的关键细节**（`reason_step.py:255-287`）：

- 优先读服务端真实停止原因 `finish_reason`："length" 是明确截断信号（即使 content 很短——CoT 吃光额度）；"stop" 是正常结束（长响应也只是格式失误）；
- 无 finish_reason（平台 client）时退回长度启发式：`len(text) >= 2000` 且无 `## 最终答案`；
- **捞回来源的答案不算真实产出**：`answer_source in {"salvaged_prose", "fallback_line"}` 时仍判耗尽（`reason_step.py:252, 267-269`），否则一条被扫描到的中间句会掩盖真实耗尽、跳过唯一有效的压缩重试（注释记录了曾因此"数题以中间句出厂"）。

### 2.3 重试可负担性审计（`utils/retry_pricing.py` + `utils/wall_clock.py`）

`can_afford_retry(clock, "reasoning")`（`reason_step.py:611`）按**上一轮实际成本**核拨重试额度：`last_attempt_cost` 取 spend_log 中最近一次同类调用的实测秒数（失败尝试也计入——"一次传输故障烧掉 363s，就还会再烧 363s"），默认 300s；`*_compressed` 标签不计入（短调用不能冒充"再来一轮很便宜"）。这套逻辑直接针对实测事故：推理与 Python 各跑两次 510-552s，第二次获批时只剩 349s，超 deadline 198s 且零交付。

### 2.4 传输层重试（`utils/llm_retry_guard.py`）

- `LLMRetryGuard`：外层 `max_retries=3`、指数退避 `2^attempt`（`llm_retry_guard.py:146`）；
- **每次尝试前先问时钟**：`_affordable(observed)` 用"上次实测耗时 vs 剩余时间"决定是否值得再试（`llm_retry_guard.py:69-89`），杜绝嵌套重试（平台客户端内部 3×120s + 外层 3 次 = 最坏 9 请求 1080s 击穿 20 分钟上限）——这是 1307s 超时事故的根治；
- `ChatResult(str)`（`llm_retry_guard.py:17-30`）：str 子类，携带 `finish_reason / reasoning_content / usage`，所有调用点无需改动即可在截断判定处读到元信息；并发下从 `client._tls` 线程本地读取（`llm_retry_guard.py:121-125`，`llm_client.py:89-91`）——"记录了 finish_reason 却因并发共享 client 读不到、等于盲飞"的缺口被补上。

### 2.5 客观题的三级升级路径（`reason_step.py:489-592`）

1. 一次普通小调用（`objective_reasoning_plain`，预期 45s）→ 解析成功且填空覆盖完整即返回；
2. 失败则 `chat_prefilled(prefix="答案：")` 种子调用（预期 60s）→ 重试答案只在"有答案且不比首轮残缺"时替换；
3. 两次都空 → 升级为一次四章节压缩推理（`_compressed_reasoning_retry`），prefill 抑制私有 CoT，"## 最终答案"给归一化器一个真正的答案行。

---

## 3. 验证机制

### 3.1 Python 验证提示词（`utils/prompt_bank.py:216-261` `PYTHON_PROMPT`）

结构：角色 + 严格输出要求（**只输出一个 ` ```python ` 代码块**，代码块外零文字）+ **60 秒执行时限** + 三行证据契约 + 10 条题型防坑 + 验证知识提示。

**证据契约**（`prompt_bank.py:222-223`，也是重试时反复重申的 `_EVIDENCE_RETRY_CONTRACT`，`verify_step.py:67-73`）：

```text
print("验证状态:", status)      # status ∈ {PASS, FAIL, INCONCLUSIVE}
print("验证证据:", evidence)    # FAIL 必须含反例或失败断言
print("最终答案:", answer)      # 必须是最后一行
```

**反作弊条款直接写进提示**（`prompt_bank.py:223`）："验证状态必须由代码中的实际比较产生（assert/== 比较小规模精确值与公式），不得基于注释、记忆或'已知竞赛结论'直接打印 PASS——无实质计算的 PASS 会被系统静态检查判为无效证据并作废"。配套 10 条题型防坑（`prompt_bank.py:224-252`）：极值题至少比较两个不同构造；"不可能/无解"必须先小规模证伪；"第 k 大"先枚举 k=1,2,3；大规模计数先小规模暴力对拍闭式；游戏/操作题必须真实实现状态转移+搜索、禁止用"二进制 1 的个数"等已知结论冒充仿真；几何/最小 k 题禁止自造公式自我核验；`answer` 必须单行自包含字符串（矩阵 str() 转换、多量用分号带名、统计题保留命名字段）；常见 API 陷阱（jordan_form 返回 (P,J)、diagonalize 返回 (P,D)、solve 返回列表先判空）。

**独立核验注入**（`verify_step.py:232-237`）：推理侧答案以"待核验候选（不可信，仅用于独立核对）"形式注入，明确要求"请不要盲从该候选；根据代码计算结果填写验证状态和验证证据"。**这是"验证器独立于推理"的关键设计**——候选会被质疑而不是被背书。

**素材注入**（`verify_step.py:213-231`）：`validation_script`（领域验证示例，`select_script_excerpt` 按题面选 2000 字符）+ 技能手册速查条目（`select_skill_excerpt` 1600 字符，与推理分支共享素材源）。

### 3.2 代码生成与提取（`verify_step.py`）

- `_extract_code`（`verify_step.py:21-57`）四层提取：严格围栏（```python 独占一行）→ 宽松围栏（内容须以 import/from/#/def 等开头）→ 任意围栏 → 基于内容找首个 import/from 行到结尾。**找不到就返回 `""`（→ 重试），绝不把思维链当代码执行**；
- `_prints_answer`（`verify_step.py:88-95`）：匹配 `print("最终答案", ...)` 调用（匹配 print 调用而非文本，避免把注释误判为有效）——**廉价的截断检测**：max_tokens 截断在文件中间会留下看似合法但未完成的代码；
- 缺打印的代码**仍然执行**（`verify_step.py:372-377`）：stdout 里往往已有结论，由 stdout 二次抽取兜底（曾出现正确公式在代码里、旧逻辑拒绝执行去换一次 450s 重生成最终一无所获的教训）；
- **确定性修复** `_append_answer_print`（`verify_step.py:102-123`）：代码定义了可无参调用的函数却没调用，确定性追加 `print("最终答案:", fn())` 再执行——不花 LLM 时间（曾出现 DFS 建模方向完全正确、只差一个主调用的案例；而"补打印"的 LLM 短重试恰会被预算门禁拒绝）。

### 3.3 执行沙箱（`exec_server/executor.py` + `utils/exec_client.py`）

**主路径 = 隔离子进程**（`executor.py:58-127`）：

1. 代码写入临时 `.py` 文件；
2. stdout/stderr 重定向到**临时文件**而非内存管道（防海量输出吃爆内存），读取时截断到 64KB/8KB 上限；
3. `subprocess.run([sys.executable, temp_file], timeout=...)`，**最小环境**：仅 PATH、HOME、LANG/LC_ALL、`PYTHONIOENCODING=utf-8`、`PYTHONUTF8=1`（Windows 上钉死 UTF-8，防 GBK 代码页错配）；不注入任何平台函数/凭据；
4. **超时救回**（`executor.py:105-117`）：`TimeoutExpired` 后从临时文件救回已打印的部分 stdout——"先出答案再陷入死循环"也能拿到答案；
5. 答案抽取 `_extract_answer_from_output`（`executor.py:22-43`）：取**最后一个** `最终答案：` 标记到末尾的全部内容（SymPy Matrix/子群列表等多行输出必须完整保留），剥掉截断产生的 U+FFFD；标记缺失返回 None（程序没走到自己的结论，通常是生成中途被截断——旧回退用 stdout 最后一行曾把中间计算日志当答案送进仲裁）。

**兜底路径 = 受限进程内 exec**（`executor.py:142-183`）：`_exec_in_process` 用 **builtins 白名单**（`_SAFE_BUILTINS`，`executor.py:132-139`，仅 abs/all/any/.../zip 等纯计算基础类型，无 os/subprocess/open/eval/exec/__import__）+ sympy/numpy/math/scipy 白名单模块 + 线程超时（无法硬杀，只用于极简纯计算）。

`ExecClient`（`exec_client.py`）：MCP 工具函数以进程内方式调用（每次调用 = 独立子进程，同步且并发安全），子进程路径抛异常时回退进程内执行；执行结果打 `execution_backend` 标记（mcp / local_fallback）供 trace 审计。节点执行超时 `python_mcp_execute = 60s`（`config.py:13`）。

### 3.4 验证证据判定（`utils/verdict_evidence.py`）

`parse_verification_evidence(output, candidate_answer, code)` 把执行器输出规范化为**与进程状态分离的证据**，产出 `evidence_status ∈ {support, contradict, inconclusive}`：

1. **低置信答案通道**（`verdict_evidence.py:61-65`）：高置信通道（`最终答案:` 标记）为空但 stdout 明确给结论时，`mine_stdout_answer` 挖掘并打 `answer_source="stdout_mined"`；
2. **状态/证据行解析**（`verdict_evidence.py:67-70`）：`_STATUS_RE` 匹配 `验证状态：PASS|FAIL|INCONCLUSIVE`，`_EVIDENCE_RE` 取证据文本；
3. **矛盾收集**（`verdict_evidence.py:72-94`）：逐行找"反例/矛盾/不一致/失败断言/counterexample/FAIL"；FAIL 状态把证据行提到矛盾首位；**数值对账**：候选答案是标量时，正则 `_MAX_RATIO_RE` 扫 stdout 里的 maximum/ratio 值，发现比候选更大的观测值即记为矛盾（防"只验证了局部最优却宣称全局"）；
4. **状态合成**（`verdict_evidence.py:96-108`）：有矛盾 → `contradict`；PASS 且**进程 success=True** → `support`（"失败进程可能残留一行半截的 PASS。不能借进程失败来'授权'一次匹配"）；PASS 但 success 非真 → `inconclusive`；其余 → `inconclusive`；
5. **反伪造降级**（`verdict_evidence.py:110-117`）：`assess_verification_authenticity` 判伪造时，support 一律降级为 inconclusive 并改写证据摘要为 `[反伪造] ...`——"contradict 的反例本身就是计算产物，不受权威引用污染"，只降 support。

### 3.5 反伪造检测（`utils/verdict_authenticity.py`）

针对最危险的失分形态：**验证代码不做任何计算，直接以注释/打印断言结论**（"基于已知竞赛结果验证…PASS"），验证器退化成结论的回声。纯静态、保守检测：

1. **计算实质度**（`verdict_authenticity.py:28-38`）：`_COMPUTE_RE`（循环/推导式/solve/simplify/枚举/聚合调用/函数定义）+ `_VAR_ARITH_RE`（对变量的真实算术，排除纯字面量"1+1"）——存在任一即认为"确实算了点什么"；
2. **权威引用断言**（`verdict_authenticity.py:47-51`）：注释/字符串里出现"已知(结论|结果|竞赛|存在)|经分析|well-known|known result"等，且伴随结论；
3. **无条件 PASS**（`verdict_authenticity.py:54-57, 103-105`）：`print("验证状态: PASS")` 实参是字面量而非变量，代码里也无 assert/== 比较。

判定规则（`verdict_authenticity.py:95-105`）：无实质计算却宣称 PASS → fabricated；有计算但无比较且引权威 → fabricated；字面量 PASS + 无比较无计算 → fabricated。**判定宁可放过**："只要代码含有实质计算就不算伪造——误伤真实验证会废掉整条 Python 分支，损失比放过一段回声大得多"（`verdict_authenticity.py:18-19`）。

### 3.6 交叉验证与路由（`nodes/judge_step.py` + `graph/solve_branch.py`）

`solve_branch.py:14-26`：非客观题从 START **扇出两条并行分支**——`reasoning_agent` 与 `python_agent`（各带 `branch_hint`），汇合到 `cross_validator`（judge_step）。客观题只扇出推理分支（短路径自带校验）。

`judge_step.py` 的路由逻辑：

- `evidence_status == "contradict"` → **反例显式证据优先于文本/符号一致**（`judge_step.py:100-120`）：优先对账重试（`reconciliation_round` 门控），重试耗尽则仲裁并标记 `unresolved_contradiction`；
- `match`（`AnswerCompare.match_answers` 判定一致）→ 计算题且 Python 成功且方法为 `symbolic/structured_symbolic` 时记 `bypass_reason`（`judge_step.py:123-125`），确定性答案优先于 LLM 散文；`validated_answer` 由 `preferred_answer`（`utils/answer_selection.py`）选出；
- `mismatch` → 对账重试（最多 `reconciliation_max_rounds=2`，`config.py:52`）→ 仲裁；
- `uncertain` → 直接仲裁（语义选择器在既有候选间裁决，可弃权回对账，不改动候选）。

`preferred_answer`（`answer_selection.py:23-62`）的选择优先级：客观题规范化候选 → **contradict 且 Python 答案非伪造 → 取 Python 答案** → match 时取推理答案 → uncertain 时比较缺失分量/分项数 → 计算题 Python 成功且非伪造优先 → 推理答案兜底。

---

## 4. 截断处理

### 4.1 max_tokens 的取值哲学（`config.py:38-51`）

关键背景：该模型把 `reasoning_content`（私有 CoT）计入 max_tokens，生成速度 ~55 tok/s，**调大 max_tokens 不会让调用变快，只会让它在 CoT 中途被截断**（`wall_clock.py:3-9`）。

`max_tokens` 演进史（`config.py:38-44` 注释原文）：

- 首轮 reasoning **30720 → 16384 → 8192 分级下调**：30720 让私有 reasoning_content 膨胀到 ~560s 逼近节点 1100s 上限（30/99 题超时）；16384 在 20 路并发（~15 tok/s）下仍需 ~1092s，被 550s 单次墙钟上限切断且**拿不到任何已生成内容**，等于白跑；
- **8192（~546s）让首轮在 max_tokens 处正常截断（`finish_reason=="length"` 可读），配合断点续写（复用已生成的结论速览/步骤）补全剩余章节——这是"小额度 + 断点续写"三件套的核心**；复杂多问/枚举答案由续写兜底，不再靠单次大额度硬撑；
- 2026-08-22 官方评测复盘（112 题）：684 次请求中 `truncated_count=286`（约 42% 被截断）→ 压缩重试额度 8192→16384、coordinator 16384→24576；
- 分级熔断设计（`config.py:29-30`）：**首轮后耗尽改发 prefill 压缩重试（助手种子抑制私有推理，全部 token 用于可见章节）**。

### 4.2 截断判定（读服务端真实停止原因，不猜）

- `ChatResult.finish_reason`（`llm_retry_guard.py:17-30, 118-129`）从 `client._tls` 线程本地读本次调用的真实停止原因；
- `_looks_token_exhausted`（`reason_step.py:255-287`）：「length」→ 截断（即使 content 短）；「stop」→ 正常结束（长响应只是格式失误，走格式提醒重试而非压缩重试）；无 finish_reason → 长度启发式（≥2000 字符且无 `## 最终答案`）；
- verify 侧同款判定（`verify_step.py:351-354`）：`finish_reason=="length"` 或（非 stop 且 ≥2000 字符）。

### 4.3 "断点续写"的具体实现（三件套）

**不是把截断文本原样续传，而是"线索复用 + 续写模式重启"**：

1. **prefill 续写模式**（`utils/seed_prompt.py`）：`prefill_messages` 在 messages 末尾追加 `{"role": "assistant", "content": prefix}`，模型进入续写模式后**不再打开 reasoning_content 块**（`seed_prompt.py:10-12` 实测：分类 40.4s/1969 token → 0.9s/12 token，58×；仲裁 70.2s/~2000 → 0.8s/4 token，140×）。`stitch`（`seed_prompt.py:33-55`）兼容后端三种行为（续写/回显/忽略）；
2. **压缩重试从"答案开头"续写**：`_COMPRESSED_PREFILL = "## 结论速览\n\\boxed{"`（`reason_step.py:312`）——先让模型把结论写出来（答案前置），再展开后续章节，截断也不丢答案；verify 侧 `_COMPRESSED_PREFILL = "```python\n"`（`verify_step.py:140`），直接从代码围栏开始续写；压缩指令 `_COMPRESSED_INSTRUCTION`（`reason_step.py:314-319`）明确"禁止展开探索性思考：沿最可行的一条路线直接写出四个章节，每章从简（'## 详细解题步骤' 最多 4 步、每步 3 行以内）"；
3. **首轮结论线索复用** `_extract_clues`（`reason_step.py:322-341`）：散文泄漏里往往已推导到关键结论，用 `recover_verdict_sentence` + `extract_partial_findings` 提取（限 1200 字符），以"你上一次推导已经得到以下结论（可信，直接复用，不要再重新推导）"注入——续写不必从头重算（注释记录：idx 0 首轮已算出 T=29，从头重解却产出 44+√(6109/90) 错误值）；
4. **压缩重试的"答案覆盖度"条款**（`reason_step.py:335-341` + `question_profile.py:335-355`）：压缩针对推导篇幅，绝不针对答案该覆盖的内容（全部解支/多问项/计数），防止"沿最可行的一条路线"被模型理解成"只报一个值"。

### 4.4 时间预算：截断处理的隐形骨架（`utils/wall_clock.py`）

三个地平线（`wall_clock.py:31-40`）：`total=1200s`（平台硬限）、`safeguard=300s`（软时限 = total - safeguard，超过后不再为可选工作付 LLM 钱）、`fast_path_threshold=300s`（低于它跳过重试）。`timeout_for`（`wall_clock.py:97-107`）把节点 ceiling 压到截止时间真正允许的范围。压缩重试的定价（`reason_step.py:290-306`）：估时 350s（16384 token @ ~50 tok/s），硬时限余量 `compressed_safeguard_s=250s`，条件 `remaining_hard() - 250 >= 350` 才放行——软预算已尽也放行，只要硬上限前还容得下"本次调用 + 收尾链（仲裁 prefill 75s + 应急直答 + 确定性拼装）"。

---

## 5. 答案提取链路

### 5.1 推理侧：`_parse_reasoning_output`（`reason_step.py:28-106`）

按优先级链提取，**每一级都打 `answer_source` 来源标记**（trace 中可区分"模型答错"与"解析器漏掉"）：

```
1. 章节解析：## 问题分析 / ## 详细解题步骤(步骤N：…)/ ## 最终答案
2. 兜底 A：## 结论速览 节（仅当最终答案缺失，answer_source="quick_conclusion"）
3. 兜底 B：extract_answer_fallback_with_source(raw)（reason_step.py:65-73）
   ├─ boxed（\boxed{...} 配平扫描，answer_pull.py:21-50）→ "fallback_marker"
   ├─ labelled（最终答案|答案|结论：xxx 行）→ "fallback_marker"
   └─ payload_line（行级扫描，须过 is_committed_result 且非推导碎片）→ "fallback_line"
   （fallback_line 置信度与 salvage 同级：耗尽判定视为"没有产出"，reason_step.py:249-252）
4. 兜底 C：客观题 bare_objective_line（逐行反向扫，规范化后须 usable，reason_step.py:74-88）
5. 兜底 D：recover_verdict_sentence（verdict_salvage.py，散文捞回）→ "salvaged_prose"
6. 客观题规范化：normalize_objective_answer（question_profile.py:390-423）
```

**`_distill_answer` 六策略**（`reason_step.py:157-237`，对 `## 最终答案` 节文本提炼）：

1. 枚举节（≥2 个编号项且 ≤1600 字符）→ 全部条目整体保留（"曾出现只保留 1 个群，其余条目被丢"）；
2. 多行短节（2-8 行 ≤900 字符）→ 保留所有信息行（等式行 + 结论行，过滤纯引导语）；
3. ≥3 个等式行 → 等式行 + 结论词行合并（"拒绝/不拒绝 H0" 这类结论行曾被只留等式行的旧逻辑丢弃）；
4. `\boxed{...}` 结论（多个用「；」连接，`reason_step.py:199-206`——"曾出现整节「所有…为 / $$\boxed{ab\ge e^3}$$ /（即…）」按旧逻辑取最后一行括注、把公式丢了"）；
5. 短节（≤80 字符）→ 最后一行；
6. 显式"答案是X"模式 → 行尾**顶层 '=' 右值**（`_rhs_of_top_level_eq`，`reason_step.py:135-154`，LaTeX 感知：跳过括号/花括号/$...$ 数学模式内的 '='，避免从 `\sum_{i=1}^n` 下标或公式中间切断）→ 首个信息行 → 整节。

**洁净度门**（`_reject_bad_answer`，`reason_step.py:109-117`）：`is_placeholder_answer`（`cot_trim.py:84-103`，占位符/模板骨架回显）+ `looks_like_latex_fragment`（`answer_pull.py:135-159`，未配对 \end/\begin、奇数个 $、花括号不平衡、游离 &）+ `looks_incomplete_answer`（`answer_pull.py:109-132`，括号开闭不等、悬垂结尾"如下/分别为"、无解/缺数据标记）+ `is_junk`（`answer_sanitize.py:87-132`，元叙述/自我怀疑/内部指令回显/英文探索散文密度）。

**`recover_verdict_sentence`**（`verdict_salvage.py:55-89`）专治"推理正文走到结论却没写出章节标题"（实测 41k-43k 字符正文、解析为空、1072s/1165s 后以"解题过程中出现错误"收场）：只扫末尾 40 句、从后往前、必须命中结论标记（因此/所以/综上/故/答案是…）+ 信息负载（等式/数值/LaTeX/定性判断）+ 长度 6-400 + 洁净度门 + 推导现场碎片门（`is_derivation_fragment`：otherwise/similarly 开头、条件对 if…if、裸符号和、true/false 试错尾巴）。

### 5.2 Python 侧：执行器高置信通道 + stdout 低置信挖掘

- 高置信：`_extract_answer_from_output`（`executor.py:22-43`）只认 `最终答案：` 标记，取最后一个标记之后全部内容；
- 低置信：`mine_stdout_answer`（`output_miner.py:68-108`）仅在标记缺失时启用，三级优先级（每级过 `is_committed_result` 洁净度门）：`matches <公式>` 型断言（程序主动把数据与公式对上，最强信号）→ 末尾命名结果行（`f(2015) = 3024`，`_NAMED_RESULT_RE`，保留左侧名称）→ 结尾裸数字行；`_looks_like_prose_log`（≥4 个拉丁词或 ≥12 个汉字 → 叙述日志）宁漏勿滥；
- 合并：`verdict_evidence.py:61-65` 把挖掘结果写回 `answer` + `answer_source="stdout_mined"`。

### 5.3 交叉比较（`utils/answer_compare.py` + `utils/structured_reply.py`）

`AnswerCompare.match_answers` 等价匹配管线（`answer_compare.py:1-6`）：unicode 常量归一化（π√×÷²）→ LaTeX 转 sympy 文本（\frac \sqrt \pi，`_convert_frac` 用平衡花括号扫描支持嵌套）→ 隐式乘法解析 → `simplify` 差为零或数值（含复数）容差比较；另有常量乘积形态（'πie'→'(pi)(I)(E)'）、矩阵环境转换（pmatrix→Matrix）。证明题走宽松匹配。

`structured_reply.py` 提供**结构化字段比较**：`extract_named_fields`（`structured_reply.py:256-309`）把答案按数学定界符/分号切段，抽取 `f(2015) = 3024`、`\bar y = ...` 这类**命名字段**（标签正则支持 LaTeX 字母命令与重音记号，`_canonical_label` 把 `\bar{y}`→`y`、`\beta_0`→`beta0`，让 LaTeX 推理答案与纯文本 Python 答案对上同一字段）；`compare_structured_answers`（`structured_reply.py:332-419`）逐字段等价比较，产出 matched/mismatched/coverage；`is_self_conflicting`（`structured_reply.py:312-329`）检测同一答案内部自相矛盾（`a = 1; a = 2`）——"两份一致副本彼此同意不构成任何证据"。

### 5.4 汇合与出厂（`answer_selection.py` + `nodes/assemble_step.py` + `user_agent.py`）

`preferred_answer` 选择最终答案（见 §3.6）；coordinator（`prompt_bank.py:97-169`）把推理步骤 + Python 代码/输出 + 验证状态 + 选定答案拼成面向读者的完整解答（"最终答案"章节完整自包含、逐问项列出，不提及内部实现细节）；`user_agent.py:73-105` 把 reasoning/python 全部写入 trace（含 `answer_source`、raw_response 节选、证据状态/摘要/矛盾列表），支持事后区分"模型答错"与"解析器漏掉"；`node_guard._fallback_for_node`（`node_guard.py:28-130`）为每个节点定义失败兜底（reasoning 空结果 / python 空证据 / coordinator 回退到原始推理文本尾部 2000 字符）。

> 注：`utils/extractors.py` 是更早期的简易提取器（ANSWER: 标记 → 最终答案/答案是 → boxed → 证毕/结论 → 末行 → 最后数字），本版推理主链路已由 `_parse_reasoning_output` 取代，仅作兼容参考。

---

## 6. 亮点清单（按对 71 分的贡献排序）

### L1. 答案前置 + 截断不丢答案（结论速览三件套）—— 提分核心
**机制**：提示要求 `## 结论速览` 放最前且含 `\boxed{}`（`prompt_bank.py:58-63`）；解析兜底读结论速览（`reason_step.py:47-52`）；压缩重试 prefill 种子就是 `"## 结论速览\n\boxed{"`（`reason_step.py:312`）；首轮主动用小额度 8192 让截断可预期、`finish_reason` 可读（`config.py:41-44`）。
**为什么有效**：该模型 CoT 计入 max_tokens，42% 的响应被截断（684 请求中 truncated_count=286）——**截断是常态而非异常**。答案放在输出流头部，无论后面章节写没写完，解析器总能拿到结论；这是把"截断事故"从丢分项变成可控流程的关键一招。

### L2. 墙钟预算驱动的重试定价（TimeBudget + 实测成本审计）—— 防止超时零产出
**机制**：`TimeBudget` 三地平线（1200/300/300，`wall_clock.py`）；每次 LLM 调用先问时钟（`llm_retry_guard.py:69-89`）；节点重试按上一轮实测成本核拨（`retry_pricing.py`）；压缩重试按硬时限余量定价（`reason_step.py:357-360`）。
**为什么有效**：单题 20 分钟是平台硬限，**超时 = 该题零分**。该系统把"时间"当成第一预算：嵌套传输重试（最坏 9 请求 1080s）、双侧盲目重试（各 510s）、首轮吃光节点上限导致压缩重试永不触发（idx 0/7/11/12/13 实测全超时）——全部是实测踩坑后修出来的。时间管理是这套代码里工程密度最高的部分，直接决定了"难题不空手而归"。

### L3. prefill 抑制私有 CoT 的压缩重试/续写 —— 截断后的真正挽救
**机制**：assistant 种子接管续写模式，模型不再打开 reasoning_content（`seed_prompt.py:10-17`，58×/140× 加速实测）；8192-16384 token 全部落在可见输出；首轮结论线索复用（`_extract_clues`）。
**为什么有效**：普通重试会以同样方式再耗尽 ~450-550s 而一无所获；压缩重试 ~150s 内产出可解析四章节，且"耗尽后普通重试"常因预算不足被跳过——**这是唯一能在耗尽后挽救的手段**。种子从结论速览开始 = 答案前置的最后一层保险。

### L4. 验证反伪造（verdict_authenticity）—— 防止验证器变成结论回声
**机制**：静态检测"无实质计算却宣称 PASS / 权威引用断言 / 字面量 PASS"（`verdict_authenticity.py`），伪造证据一律降级 inconclusive、不得作为仲裁锁定依据（`verdict_evidence.py:110-117`）。
**为什么有效**：LLM 生成验证代码时天然倾向"顺着候选答案写个自洽脚本"。若验证器只是结论的回声，双路交叉验证就退化成"自己证明自己"，反而把错误答案"验证"成高置信。**这条机制保证了 support 证据的数学含义**——必须是小规模精确值 + 实际比较，且"宁可放过、不可误伤"。

### L5. 双路并行 + 证据分级 + 反例优先路由 —— 交叉验证的可靠性
**机制**：推理 ∥ Python 扇出（`solve_branch.py:14-26`）；证据三分级 support/contradict/inconclusive（`verdict_evidence.py`）；**反例优先于文本/符号一致**（`judge_step.py:100-120`）；对账重试 → 语义仲裁 → 确定性拼装的分级降级。
**为什么有效**：计算题上 SymPy 的确定性答案优于 LLM 散文（`judge_step.py:1-5`）；程序可能运行成功却恰恰证明候选是错的——**contradict 是最强信号**，直接触发重试/仲裁而不是被"成功执行"掩盖。stdout 二次挖掘（`output_miner.py`）则堵住了"公式算对了但缺打印标记"的漏分。

### L6. 技能文档/验证示例主题选摘 —— 上下文相关性
**机制**：`select_skill_excerpt`/`select_script_excerpt` 按题面术语重合度打分选模块（`domain_excerpt.py`），推理 3000 字符、Python 2000+1600 字符。
**为什么有效**：知识库有 17 个学科目录（最大文档 182KB），裸切片只会给模型"文档开头"——博弈题拿到 Lebesgue 测度（真实事故）。主题选摘让有限的 token 预算花在真正相关的知识上，且推理与验证共享同一素材源保证口径一致。

### L7. 客观题快速路径（题型画像 + 两行契约）—— 低耗时高得分
**机制**：`classify_question_mode` 判定 choice/true_false/fill（`question_profile.py:73-101`）；客观题不扇出 Python 分支、不走 30k token 证明搜索，用 4096 token 短调用 + "答案/依据"契约（`reason_step.py:417-488`）；空位数代码侧计数 + 填空覆盖校验（`fill_answer_matches_blanks`）；needs_derivation 切换"先算后答"。
**为什么有效**：官方题集包含大量选择/判断/填空，这类题**答案格式固定、判分刚性**（选项字母/正确错误/空位顺序）。短路径大幅节省时间预算（把时间留给难题），格式化输出让解析器 100% 命中，空位计数拦截"三空只答零空"。

### L8. 多层答案提取 + 来源标记 + 洁净度门 —— 解析器不是瓶颈
**机制**：章节 → 结论速览 → boxed/labelled/行级 → 裸客观行 → 散文捞回的五级提取（`reason_step.py:47-105`）；每级 `answer_source` 标记；`_distill_answer` 六策略；is_junk/is_committed_result/placeholder/latex 残片/推导碎片五道洁净门。
**为什么有效**：模型输出极不规范（漏标题、散文泄 CoT、截断残片、模板回显、元叙述），**解析器丢答案与模型答错同样致命**（实测 41k 字符正文解析为空、两次 1000s+ 白跑）。这套链路的哲学是"宁可多级捞回打低置信标记，也不让一个算对的答案被格式问题闷死"，同时用来源标记让下游（耗尽判定/仲裁）能区别对待置信度。

### L9. 确定性修复（不花 LLM 时间的修复）—— 成本为零的兜底
**机制**：`_append_answer_print` 给"定义了函数却没调用"的代码补主调用 + 打印（`verify_step.py:102-123`）；`_rhs_of_top_level_eq` 等纯字符串/正则修复。
**为什么有效**：LLM 短重试会被预算门禁拒绝（实测），而确定性改写零成本、可审计。执行仍受 60s 执行器时限保护——"能确定性地修，就不花 LLM 时间"。

### L10. 题面结构画像注入（structure_instruction）—— 定向防错
**机制**：正则探测"全部解/极值/开区间端点/第 k 序号/可行性+计数/F₂ 值域求和"（`question_profile.py:108-226`），逐条注入强制检查单（`structure_instruction`，`question_profile.py:269-332`）。
**为什么有效**：差一错误、漏支、端点不可达、F₂ 中按整数求和（六个 f 值全对最后得 3）——都是"推理跑完了之后的收尾核验缺失"。按题面语义定向注入的检查单成本极低（几行提示词），但直接决定"全对 vs 零分"的边界判分。

---

## 7. 对我们的启示（超越方向）

1. **把截断当常态来设计**：官方 cap=8192 时，"调大 max_tokens"是死路（只会更长的 CoT 截断）；答案前置 + prefill 续写 + 线索复用是经过实测验证的三件套，可直接借鉴；
2. **验证器必须反伪造**：没有 `verdict_authenticity` 这类静态实质计算检测，双路交叉验证会退化成自证；我们的验证提示词同样要写死"状态必须由实际比较产生"；
3. **时间是第一预算**：重试按实测成本定价、分级熔断、safeguard 配额——比"次数上限"可靠一个数量级，因为平台客户端的内部重试无法从外部控制；
4. **来源标记贯穿全链路**：answer_source 让"模型答错 vs 解析器漏掉 vs 捞回残句"在 trace 里可区分，是持续迭代的数据基础；
5. **可超越点**：该实现无温度递增/多数投票、无真正的"同一次截断内续传"（线索复用是近似）、证据契约靠提示词约定而非结构化输出（JSON schema）约束、压缩重试是对整个四章节的重生成而非逐节补全——这些是我们的升级空间。

---

*本文档基于对 `MathAgent-S1-New` 源码的完整阅读整理，所有行号对应阅读时文件版本（reason_step.py 实际 734 行、verify_step.py 实际 424 行，与任务描述的 678/393 行略有出入，可能为中间版本差异）。*
