# 起源数智 — 多径推理与过程审计数学智能体

> 2026 挑战杯人工智能赛道 · XH-202627「基于 Intern-S1 的数学智能体设计与推理创新」初赛作品
> 在一个公开 LangGraph 图架构参考基线之上，深度重构与扩展的原创版本；
> 依上游 AGPLv3 许可开源衍生，保留全部原始版权声明。

本项目是一个面向数学题自动求解的多智能体系统，以 LangGraph 为编排框架。相对基线架构，
起源数智围绕「**正确答案率**」这一唯一客观判分维度，针对上游方案在评委报告中暴露的
失分形态，新增五大机制：**异构双方法推理、过程审计智能体、确定性复算季后赛、难度感知
动态预算、模结构确定性守护**，并对符号等价判定与代码执行沙箱做了工程加固。

在 2026-08-12 官方规则更新（并发 3 / 单题 20min / 总运行 6h，超时未答不计分）后，
进一步升级为 **V2（全卷完成率 + 资源分级）**、**V2.1（证明结构对齐 + 提速）**、
**V2.3（初解质量 + 证明自检 + 难题兜底）**、**V2.4（prefill 抑制 CoT 提速 +
判分口径对齐 + 客观题格式兜底）**、**V2.5（token 分级上调 + 关键步骤含计算式 +
证据闸门堵漏 + 异构 B 路径全领域）**、**V2.6（深度反思环 + 判分器对齐官方口径）**、
**V2.7（给足 token + SymPy 代回硬验证 + RAG 注入量 4 倍）**、**V2.8（thinking_mode
深度思考 + RAG 检索修复 + 知识库三层覆盖）** 与 **V2.9（追方法差距：多候选验证链
修复 + N≥8 分层温度采样 + 题型六分类 + 零格式错误 + 研究生级知识库补齐）**：
累计 **M1-M14 + 反思环 + 多候选 N≥8 等 30+ 机制**（详见第 10 节）。

> ⚠️ **重大认知修正（2026-08-17）**：V2.6 官方实测 **53.57 分（60/112，反思环 +
> token 上调零提分）**，V2.8 官方实测 **58.04 分（65/112）**。关键认知：**官方题是
> 科研级（考定理链）+ judger 判"结论+过程"**，所以"方法差距"而非"能力天花板"是主因。
> V2.9 系统性修复了 14 处"架构让模型在会做的题上出错"的真 bug（B 路径被分类器置信度+
> 软预算双重挡住、B 反对票被忽略、证据闸门关键词漏配、fast_path 软预算误伤、token 分级
> 依赖分类器、知识库公式错误等），并补上官方评分五层规则要求的 N≥8 多候选、题型六分类、
> trace plan 步骤、消融实验。详见 `docs/V2.9方案.md`。

竞赛接口保持约定：评测侧导入 `user_agent.py` 的 `ReasoningAgent`，平台以其官方 client 构造，
调用 `solve(problem, metadata)` 取得 `final_response` 与 `trace`。

---

## 1. 为什么这样升级：基线失分点 → 机制对照

既有图架构基线的残余失分，来自其架构无法覆盖的四类形态：

| 基线失分形态（评委报告实录） | 根因 | 起源数智 机制 |
|---|---|---|
| F₂ 六值按整数相加得 3（应模 2 得 1） | 提示词"注意模结构"无强制力 | **模结构守护**：语境检出 + 提示强制条款 + 代码静态核查三重防线 |
| 多问漏答（三值报一）、三空答零空仍 0.78 置信放行 | 契约检查靠手写正则，必然漏 | **过程审计 Critic**：LLM 契约审计 + 计算抽核，缺口转定向修复 |
| 输出被自家程序反驳的答案（枚举得公式却输出 "4"） | 仲裁只能"选"不能"算" | **确定性复算 Playoff**：候选代回原题复算裁决 |
| 同一思路重复同一错误（重跑无信息量） | 单样本推理的系统性强偏差 | **异构第二推理路径**：构造/不变量/反向/生成函数强制换方法 |
| 简单题与奥赛题同吃 1200s 软预算 | 无全局预算观（6h÷并发3≈9.6min/题） | **难度感知软预算**：easy/medium/hard 三档，分类顺带输出 |
| `4π(32-a⁵)/5` 与等价 LaTeX 判不了落仲裁 | simplify 无超时、异名符号不判 | **matcher 加固**：限时代简、符号对齐映射、≤4 维采样 |
| 客观题单采样定生死 | 无独立复核 | **低置信复核投票**（仅空位覆盖不足时触发） |
| 执行代码可 import 任意库/读环境变量 | 沙箱只限时不限能 | **执行器加固**：危险能力静态拦截 + POSIX 资源限额 |

## 2. 系统架构

### 2.1 主图拓扑

```mermaid
flowchart LR
    Start([START]) --> Input[input] --> Classifier[classifier<br/>领域+难度画像]
    Classifier --> Solving[[solving 子图]]
    Solving --> Route{route_after_solving}
    Route -->|match| Critic[critic 过程审计]
    Route -->|mismatch·计算题| Playoff[playoff 复算裁决]
    Route -->|mismatch 可重试| Reconciliation[reconciliation]
    Route -->|uncertain/耗尽| Arbiter[semantic_arbiter]
    Playoff -->|decisive| Critic
    Playoff -->|NEITHER 双证伪| Reconciliation
    Playoff -->|indecisive| Arbiter
    Reconciliation -->|换方法重算| Solving
    Reconciliation -->|轮次尽| Arbiter
    Arbiter -->|selected/fallback| Critic
    Arbiter -->|abstain+可重试| Reconciliation
    Critic -->|pass| Coordinator[coordinator 成稿]
    Critic -->|缺口+可负担| Reconciliation
    Coordinator --> End([END])
```

与基线的拓扑差异：**所有答案进 coordinator 前必经 Critic 审计门**；计算题冲突
**先进 Playoff 再决定重算或仲裁**——重算从"第一反应"降级为"复算无效后的手段"。

> V2.1/V2.3 的增强（M7 证明深加工 → M12 逻辑自检 → M13 覆盖检查 → M14 难题兜底）
> 全部收敛在 coordinator 节点内部，不改变主图拓扑——成稿前做"结构补强 + 逻辑审查 +
> 覆盖核对 + 放弃兜底"四道后处理。

### 2.2 Solving 子图（多径扇出）

```mermaid
flowchart LR
    S([START]) --> FanOut{fan_out}
    FanOut -->|FastLane 单路径直出 M8| R1[reasoning_agent]
    FanOut -->|主路径 直接法| R1
    FanOut -->|method_variant=B<br/>异构方法 8选1| R2[reasoning_agent]
    FanOut -->|非客观题| P[python_agent]
    R1 --> V[cross_validator]
    R2 --> V
    P --> V
    V --> E([END])
```

第二推理路径（`method_variant="B"`）通过 `DIVERSE_METHOD_HINT` 强制切换方法论，
从八类独立方法中选一（V2.3 M11 由 4 类扩充）：构造枚举 / 不变量极端原理 /
反向归谬 / 生成函数递推 / 变量代换 / 逼近极限 / 对偶互补 / 特例反推，
与主路径形成方法论独立的双证据。触发纪律（控成本）：

- **FastLane（V2.1 M8）**：L1 客观题且分类高置信 → 单路径直出，跳过 B 路径与
  Python（省时间给难题）；
- **首轮**：仅高风险领域（离散数学、非基础及进阶课程、微分几何、测度积分——
  评测集占比最高且单样本错误率最高的四类）并行扇出；
- **重算轮**：一律追加（此时已知首路径失败，多样性正是缺的东西）；
- 客观题、时间 fast path、`enable_diverse_reasoning=False` 时不发。

三路比对规则：主路径与 Python 不一致但**第二路径与 Python 一致**时直接采纳第二路径
（异构方法的独立双证据，强于任何单路径）。

### 2.3 过程审计智能体（Critic）

定稿前的质量门，两级审计：

1. **契约完整性**：确定性契约（`answer_contract`，零成本）与 LLM 审计
   （prefill JSON，~15s）合并判定，保守方向——任一方报缺即缺。
   覆盖：多问项逐项、枚举全列+总数、区间双端点、检验结论句、极值+取到构造、
   "最小常数"类的上界论证+下界构造对。
2. **关键计算抽核**：LLM 挑 1-3 个中间计算独立复算，`calc_error` 是强于"缺项"
   的重算信号。

路由：pass → coordinator；缺口且可负担 → 调解阶段**定向修复**（缺口清单随提示下发，
修复轮次独立计数封顶 1 次，杜绝审计-重算死循环）；否则带缺口标记进 coordinator——
缺口清单会显性追加进成稿提示，完整说明成为最后一次补齐机会。

### 2.4 确定性复算季后赛（Playoff）

两路候选冲突时的第三选择（基线只有"整轮重跑"与"仲裁二选一"）：
**不解原题，只做候选代回核验**——方程回代看残差、极值实测目标值并查约束、
计数缩小规模暴力枚举对照、存在性实际搜索构造。

- `A`/`B`/`BOTH`：锁定被独立计算支持的候选 → Critic；
- `NEITHER`（双证伪，强信号）→ 带证伪信息回调解，换方法重算；
- `INCONCLUSIVE`/故障 → 回退既有的重试/仲裁通道，不放大损失。

成本 ≈ 一次 prefill 代码生成（~60-150s）+ 一次沙箱执行（≤60s），
显著低于整轮 solving 重跑（~500-900s）。每题至多进入一次（`playoff_trace` 防循环）。

### 2.5 难度感知动态预算

全局约束（2026-08 判分规则）：112 题、平台并发 3、智能体总运行 6h 封顶，
超出的未答题不计分 ⇒ 平均每题仅 ~9.6 分钟。分类 prefill 顺带输出难度画像
（边际成本≈0），`TimeBudget.apply_difficulty_profile()` 据此收紧**软预算**：

| 画像 | 软预算 | 含义 |
|---|---:|---|
| easy（含全部客观题） | 480s | 快速路径，停止购买可选 LLM 阶段 |
| medium | 840s | 保留一次重试与仲裁 |
| hard | 1200s | 全预算 |

安全边界：软预算只决定可选阶段购买力；`remaining_hard`（平台 1200s 硬限）永不下调，
进行中的调用不被截断，reserve 配额通道（压缩重试/仲裁/应急直答）照常可用。
简单题绝不会出现"被截断交白卷"。

### 2.6 其他加固

- **matcher**：`simplify` 8s 限时（超时降级采样，不再卡死关键路径）；自由符号
  异名时按基数相等尝试重命名映射（n≤3 全置换，n=4 排序直映）再采样；采样维度上限 4。
- **客观题复核投票**：空位覆盖不足等低置信形态触发一次独立短采样比对，
  一致提置信 0.9，分歧保留更完整一方并留证。
- **执行器**：静态拦截危险导入/调用（subprocess、socket、os.system、写模式 open 等；
  放行 `sys.setrecursionlimit` 等竞赛常用操作）；POSIX 下 RLIMIT_AS 2GB + RLIMIT_CPU
  + 独立进程组。
- **trace 可审计**：diverse_reasoning / deterministic_playoff / process_audit /
  objective_revote / modular_guard 各阶段证据全部入 trace，供 judger 综合判定。

## 3. 目录结构（相对基线的新增）

```text
├── nodes/
│   ├── critic_node.py            # 【新】过程审计智能体：契约完整性 + 计算抽核
│   ├── playoff_node.py           # 【新】确定性复算季后赛：候选代回裁决
│   ├── reasoning_agent_node.py   # 【改】+ method_variant="B" 异构路径调度
│   ├── cross_validator_node.py   # 【改】+ 三路比对、playoff 路由、复核投票
│   ├── classifier_node.py        # 【改】+ 难度画像解析与软预算应用
│   ├── reconciliation_node.py    # 【改】+ critic/playoff 定向修复直通
│   ├── semantic_arbiter_node.py  # 【改】+ 第三候选（reasoning_alt）
│   └── coordinator_node.py       # 【改】+ 审计缺口随成稿提示显性补齐
├── utils/
│   ├── critic_audit.py           # 【新】审计 JSON 解析与保守合并
│   ├── modular_guard.py          # 【新】F_2/Z_m 语境检出 + 结构内聚合核查
│   ├── time_budget.py            # 【改】+ apply_difficulty_profile 软预算
│   ├── judge_confirm.py          # 【新】判断题双向确认（结论→温度0自证→反向重解）
│   ├── counting_guard.py         # 【新】计数题小规模枚举对照 + 代码静态核查
│   ├── verify_router.py          # 【新】填空细分：概念填空快速路径 / 实算填空双路验证
│   ├── prefill.py                # 【新】prefill 组装与拼接（分类/仲裁/审计加速）
│   ├── problem_anchor.py         # 【新】题面契约哈希锚点（完整性事件）
│   ├── problem_profile.py        # 【新】题型画像（计算/证明/选择/判断/填空）
│   ├── numeric_crosscheck.py     # 【新】V2.4 数值比对兜底（LaTeX→sympy 精确比对）
│   ├── category_embedding_index.py  # 【新】领域候选的 TF-IDF 检索
│   ├── prompt_templates.py       # 【新】集中提示词模板（分类/推理/仲裁/审计）
│   ├── response_normalize.py     # 【新】官方 client 全形态响应归一化（0 分防线）
│   ├── verification_authenticity.py # 【新】证据真实性校验（防伪造）
│   └── answer_matcher.py         # 【改】+ 限时代简、符号对齐、≤4 维采样
├── mcp_servers/python_executor/server.py  # 【改】+ 静态安全扫描 + 资源限额
├── graph/solving_subgraph.py     # 【改】+ 条件扇出异构第二路径
├── tests/                        # 【新】153 项离线回归测试（基线未随仓附带）
│   ├── test_veritas_upgrades.py
│   ├── test_integration_graph.py
│   ├── test_integration_repair_loop.py
│   ├── test_integration_contradict.py
│   ├── test_integration_fill_verify.py
│   ├── test_m56_guards.py
│   ├── test_platform_defenses.py
│   ├── test_qiyuan_ports.py
│   ├── test_stress_faults.py
│   ├── test_v24_budget.py         # 【新】V2.4 token 分级 + 单题预算
│   ├── test_v24_grade_boost.py    # 【新】V2.4 数值比对/统计口径/执行修复
│   └── test_form_align.py         # 【新】形式对齐（含客观题格式）
└── （以下与基线一致）state/、skills_pythonscripts/（18 领域手册+验证知识）、
    main.py、user_agent.py、llm_client.py、config.py、requirements.txt
```

## 4. 运行

### 4.1 环境与依赖

Python 3.10+；`pip install -r requirements.txt`（全部为纯 Python 依赖，
评测 Docker 可正常安装；无 Lean 等非 Python 工具链）。

```bash
export INTERN_API_KEY="sk-..."
python main.py --input_file input.jsonl --output_dir outputs
```

环境变量：`INTERN_API_KEY`（必需）、`INTERN_API_BASE`、`INTERN_MODEL`
（默认 `intern-s2-preview-397b`）、`LOCAL_MAX_CONCURRENCY`（默认 8）。

### 4.2 接口契约

- 根目录 `user_agent.py`，`ReasoningAgent(client)` 由平台官方 client 构造；
- `solve(problem: str, metadata: dict) -> dict`，返回非空字符串 `final_response`
  与 `trace`；任何内部异常不外抛，单题失败不影响整卷；
- 无硬编码密钥；全部 LLM 调用经平台注入的 client；运行时不访问外网。

### 4.3 离线测试

```bash
python -B -m unittest discover -s tests -v
```

**153 项测试 + 30 项子测试**（`pytest tests/` 全绿），零 API 调用：升级机制单测
（审计解析/模守护/季后赛裁决/软预算/matcher 加固/执行器拦截/判断题双向确认/
计数枚举守护/平台防线三形态加载/PaperPacer/置信门控/DeepSolver/形式对齐/
限流退避/ProofDeepener/V2.4 数值比对/客观题格式）+ 多条全图集成链路（happy path；
双证伪→异构重算翻盘；contradict 假证据→playoff 裁决）+ 题面滥用/故障注入/ReDoS 压测。
Python 验证代码由真实执行器子进程运行。

### 4.4 本地测试与监控（local_test/）

本地评测工具链（离线，保留在开发机 `local_test/` 目录，不入评测仓库）：

```bash
python local_test/run_tests.py --input 题集.jsonl --concurrency 3 --live   # 批量求解（实时写盘）
python local_test/grade_llm.py --input 结果.json --concurrency 6            # LLM 判分（模拟官方 Judger）
python local_test/server.py --port 8000                                     # 监控面板（http://127.0.0.1:8000）
```

- **判分口径**：统一 LLM 判分（Intern-S 模拟官方 Judger，correct/wrong/partial），
  不使用本地符号/字符串判分器——与官方口径一致（模型综合判定，等价表达判对）。
- 面板实时显示：进度/正确率/API 次数/耗时/难度分布/推理过程（trace 展开）。
- `--resume` 支持断点续跑（跳过已成功题）。

## 5. 配置（新增项）

| 配置项 | 默认 | 作用 |
|---|---:|---|
| `difficulty_soft_budgets` | 480/840/1200 | 难度三档软预算（秒） |
| `enable_diverse_reasoning` | True | 异构第二推理路径总开关 |
| `high_risk_domains` | 离散数学等 4 领域 | 首轮即扇出第二路径的领域 |
| `enable_critic` / `critic_reserve_margin_s` | True / 60 | 过程审计开关与预留配额 |
| `enable_playoff` / `playoff_max_seconds` | True / 240 | 复算季后赛开关与预算 |
| `enable_objective_revote` | True | 客观题低置信复核投票 |
| `matcher_simplify_timeout_s` / `matcher_max_sample_symbols` | 8 / 4 | 等价判定加固 |
| `enable_modular_guard` | True | 模结构三重守护 |
| `enable_judge_confirm` | True | 判断题双向确认（结论→自证→反向重解） |
| `enable_counting_guard` | True | 计数题小规模枚举对照守护 |
| `node_timeouts.critic` / `playoff` | 120 / 300 | 新节点上限（仍受 deadline 钳制） |

## 6. 与基线的兼容性

- 入口、状态模型、依赖注入、断点续跑、全部韧性机制（分级熔断、压缩重试、
  结论捞回、证据优先闸门、反伪造检查、洁净度终门、应急直答）原样保留；
- 新增机制全部可经 `config.py` 独立关闭，关闭后图退化为基线拓扑
  （Critic 直通、Playoff 跳过、第二路径不扇出）；
- 许可合规：上游参考架构为 AGPLv3，本衍生作品同为 AGPLv3，保留 LICENSE 与出处说明。

## 7. 开发规范（后续迭代必须遵守）

> 本规范合并自启元 MathAgent 开发宪法与本项目图架构纪律。任何代码生成、
> 重构、修复、评审均以此为准；违反红线的提交不得合入 main。

### 7.1 平台红线（违反 = 整批 0 分）

- **入口形态不得变更**：`user_agent.py` 保持"sys.path 自举 + 导入守卫 +
  fallback 直调"结构；`ReasoningAgent(client)` 构造契约、`solve` 全兼容签名
  （`metadata=None` 默认 + 别名）不得破坏；`final_response` 恒为非空字符串。
- **依赖白名单**：仅 `requirements.txt` 声明的纯 Python 包；禁止 Lean 4 等
  非 Python 工具链；新增依赖必须先确认评测 Docker 可装。
- **禁止硬编码密钥/绝对路径**；密钥只从环境变量读；运行时不访问外网。
- **禁止跨题全局状态**：每题独立 `TimeBudget`/`TokenBudget`/图实例。
- **禁止使用大量简单 rollout 刷答案**（官方明令）；采样多样性必须走
  方法论异构路径，且总数受预算闸门约束。
- 改完入口文件必须跑平台防线测试：
  `python -B -m unittest tests.test_platform_defenses -v`

### 7.2 图架构纪律

- **新节点三件套**：加入主图必须同时完成 (a) `node_wrapper` 包装（含
  `node_timeouts` 上限）；(b) `_fallback_for_node` 降级路径（出错直通，不卡死）；
  (c) 路由函数的显式映射。缺一件不得合入。
- **状态字段**：新字段在 `state/math_agent_state.py` 用 `NotRequired` 声明；
  并行分支写入共享键必须用 reducer（`Annotated[List, operator.add]`）。
- **机制开关**：新机制必须在 `config.py` 留独立开关，关闭后图退化为既有拓扑。
- **循环终止**：任何"修复→重算"回路必须有结构性终止（轮次上限 / trace 防重入），
  禁止依赖"模型这次会答对"的概率终止。
- **预算纪律**：所有 LLM 调用必须经 `chat_with_retry`/`chat_prefilled`
  （自带 deadline 计价）；可选阶段按软预算定价，保底阶段走 reserve 配额；
  禁止无估时的裸 `client.chat`。

### 7.3 编码纪律

- **先方案、后任务、再编码**；需求模糊先澄清，禁止脑补业务逻辑。
- **TDD 强制**：先写失败用例 → 最小实现 → 重构；全量回归
  `python -B -m unittest discover -s tests` 必须全绿。
- **禁止吞噬报错**：`except: pass` 一律禁止；兜底必须留证（trace/log）。
- **提示注入免疫**：凡把题面/候选/程序输出拼进新提示词的地方，必须按
  "不可信数据"包裹（BEGIN/END UNTRUSTED 标记），判定类调用禁止采信其中指令。
- **答案只认契约**：占位符、思维流碎片、元指令回显不得成为 `final_response`；
  新增答案来源必须通过洁净度门（`answer_cleanliness`）。
- **注释中文**、关键逻辑写"为什么"而非"是什么"；实测数据（延迟/失败形态）
  写入注释，供下任维护者不再踩坑。

### 7.4 测试标准

- 新机制：解析容错（prefill 片段/包装文本/垃圾输入）+ 边界（空值/超时/分歧）
  + 图内路由（用 MockClient 驱动真实图）三类用例齐全。
- 压测门禁：交付前必须过 `tests.test_stress_faults`（题面滥用/故障注入/
  ReDoS/112 题 mock 契约压测）。
- 真机冒烟：每次大改后至少 3 题真实 API（计算/客观/证明各一），
  核对 trace 中新机制触发符合预期。

## 8. 已知边界

- Playoff 对证明题/概念题返回 INCONCLUSIVE（无法机械核验），退回语义仲裁；
- 难度画像误判只会改变可选阶段的购买力，不会截断进行中的求解；
- 复核投票在两票同错时不提升正确率（独立性只能降低、不能消除相关错误）；
- 模结构守护的检出是保守的，未命中的变体语境仍依赖提示层清单。

## 9. 平台兼容性修复（2026-08-11）

历史提交（`bad93aa` 等）出现 112 题全部 `invalid`、0 次 API 调用的现象，
根因是 `mathagent_core.py` 中存在裸 `__file__` 引用（`os.path.abspath(__file__)`）。
竞赛平台的代码加载器使用 `exec()` 不设置 `__file__` 变量，导致 `NameError`，
整个模块加载失败，`ReasoningAgent` 不存在，所有题目落入 fallback 返回空答案。

**修复方案**：所有 `__file__` 引用均包裹 `try/except (NameError, Exception)`，
平台无 `__file__` 时自动回退到 `os.getcwd()`。同时确保 `solve()` 签名
`metadata=None` 兼容平台不传 metadata 的调用方式。

**验证方法**：
```python
# 模拟平台 exec() 环境
namespace = {'__name__': '__main__', '__builtins__': __builtins__}
exec(compile(open('user_agent.py').read(), '<platform>', 'exec'), namespace)
agent = namespace['ReasoningAgent'](client=None)
agent.config.max_api_calls = 0
result = agent.solve('1+1', {'idx': 0})
assert 'final_response' in result
```

---

## 10. V2 → V2.1 → V2.3 升级总览（2026-08-12 ~ 08-13）

### 10.1 官方规则更新（2026-08-12）与项目合规

官方新规：**Agent 并发 3 · 单题最长 20 分钟 · Agent 总运行 6 小时，
超时未答不计分；零分常见原因：user_agent.py 缺失 / ModuleNotFoundError**。

| 规则 | 项目状态 |
|---|---|
| 并发 3 | ✅ 平台控制，本地 `--concurrency 3` 对齐 |
| 单题 ≤20min | ✅ `problem_time_budget_s=1200` |
| 总运行 ≤6h | ✅ `paper_total_seconds=21600`（PaperPacer 保障） |
| user_agent.py 存在 | ✅ 根目录 + 平台防线测试通过 |
| 依赖无缺失 | ✅ 全部声明于 requirements.txt（含 mpmath 版本对齐） |

### 10.2 全机制清单（M1-M14 + V2.4/V2.5/V2.6 确定性加固）

| 机制 | 模块 | 作用 | 版本 |
|---|---|---|---|
| M1 PaperPacer | `utils/paper_pacer.py` | 全卷 6h 预算池：动态收紧防超时，完成率保底 | V2 |
| M2 置信门控 | `utils/confidence_gate.py` | 置信分级投入：fast/standard/deep 三档 | V2 |
| M3 DeepSolver | `utils/deep_solver.py` | 超难领域强制异构 B 路径 + 机器验证 | V2 |
| M4 形式对齐 | `utils/form_align.py` | 题面形态识别 + 错配 LLM 重述 | V2 |
| M5 Token 分级 | `config.py` | reasoning 按难度分级（easy/medium 16384 · hard 24576，V2.6 摆脱分类器） | V2 |
| M6 限流退避 | `utils/llm_retry.py` | 429/配额错误长退避 | V2 |
| M7 ProofDeepener | `utils/proof_deepener.py` | 证明三段式结构补强 | V2.1 |
| M8 FastLane | `utils/confidence_gate.py` | L1 客观题单路径快速答 | V2.1 |
| M9 判分加固 | `local_test/grade_llm.py` | 判分重试 + 失败标记不误判 | V2.1 |
| M10 hard_plan | `utils/hard_plan.py` | 超难题"计划先行"（目标→路径→逐步求解） | V2.3 |
| M11 方法池扩充 | `utils/prompt_templates.py` | 异构方法 4→8 种 | V2.3 |
| M12 答案自检 | `utils/proof_deepener.py` | 审稿查逻辑漏洞（prefill 低成本） | V2.3 |
| M13 覆盖检查 | `utils/proof_deepener.py` | 检测"题目问几件事漏答" | V2.3 |
| M14 难题兜底 | `utils/hard_fallback.py` | 放弃型答案触发部分求解 | V2.3 |

V2.4 新增（2026-08-14，不新增编号，均为现有链路的确定性加固）：

| 机制 | 模块 | 作用 |
|---|---|---|
| prefill 抑制 CoT | `reasoning_agent_node` / `python_agent_node` | 用 chat_prefilled 预填开头跳过私有 CoT，单次调用 811s→41-77s |
| token 分级恢复 | `config.py` | easy 8192 / medium 12288 / hard 16384（V2.1 一刀切 8192 教训） |
| 数值比对兜底 | `utils/numeric_crosscheck.py` | Python 验证结果与推理候选确定性数值比对，mismatch→contradict |
| 统计口径对齐 | `prompt_templates.py` | 方差/标准差未明口径时按总体（÷n）作答 |
| 执行失败修复重试 | `python_agent_node.py` | 代码执行失败给一次独立修复（预算门禁保护） |
| 客观题格式对齐 | `form_align.py` / `problem_profile.py` | 选择/判断/填空答案形态识别 + 归一化 + 错配重述 |

V2.5 新增（2026-08-15，方向 1 + 收益点）：

| 机制 | 模块 | 作用 |
|---|---|---|
| token 分级上调 | `config.py` | medium 12288→16384、hard 16384→24576（对冲官方 truncated=91 截断） |
| 关键步骤含计算式 | `answer_formatter.py` | `_condense_step` 从"标题+定理名"升级为"标题+关键计算式"，修复 `**` 空壳 |
| 证明过程不截断 | `answer_formatter.py` | `_MAX_PROOF_BODY_CHARS` 4200→8000 |
| 证据闸门堵漏 | `coordinator_node.py` | `_evidence_override` 不再被 `answer_locked` 跳过 |
| 异构 B 路径全领域 | `solving_subgraph.py` | B 路径触发从 4 领域白名单改为 hard 难度/低置信全领域 |

V2.6 新增（2026-08-15，追 80+ 的范式级改造）：

| 机制 | 模块 | 作用 |
|---|---|---|
| 深度反思环 | `nodes/reflection_node.py` | Critic 后对答案做"正确性"批判性复核（代回/差一/符号/边界/反例），判错复用 reconciliation 重算 |
| token 摆脱分类器 | `config.py` | easy 8192→16384（分类器把科研级题判 easy 导致截断，见 10.7） |
| 判分器对齐官方口径 | `local_test/grade_llm.py` | 判分 prompt 从"只判答案等价"改为"判答案+过程合理性" |
| fix_hint 去锚定 | `reflection_node.py` | 剥离"应为 X"数值锚定，防反思误判把对的改错 |

### 10.3 全量验收（五套 112 题 · 官方 18 领域分布）

| 题集 | 难度构成 | 正确率 |
|---|---|---|
| 第一套 | L3 ~80% + L4 ~20% | **96.4%（108/112）** |
| 第二套 | L3 59 题 + L4 53 题（纯 L3/L4） | **97.3%（109/112）** |
| 第三套（V2.4） | L2 12 + L3 71 + L4 29（贴近官方分布） | **98.2%（110/112）** |
| 第四套（V2.5 陌生应用） | 定理的陌生数值应用，答案唯一可验证 | **97.0%（97/100，剔除 11 题 API 流控）** |
| 第五套（V2.6 纯 L4 科研级） | L3 22 + L4 90（科研级占 80%） | **95.96%（95/99，剔除 13 题 API 流控）** |

- **L3 竞赛级**：100%（59/59）· **L4 科研级**：94.3%（50/53）
- 完成率各套均 112/112（0 空卷）；V2.4 墙钟 **1.46h**（并发 3，官方 6h 上限余量 4.5h）
- ⚠️ **本地 96-98% ≠ 官方 59.8%**：官方 V2.4 实测 59.82 分（67/112），三套题集、
  三种判分口径均无法复现，落差根因在官方黑盒（详见 10.7 与 `docs/V2.6方案.md`）

### 10.4 难度分级定义（L1-L4）

| 级别 | 名称 | 定位 | 典型题 |
|---|---|---|---|
| L1 | 基础级 | 高中竞赛入门/大一基础 | 求定积分、行列式 |
| L2 | 进阶级 | 本科核心课 | S₃ 子群、可数集测度 0 |
| L3 | 竞赛级 | 研究生课/IMO 级 | Picard、Cauchy 公式、Fubini |
| L4 | 科研级 | 多步定理链严格证明 | Hall、Sperner、Gauss-Bonnet、Hahn-Banach |

**能力水位**：V2 裸能力 L1 100%/L2 94%/L3 76%/L4 58% → **V2.3 机制后 L3 100%/L4 94.3%**

### 10.5 版本演进里程碑

```
V1（官方 53.53 分）→ V2（PaperPacer 完成率）→ V2.1（M7/M8/M9 证明结构）
→ V2.3（M10-M14 初解质量 + 证明自检 + 覆盖检查 + 难题兜底）
→ V2.4（prefill 提速 + 判分口径对齐 + 客观题格式兜底，官方 59.82/53.57 两次）
→ V2.5（token 分级上调 + 关键步骤含计算式 + 证据闸门堵漏 + B 路径全领域）
→ V2.6（深度反思环 + 判分器对齐 + fix_hint 去锚定，官方 53.57 零提分）
→ V2.7（给足 token + SymPy 代回硬验证 + RAG 注入量 4 倍）
→ V2.8（thinking_mode 深度思考 + RAG 检索修复 + 知识库三层覆盖，官方 58.04 分）
→ V2.9（追方法差距：14 处架构缺陷修复 + N≥8 分层采样 + 题型六分类 + 零格式错误 +
       研究生级知识库补齐 + trace plan + 消融实验，未提交官方评测）
```

详见 `docs/V2.3验收报告.md`（含 L1-L4 定义与分层数据）与 `docs/V2.9方案.md`。

### 10.6 V2.4：prefill 提速 + 判分口径对齐 + 客观题格式兜底（2026-08-14）

#### 核心突破：prefill 抑制 CoT

Intern-S2 模型的私有思维链（CoT）计入 `max_tokens`，慢 API 下单次 reasoning
可到 811s（16384 token）。V2.4 用 `chat_prefilled` 预填开头（reasoning 用
`## 问题分析`、python 用 ```` ```python ````）让模型跳过 CoT 直接作答，思考
引导改由提示词内化承担——**单次调用 811s → 41-77s（10 倍提速）**，10 题 L4
难题验证 100% 正确 + 平均 3.8min。

#### 三项判分口径对齐（针对 V2.4 全量评测 2 道错题根因）

| 错题 | 根因 | 修复 |
|---|---|---|
| idx=85 留数符号 | Python 验证内部自洽 PASS 但符号与候选相反，support 证据出厂 | **数值比对兜底**：`numeric_crosscheck` 确定性比对，mismatch→contradict |
| idx=92 方差口径 | 官方总体口径（÷n）vs 智能体无偏（÷n-1） | **统计口径提示**：未明口径按总体作答，两种口径都注明 |

外加 **执行失败修复重试**（代码语法错误给一次独立修复）。

#### 方向 5：客观题格式兜底（选择/判断/填空）

官方题集若对客观题精确匹配，格式即生死线。`form_align` 新增 choice/fill
形态识别（选项标记 ≥2 优先于"判断"字样/空括号，修复"判断下列说法正确的是
（  ）A. B. C. D."误判为判断题的边界），判断题判定词补全（正确/错误/对/错/√/×），
错配触发低成本 LLM 重述 + choice 答案确定性归一化（"选B"→"B"）。

#### V2.4 全量评测（110/112 = 98.2%）

- 全新 112 题（L2 12 + L3 71 + L4 29，与 v23b/v23c 查重 0 重复）
- **110 correct / 2 wrong / 0 partial / 0 error**；medium 48/48、hard 7/7 全对
- 并发 3 墙钟 1.46h（官方 6h 余量 4.5h）；总 API 调用 1761（平均 15.7/题）
- 保守官方分预估 60-68 分（中枢 63-65，稳超 V1 53.53；中性 68-75，乐观 76-85）

#### 测试规模

**153 项测试 + 30 子测试全绿**（较 V2.3 新增 `test_v24_budget`、
`test_v24_grade_boost`、`test_form_align` 客观题 9 用例）。

### 10.7 V2.5 / V2.6：追 80+ 的范式级改造（2026-08-15）

#### 背景：官方实测打脸"本地高分"幻觉

V2.4 官方实测 **59.82 分（67/112）**，而本地第三套题集 98.2%。落差 43 题。
官方 eval 日志三版本轨迹：

```
V1:   60 correct = 53.53 分 / 1131请求 / 5h   / 4.3M token / 1 error
V2.1: 45 correct = 40.18 分 / 825请求  / 8h   / 1.66M token / 19 error / 22 invalid
V2.4: 67 correct = 59.82 分 / 892请求  / 2.5h / 7.6M token / 0 error / 0 invalid
```

关键信号：`truncated_count=91`（10.2% 请求输出截断）、`completion` 平均仅 2151 token。

#### V2.5：token 分级上调 + 过程可读

- **token 分级上调**：medium 16384、hard 24576，对冲 truncated 截断（prefill 抑制
  CoT 后 token 只花在可见答案，给足 ≠ 慢生成）
- **关键步骤含计算式**：`_condense_step` 从"标题+定理名"升级为"标题+关键计算式"，
  修复 description 里 `**` 残留导致空壳的 bug——官方 judger 终于能看到"支撑结论的计算过程"
- **证明过程不截断**（4200→8000）、**证据闸门堵漏**（answer_locked 不再跳过）、
  **B 路径全领域**（hard/低置信时任何领域都扇出异构第二路径）

#### V2.6：深度反思环 + 判分器对齐

- **深度反思环**：官方实测 `retry_count=0` 揭示根因——reasoning+python 两路"match 一致"
  就直达出厂，即使两路一致地错（单样本系统性错误）也没任何环节回答"答案对不对"。
  新增 `reflection_node` 挂在 Critic 后，对答案做"正确性"批判性复核（代回/差一/符号/
  边界/反例），判错复用 reconciliation 定向重算（合规，非简单 rollout）。
- **token 摆脱分类器**：真机发现分类器把 L4 科研级题判 easy + 0.98 高置信却照样答错，
  easy 档 8192 会让"被判 easy 的难题"答案被截断。easy 提到 16384（max_tokens 是上限
  非配额，提高不增加耗时）。
- **判分器对齐官方口径**：`grade_llm.py` 从"只判答案等价"改为"判答案+过程合理性"，
  对齐官方"结合参考答案与解题过程综合判定"。
- **fix_hint 去锚定**：反思 LLM 与主模型同源会误判（idx=28 反思说"应为 198"实际 748），
  剥离 fix_hint 里"应为 X"数值锚定，防误判把对的改错。

#### 三连证伪（重要认知，勿再重蹈）

本地五套题集 + 三种判分口径，全部无法复现官方 59.8% 的落差：

| 假设 | 验证方法 | 结果 |
|------|---------|------|
| 题集太简单 | 换陌生应用题集（v25）、纯 L4 科研级（v26） | 96-97% ≈ 98%，证伪 |
| 过程缺失 | 判分器加"判过程"口径 | 只掉 1 题，证伪 |
| 判分口径太松 | 判分器对齐官方"判过程" | 只掉 1 题，证伪 |

**结论**：本地题模型确实会做（~96%），官方 59.8% 的落差根因在官方黑盒（官方 judger
具体实现 + 官方题集具体内容），本地无法复现。详见 `docs/V2.6方案.md`。

#### 测试规模

**181 项测试 + 30 子测试全绿**（新增 `test_v25_step_condense`、`test_v25_gaps`、
`test_v26_reflection` 共 12 用例）。

### 10.8 V2.7 / V2.8：破解"零提分"的结构性修复（2026-08-16）

#### 背景：V2.6 官方实测 53.57 分（零提分）

```
V1:   60 correct = 53.53 分
V2.1: 45 correct = 40.18 分（超时事故）
V2.4 第一次: 67 correct = 59.82 分
V2.4 第二次: 60 correct = 53.57 分（同代码 ±6 分题集波动）
V2.6: 60 correct = 53.57 分 ← 反思环 + token 上调全部失效，retry_count=0
```

三个硬证据：① `retry_count=0`（反思 LLM 与主模型同源，永远判 correct 漏判）；②
`truncated=112`（token 上调没降截断，根因是 CoT 塞满 max_tokens 无底洞）；③
completion 平均 2047 token（答案普遍没写全）。精读官方 87 页 PDF 找到破解钥匙。

#### V2.7：给足 token + SymPy 代回 + RAG 注入量 4 倍

- **给足 token**：官方「不额外限制 token + 可用思考模式」，medium 24576 / hard 32768
- **SymPy 代回硬验证**：反思判 correct 后加机器代回（答案代回原题算残差），抓到 LLM
  反思漏判的算术错误（真机 5/5 全对，含抓差一、抓错值）
- **RAG 注入量 4 倍**：主求解 3000→12000、复核 2200→8000、Python 验证 2000→4000

#### V2.8：释放深度思考 + 修复 RAG 失效 + 知识库三层覆盖

| 改动 | 根因 | 效果 |
|------|------|------|
| **thinking_mode 深度思考** | prefill 一直强制模型"不思考硬答"（方向性错误） | 释放 Intern-S 核心推理能力，救回 v26 曾答错的 2 类题 |
| **RAG 检索正则修复** | `_MODULE_RE` 只识别 2 种标题，12/18 领域公式速查表从 V1 起从未被注入 | 复分析模块 0→14，留数定理终于能检索到 |
| **知识库补全 15 领域** | 缺研究生级定理 | 有限域生成元计数（官方样例 72 分题）、Cramer-Rao 等答对 |
| **反思环深度思考** | 把关人还在 prefill 浅思考"闭眼" | 深度复核抓到浅思考漏判的错误 |
| **证据闸门方向性修复** | Python 自己写错符号的 contradict 误覆盖主推理正确答案 | 洛朗题 +Σ → -Σ |
| **trace 脱敏回退** | PDF 要求"记录候选答案"，之前误删 | trace 符合官方"记录候选答案+脱敏密钥"口径 |

#### 真机验证（V2.8）

- 10 道 L3/L4 科研级题 **10/10 全对**（含 v26 曾答错的 S₃ 中心阶、洛朗展开）
- 官方样例「有限域 F₈₁ 生成元计数」答对 **72**（真子域补集 + Möbius 反演交叉验证）
- Cramer-Rao 下界答对 1/n、洛朗符号题答对 -Σ
- 平均 ~190s/题，全卷约 2h（6h 上限留 4h 余量）

#### 测试规模

**183 项测试 + 30 子测试全绿**。

### 10.9 V2.9：追方法差距——多候选验证链修复 + 官方评分五层规则对齐（2026-08-17）

#### 核心认知

V2.8 官方实测 58.04 分（65/112）+ `retry_count=0`（反思环 112 题从未判错）暴露了
本质：**官方题是科研级（考定理链）+ judger 判"结论+过程"**，所以主因是"方法差距"
而非"能力天花板"。V2.9 系统性地修复了 14 处"架构让模型在会做的题上出错"的真 bug。

#### 14 处改动清单

| # | 改动 | 解决的失分 |
|---|------|-----------|
| 1 | B 路径全量扇出 | retry=0 根因：分类器置信度 + 软预算双重挡住异构验证 |
| 2 | 三路投票 | B 反对票被忽略（A=Python 同源一致放行） |
| 3 | 证据闸门误差关键词漏配 | "反例/差值"漏配，Python 错答案覆盖正确答案 |
| 4 | fast_path 改硬预算 | easy 题软预算恒 fast_path，必要重试被跳过 |
| 5 | token 分级统一 32768 | 分类器误判 easy 截断长证明 |
| 6 | 知识库有限域生成元公式修正 | n 多素因子需容斥（F₆₄=54 非 56） |
| 7 | 多候选 N≥8 分层温度采样 | 官方"3 候选仅够跑通远不够满分" |
| 8 | 题型六分类 | 补"推导/解释"两类 |
| 9 | invalid 兜底统一 | "无法给出答案"→"最终答案：未能求解"，消除格式错误 |
| 10 | trace plan 步骤 | 第三层要求记录设计思路 |
| 11 | 反思环误判防护 | 判 wrong 先 SymPy 代回确认，避免误判重算 |
| 12 | 采样时间保护 | 采样循环缺预算保护，慢 API 逼近硬限 |
| 13 | 删死配置 high_risk_domains | B 路径全量扇出后遗留 |
| 14 | 5 领域研究生级定理补齐 | 泛函三大定理/测度 Hahn 分解/微分 Ricci/数值特征值扰动/离散谱图 |

#### 真机验证（V2.9）

- 12 题混合难度 **12/12 全对**（含洛朗、Hilbert、有限域、平均曲率等科研级题）
- 4 题真科研级定理题 **4/4 全对**（F₆₄=54、高斯曲率 4/81、留数 πie、Sylow n₇=1）
- 洛朗题：修复前 `-z⁷`（错）→ 修复后 `-1/z-Σ`（对）；Hilbert：324.5 → 748
- 平均 ~125s/题，全卷估算 ~1.5h（6h 内安全）

#### 官方评分五层规则对照

| 层 | 状态 |
|----|------|
| 第一层 工程契约 | ✅ 达标（invalid 已修，零格式错误） |
| 第二层 正确性 | ✅ 多候选 N≥8 + 题型六分类 + 三重验证 + 反思闭环 + 投票全齐 |
| 第三层 系统设计 | ✅ 多模块协同 + trace plan 步骤 |
| 第五层 创新性 | ✅ 5 项创新机制 + 消融实验（`docs/消融实验.md`） |

#### 测试规模

**183 项测试 + 30 子测试全绿**。代码审查（工具 + 人工 AST）无真实 bug。

