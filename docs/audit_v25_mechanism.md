# V2.5 机制缺陷审计报告（audit_v25_mechanism）

- **审计对象**：`C:\Users\34707\Desktop\math_competition_agent (1) - 副本`（VeritasMath 数学竞赛智能体）
- **审计版本**：git HEAD = `d396205`（"feat: V2.5 确定性计算前置——嫁接榜一 sympy 事前求解三件套"），V2.5
- **审计范围**：`nodes/`、`utils/`、`graph/`、`config.py`、`langgraph_math_agent.py`、`user_agent.py`
- **审计目的**：逐一核对 V4/V5 审计中发现的 10 项机制缺陷在 V2.5 中是否依然存在
- **方法**：静态代码审计（读码 + grep 交叉验证），未修改任何项目代码
- **结论摘要**：

| # | 缺陷 | 存在性 | 严重度 |
|---|------|--------|--------|
| 1 | 验证器同源确认偏误 | 存在（有部分缓解） | P1 |
| 2 | 弱验证错锁 | 部分存在 | P1 |
| 3 | coordinator 不可信边界 | 存在 | P1 |
| 4 | SC（self-consistency）缺失 | 存在（缺失，仅小 N 投票） | P2 |
| 5 | 交错推理/代码主解与冲突处理 | 部分存在 | P2 |
| 6 | 预算/时间控制 | 基本健全（有工程隐患） | P2 |
| 7 | 采样策略 | 部分存在 | P3 |
| 8 | 答案选择仲裁 | 存在 | P1 |
| 9 | 分类器 | 部分存在 | P2 |
| 10 | sympy_pre_solver | 存在（接线正确但误触发风险高） | P1 |

严重度定义：**P0** 致命（必然导致全卷失败）；**P1** 严重（大概率导致单题错误答案出厂/失分）；**P2** 中等（特定场景失分或资源浪费）；**P3** 轻微（边缘场景/工程隐患）。

---

## 1. 验证器同源确认偏误 —— 【存在，部分缓解】P1

**结论**：V2.5 的"独立证据"是**同源模型生成的 Python 代码在沙箱中的确定性执行**，并非真正独立于 LLM 的证据链；且存在"match 即锁定答案"的出厂路径。验证 PASS 会在功能上锁定答案。

**证据**：
- 推理、Python 代码生成、仲裁、Critic、coordinator 全部使用同一模型 `intern-s2-preview-397b`（`config.py:2`）。Python 验证代码由同一模型编写（`nodes/python_agent_node.py:352-362` `chat_prefilled(..., label="python")`），且把推理候选直接喂给代码生成器：`python_agent_node.py:232-237`（"待核验候选（不可信，仅用于独立核对）"——提示上声明独立，但写代码的仍是同一模型，确认偏误从"结论层"转移到"代码层"）。
- 证据判定是**静态启发式 + 字符串标记**，不是独立复算：
  - `utils/verification_evidence.py:51-125` `parse_verification_evidence`：证据状态来自 stdout 里的 `验证状态: PASS/FAIL` 标记与正则扫描（`_STATUS_RE`/`_CONTRADICTION_RE`），PASS 且进程成功 → `support`。
  - `utils/verification_authenticity.py:77-107` `assess_verification_authenticity`：只做静态反伪造（有无循环/求解调用/权威引用），注释明确"宁可放过"（L18-19），是保守过滤器而非独立验证。
  - `utils/numeric_crosscheck.py:139-181` `numeric_crosscheck`：比对的是"推理候选 vs Python 结果"——**两个同源模型的输出**，不是与标准答案的独立对照。
- **match 即锁定**：`nodes/cross_validator_node.py:419-441` match → 写 `validated_answer` 并直送 coordinator；`coordinator_node.py:233` 以 `validated_answer` 为最终答案；`answer_formatter.py:598-669` `post_process_final_response` 以 `format_answer_for_output(validated_answer)` 为 `fa` 输出"最终答案：{fa}"。验证 PASS 后答案内容不再被挑战（仅形式改写）。
- **客观题完全无独立证据**：`cross_validator_node.py:185-268` 客观题走快速路径（无 Python），复核投票 `_objective_revote`（`cross_validator_node.py:113-163`）与判断题双向确认 `judge_confirm`（`cross_validator_node.py:230-255`）**都是同一模型再采一次样**——纯同源自评。

**缓解因素**（为何不是 P0）：Python 代码在沙箱 MCP 中真实执行（`utils/python_mcp_client.py`），算对才算对；反伪造静态检查拦截"不计算只断言"；计算题 mismatch 先送 playoff 确定性复算（`cross_validator_node.py:333-384`）。

**V2.5 实际影响**：A、Python 同源一致地错（"同源一致地错"）时，错误答案以"验证通过"形态出厂；客观题（尤其 choice/true_false）单样本同源自评出厂。V2.5 已有一个针对性的补丁（三路投票，`cross_validator_node.py:386-417`：A 与 Python 一致但 B 反对时送 playoff 裁决 A vs B），但 B 路径只在高风险领域/重算轮扇出（`graph/solving_subgraph.py:11-41`），覆盖不全。

---

## 2. 弱验证错锁 —— 【部分存在】P1

**结论**：V2.5 保留了"仲裁锁定 → coordinator 逐字输出且跳过证据闸门"的机制，**锁定不要求数值证据强度**；V4 的"官方答案 100 却锁了 50"场景在 V2.5 的结构上仍可复现。但有部分缓解。

**证据**：
- 锁的语义：`nodes/semantic_arbiter_node.py:381-389`，只要仲裁选定候选（且非 unresolved_contradiction）即 `answer_locked = True`，**不检查 Python 是否有数值证据**。
- 锁的效力：`nodes/coordinator_node.py:266-267` `if not state.get("answer_locked"): validated, evidence_note = _evidence_override(...)`——**锁定后证据优先闸门被跳过**；`coordinator_node.py:280-324` 锁定答案逐字输出（"Do not let another LLM or formatter paraphrase"）。
- 弱验证能产生 match：`cross_validator_node.py:256-268` 客观题快速路径在无数值证据下直接置 `status="match"`、`confidence=0.45~0.78`（空位覆盖不足时 0.45）——弱 match 同样形成"验证通过"的出厂路径。
- 缓解：
  - 新 match 会复位锁：`cross_validator_node.py:502-505` `result["answer_locked"] = False`。
  - 仲裁有契约兜底 `_enforce_contract`（`semantic_arbiter_node.py:152-189`），只在"可证明的字段覆盖缺失"时改选，**不验证数值正确性**。
  - 计算题冲突先 playoff（`cross_validator_node.py:442-467`）。

**V2.5 实际影响**：仲裁锁定的候选（同源模型从同源候选中选出）以"结论：/最终答案："逐字出厂，绕过证据覆盖与洁净度门。若仲裁在 `{50, 100}` 中错选 50，即复现 V4 的错锁事故；只有当 BOTH 都错/字段缺失等少数情形才被兜底纠正。

---

## 3. coordinator 不可信边界 —— 【存在】P1

**结论**：V2.5 **没有** V4 修复中的 BEGIN/END UNTRUSTED 边界包裹；coordinator（及 form_align、应急直答）的 LLM 输出可未经结构化约束直接进入 `final_response`。`UNTRUSTED` 标记只存在于仲裁的候选展示块，且那是对"候选文本"的标记而非对输出的约束。

**证据**：
- `grep "UNTRUSTED"`：仅 `nodes/semantic_arbiter_node.py:259-263`（候选块包装，用于仲裁输入，非输出边界）。
- coordinator 主路径：`nodes/coordinator_node.py:390-400` `raw = chat_with_retry(..., label="coordinator")` → `coordinator_node.py:433-434` `cleaned_raw = strip_cot_prefix(raw)` → `post_process_final_response(cleaned_raw, validated, ...)`。提示词只"请求"模型重述 validated（`coordinator_node.py:372-380` `COORDINATOR_PROMPT.format(... validated_answer=validated)`），**没有任何机制强制模型输出 == validated**。
- 多条路径让 LLM 散文/幻觉进入 final_response：
  1. **证明题**：`utils/answer_formatter.py:605-616`，证明题输出 `结论：{fa}\n\n{cleaned}`——coordinator 的**整段未经验证的正文原样出厂**；且 `_prefer_complete_proof_answer`（`answer_formatter.py:528-546`）允许 coordinator 自写的"最终答案"章节在完整性得分更高时**替换** validated。
  2. **多问项**：`answer_formatter.py:639-643`，LLM 章节更长时替换 fa。
  3. **结构化计算题**：`answer_formatter.py:644-645` + `_prefer_structured_final_section`（L253-260），LLM 章节字段得分高时替换 fa。
  4. **fa 残缺时**：`answer_formatter.py:630-638`，回退到 LLM 章节/`_extract_fallback_answer`（L123-161，扫描 LLM 正文里的 boxed/结论行）。
  5. **form_align 重述**：`coordinator_node.py:239-255`，`_form_align_reframe`（L168-225）是又一次同源 LLM 单行输出，**直接替换 validated**，无正确性校验。
  6. **应急直答**：`coordinator_node.py:109-165` `_emergency_direct_answer`——同源模型"猜一个最可能答案"，仅过占位符/噪声门，**无正确性校验**，直接 `_with_emergency_answer` 出厂（L357-363）。
- 洁净度门只挡"噪声/占位符/残片"（`coordinator_node.py:273-279`），**不校验数学正确性**，也不校验输出是否与 validated 一致。

**V2.5 实际影响**：coordinator 叙述中若出现幻觉结论（重述错值、多写一个值、漏写字段），在多问项/证明/结构化/残缺回退等路径下会以最终答案出厂；计算题主路径虽偏好 validated，但上述分支使幻觉仍有实际出口。61.61% 成绩下此类事故应已零星发生（代码注释中 idx 20/87/94 等均为该家族事故）。

---

## 4. SC（self-consistency）缺失 —— 【存在（缺失）】P2

**结论**：V2.5 **没有** `sc_consensus` 节点（源码中零引用；`nodes/__pycache__/sc_consensus_node.cpython-311.pyc` 是早期版本残留的编译产物，无对应源文件，不参与构建）。系统性地依赖单样本推理，仅有两个狭窄的补充采样通道。

**证据**：
- `grep "sc_consensus|self_consistency"`：源码零命中。
- 补充采样（均非通用 SC）：
  - 小 N 投票：`nodes/reasoning_agent_node.py:764-822` `_should_sample_vote`/`_sample_vote_reasoning`——N=3、温度 [0.4, 0.8, 1.0]、多数 ≥2 才采用；**仅 hard 难度或分类低置信（<0.85）的计算题首轮触发**，中置信/常规题仍单样本。
  - 客观题复核投票：`cross_validator_node.py:113-163`（fill 题必投，choice/true_false 低置信时投）。
  - 判断题双向确认：`cross_validator_node.py:230-255`（温度 0 第三票）。
- 设计注释明确承认单样本偏差：`reasoning_agent_node.py:828-831`（"单样本推理的系统性强偏差无法靠'同样提示再跑一次'消除"），替代方案是 B 异构路径（`DIVERSE_METHOD_HINT`），但 B 路径仅高风险领域/重算轮扇出（`graph/solving_subgraph.py:11-41`）。

**V2.5 实际影响**：中置信常规题的**系统性偏差（同符号错、同类计数错）无法被打破**——这正是 V2.9 用 N≥8 分层采样要解决的问题（本仓库历史 902e073 显示 V2.9 曾实现 N≥8 但回退，说明团队已知）。61.61% 分下仍有天花板：单样本"碰巧错"的题（如 idx 13/40 填空）靠 Python 分支兜，但概念题/证明题无机器兜底。

---

## 5. 交错推理/代码主解与冲突处理 —— 【部分存在】P2

**结论**：V2.5 中 Python（sympy）对**计算题是主解路径**（"deterministic > LLM prose"），但 match/uncertain 分支下**正确的代码答案仍可能被推理答案覆盖**；冲突处理链完整（playoff → reconciliation → arbiter → evidence override）。

**证据**：
- 代码主解：`nodes/cross_validator_node.py:104-107` `_preferred_answer`——`ptype == "computation" and python_ok and 答案完整` → 选 Python；`cross_validator_node.py:86-89` contradict 且 Python 持不同可用答案 → 选 Python（证据优先最后防线）。
- 冲突处理链：
  - playoff：`cross_validator_node.py:333-384`（contradict）、`442-467`（mismatch）→ `nodes/playoff_node.py:91-210` 候选代回复算，decisive 则锁定 A/B/BOTH。
  - reconciliation：`nodes/reconciliation_node.py:18-128`，两侧独立重解（`reconciliation_node.py:100-109` 明确不把 Python 答案灌给推理侧防锚定）。
  - 最终证据闸门：`coordinator_node.py:19-55` `_evidence_override`（被自家验证器反驳的答案不得出厂）。
- **正确代码答案被丢弃的场景**：
  1. **match 时推理优先**：`cross_validator_node.py:90-93`——匹配成功且推理完整 → 输出推理答案（"retain the already validated human-readable reasoning form"）。若 matcher 把"相似但不同"误判为 match（字符串/符号化简局限），**正确的 Python 答案被同化的错误推理答案替换**。
  2. **uncertain 时**：`cross_validator_node.py:94-103`——仅当 Python 的缺失字段数**严格更少**才选 Python；缺失数相等时**仍选推理**，即使 Python 是正确答案。
  3. `cross_validator_node.py:104-107` 的 Python 主解被 `fabricated and reasoning_ok` 例外绕过（反伪造误伤面）。

**V2.5 实际影响**：计算题大部分时候能保住正确的代码答案（主解优先 + contradict 兜底），但 match/uncertain 的"推理优先"分支存在把正确代码答案换成错误推理答案的窗口；多字段题（缺失数相等）最危险。

---

## 6. 预算/时间控制 —— 【基本健全，有工程隐患】P2

**结论**：全卷 6h 预算由 PaperPacer 按"剩余时间 ÷ 剩余题数"动态收紧**软预算**（以 5h 为目标留 1h 余量），单题硬上限 1200s 由 TimeBudget + node_wrapper 双重钳制，**单题无法在结构上超过 1200s**；LLM 调用无硬次数上限，但每次调用都先过可负担性检查，重试仅 1 次。存在两个工程隐患。

**证据**：
- 全卷分配：`utils/paper_pacer.py:89-117` `budget_for`（落后时 `cap = max(MIN_SOFT=120, per_problem)`，健康时 IDEAL=1200）；`config.py:117` `paper_total_seconds=18000`（5h 目标）；接线 `langgraph_math_agent.py:104-117`（`clock.paper_cap = paper_cap`，与难度画像取 min）。
- 单题硬限：`utils/time_budget.py:47-76`（`total=1200` 硬限、`reserve=300`、`fast_path_threshold=300`）；`utils/error_handler.py:163-208` `node_wrapper` → `utils/timeout_control.py:37-59` `resolve_timeout`/`run_with_timeout`——节点 ceiling 被 `remaining_hard` 钳制，超时即 fallback。
- LLM 调用：`utils/llm_retry.py:71-92` `_affordable`（每次调用前查时钟；`reserve_margin_s` 模式允许动用 reserve 但必须留收尾余量）；`config.py:6` `llm_max_retries=1`、`max_retries_per_node=1`。
- **隐患 1**：`PaperPacer.reset()` 只在测试中调用（`tests/test_paper_pacer.py:26,70-73`），生产路径从不 reset；单例 `get_instance()`（`paper_pacer.py:74-79`）在**并发 3 的官方部署**下，若平台起多进程 worker，每个进程各持一个 planned=112 的 pacer，会误判"还有 112 题要跑"而**过度收紧**软预算。
- **隐患 2**：`run_with_timeout` 用 `executor.shutdown(wait=False)`（`timeout_control.py:56-59`），超时后**线程无法被杀**，在途 LLM 调用会继续消耗到 socket 超时——极端情况下单题实际墙钟可越过 1200s（被平台强杀则整题 0 分，即 V2.1 的 19 题超时教训；V2.5 用 5h 目标 + reserve 缓解）。

**V2.5 实际影响**：6h 完成率引擎设计正确且在官方评测（61.61%，69/112）中有效；剩余风险是部署形态（多进程 vs 单进程）与在途调用越界，属工程层面的不确定性而非机制性超限。

---

## 7. 采样策略 —— 【部分存在】P3

**结论**：主路径单次采样（prefill，计算题温度 0.8）；无通用温度分层/投票；仅 hard/低置信计算题有 N=3 投票；预算不足时有完整的多级保底出答案链。

**证据**：
- 主路径单采样：`nodes/reasoning_agent_node.py:668-679`（`chat_prefilled(..., temperature=CONFIG["temperatures"]["reasoning"])`，一次成功即返回 L717-719）。
- N=3 投票：`reasoning_agent_node.py:784-822`（温度 0.4/0.8/1.0，≥2 一致采用，否则回退 0.8 那次；`remaining_hard < 60` 停止采样）。
- 保底链：`coordinator_node.py:326-363`（deadline 直接输出 validated）→ `_partial_response`（L74-106，结论捞回/stdout 挖掘/部分结论/噪声门尾部）→ `_emergency_direct_answer`（L109-165，reserve 直答）→ `error_handler.py:129-147`（coordinator 节点兜底）。
- 温度分层仅存在于投票路径：`config.py:45-47` `sample_temperatures=[0.4,0.8,1.0]`、`sample_vote_consensus=2`。

**V2.5 实际影响**：中置信常规题仍单样本；投票通道窄（hard/低置信 + 非客观 + 首轮）。收益有限但成本受控（这正是与 V2.9 N≥8 反噬的刻意区别，`reasoning_agent_node.py:786-792`）。保底链完备，无答案场景极少。

---

## 8. 答案选择仲裁 —— 【存在】P1

**结论**：多候选冲突时由**同源模型**仲裁（prefill 非ce 标记选择），候选间**权重相等**（不按证据强度排序），`_enforce_contract` 只处理字段覆盖不处理数值正确性；V4 的"SC=100 正确但仲裁选了 44"场景在 V2.5 结构上可复现。

**证据**：
- 候选池等权：`nodes/semantic_arbiter_node.py:56-118` `_candidate_pool`——reasoning / python / reasoning_alt 三个候选，除反伪造警告外无证据强度加权（Python 的 `evidence_status=support` 与推理散文同等对待）。
- 选择机制：`semantic_arbiter_node.py:249-352`——nonce 标记 `SELECT:<id>`/`ABSTAIN`；prefill 失败回退完整调用；`_parse_selection`（L121-143）取最后一个合法标记。
- 契约兜底只查覆盖：`semantic_arbiter_node.py:152-189` `_enforce_contract`（`missing_components` 比较，**不验证谁算得对**）。
- 兜底路径：`semantic_arbiter_node.py:192-231` `_fallback_result`——abstain/超时/错误时用 `_preferred_answer`（推理优先，`cross_validator_node.py:90-110`）。
- 锁定：`semantic_arbiter_node.py:381-389` 选定即 `answer_locked=True` → coordinator 逐字输出（见缺陷 2）。
- 缓解：计算题 mismatch/contradict 先 playoff 确定性裁决（`cross_validator_node.py:333-384,442-467`；`playoff_node.py:165-182` decisive 锁定），仲裁是 playoff 之后的选择通道；**但 playoff 的裁决依据是 LLM 生成的代码打印的 `PLAYOFF_RESULT`/`候选 X 核验: PASS` 标记（`playoff_node.py:74-88` `parse_playoff_result`），对 playoff 代码没有反伪造静态检查**（对比 python_agent 的 `parse_verification_evidence` 有 `assess_verification_authenticity`）——所谓"确定性证据"的实际可信度取决于同源模型写的断言打印是否属实。

**V2.5 实际影响**：当两个候选一真一假（真值未在字段覆盖上占优）时，仲裁可能选假；选中即锁，coordinator 不再挑战。61.61% 分下的残余错误中，此通道是主要的结构性风险之一。

---

## 9. 分类器 —— 【部分存在】P2

**结论**：分类错误不直接改答案，但通过**技能文档/验证示例的选择、难度软预算、置信度门控**三路影响后续路径，可间接导致失分或资源错配。

**证据**：
- 分类 → 知识提示：`nodes/classifier_node.py:616-673` 输出 category；`reasoning_agent_node.py:414-417` 按 category 取技能文档摘要；`python_agent_node.py:214-231` 按 category 取验证示例/技能文档——错分类 → 错误的方法论提示，但题面全文仍在，属"提示降质"而非"答案必错"。
- 分类 → 难度软预算：`classifier_node.py:676-693` `_finalize_classification` → `time_budget.apply_difficulty_profile`（`utils/time_budget.py:78-101`；`config.py:123` easy 480 / medium 840 / hard 1200）——**hard 题被误分 easy → soft_total 480s → fast_path 提前触发 → 重试/仲裁/叙事被跳过**，难题可能以半成品出厂。
- 分类 → 置信门控：`utils/confidence_gate.py:17-38`（fast/standard/deep 三档）；`graph/solving_subgraph.py:29-31,58-101`（fast 档省 B 路径/Python；deep 档强制 B 路径）；`confidence_gate.py:41-57` `can_skip_python_verify`（高置信客观题跳过 Python）——置信虚高 → 单样本出厂；置信虚低 → 浪费预算。
- 确定性兜底：`_domain_override`（`classifier_node.py:317-590`）对边界题型有高精度规则；LLM 失败时 `_deterministic_fallback`（L267-294）。

**V2.5 实际影响**：分类对**正确性**影响间接（提示层），对**预算分配**影响直接（难度/置信门控），后者在"难题误判 easy"时可能把难题推进快速路径而失分。属中等风险。

---

## 10. sympy_pre_solver —— 【存在，接线正确但误触发风险高】P1

**结论**：接线正确（在推理节点内调用，命中即跳过 LLM），失败静默降级（返回空交回 LLM）也正确；但**存在"把题面中的子表达式当作完整目标直接出厂"的误触发风险**，以及 `sympy.solve` 丢根/部分求解的风险。此外 V2.5 设计文档中的"三件套"只落地了两件（主线 1 sympy_pre_solver、主线 3 render_sentence_answer），**主线 2（sympy 硬校验 `utils/sympy_equivalence.py`）未实现**。

**证据**：
- 接线：`nodes/reasoning_agent_node.py:350-394` `_sympy_pre_solve`（条件：非客观题 + 非 hard + 非多问项），`reasoning_agent_node.py:399-402` 命中即返回伪 `reasoning_result`（`answer_source="sympy_pre_solve"`），**跳过 LLM**。
- 静默降级：`sympy_pre_solver.py:41-59`（异常返回空）、`reasoning_agent_node.py:370-377`（ImportError/异常 → None → LLM）。
- **误触发风险**（把子表达式当完整目标）：
  - 同余：`sympy_pre_solver.py:212-225` `_congruence` 正则 `(-?\d+)\s*\*?\s*x\s*≡\s*(-?\d+)\s*(?:mod\s*|\b)(\d+)`——**只要题面出现 "3x ≡ 5 mod 7" 形态即触发**。CRT 类题（多同余联立）会取第一条同余返回 `x=... (mod ...)` 作为 whole_answer；若题目真正要求的是别的量（如 "求同时满足…的 x 的最小正整数"），`pre_solve` 返回首个 whole_goal 结果（L54-59）→ 错误答案跳过 LLM 直接出厂。
  - 模幂：`sympy_pre_solver.py:228-236` `_modular_power` 正则 `(-?\d+)\s*\^\s*(\d+)\s*mod\s*(\d+)`——题面含 "a^b mod c" 即触发，即使题目问的是别的（如指数位数字、周期）。
  - 求值：`sympy_pre_solver.py:76-83`（"计算/求值" 锚定行尾，风险较低）。
  - 导数：`sympy_pre_solver.py:118-127`（`f(x)=expr 的导数` 或 `y=expr` + 导数关键词），正则非严格锚定，题面含 "求 y=f(x) 的导数" 之外的内容时可能截取错误表达式。
  - 方程：`sympy_pre_solver.py:130-146`——取**第一个**含 "=" 的数学片段、**只 split 第一个 "="**（L134）；方程组 "x+y=3, x-y=1" 只会解 `x+y=3` 得 `x=3-y` 出厂；`sympy.solve`（L183-190）对三角/指数/含参数方程**丢周期解/分支解**（如 `sin(x)=0` 只给主值）；`_math_parts` 从 `$...$`/纯文本抓取（L255-270），`\begin` 矩阵等跳过。
  - 无目标一致性校验：命中后直接跳过 LLM，**没有任何环节核对"算出的东西是不是题目要的"**。
- 主线 2 缺失：`docs/升级方案_V2.5.md:38-46` 计划 "sympy 硬校验（sympy_equivalence）"；`glob utils/sympy_equivalence.py` 无此文件；HEAD 提交（d396205）stat 中无该文件。
- 缓解：计算题 solving 子图并行扇出 python_agent（`graph/solving_subgraph.py:98-100`），cross_validator 会拿 sympy 答案与 python 结果比对；若 python 独立算出不同值 → mismatch → playoff。但 python 也是同一模型读同一题面，**可能同样只解子表达式而"一致地错"**（同源偏差），此时错误答案带"验证通过"出厂。

**V2.5 实际影响**：对"纯求值/单同余/单方程"类题是净增益（跳过 LLM，省时且准）；对题面含多个数学式、联立约束、周期解、CRT 的 medium 题，存在静默输出错误 whole_answer 的窗口。结合 61.61% 的成绩，此机制应已产生少量错误答案（属于"新引入的风险"而非"修复的风险"）。

---

## 交叉发现（审计过程中的额外结论）

1. **playoff 无反伪造检查**（缺陷 1/8 的放大器）：`playoff_node.py:74-88` 直接信任 LLM 生成代码打印的 `PLAYOFF_RESULT`/`候选 X 核验: PASS`；而 `python_agent_node.py:274-308` 的验证证据有 `assess_verification_authenticity` 静态反伪造。同源模型可以"写一段没真正核验就打印 PASS 的代码"，使 playoff 的"确定性裁决"实际仍是模型断言。**建议 P1**。
2. **PaperPacer.reset() 生产路径缺失**（缺陷 6）：`grep` 显示仅在测试调用；官方并发部署下多进程各自持有 planned=112 的 pacer，预算收紧可能失真。
3. **stale 编译产物**：`nodes/__pycache__/sc_consensus_node.cpython-311.pyc`、`answer_vote_node`、`substitute_node`、`solver_decision_node` 等 pyc 属于早期版本残留（无对应 .py），不影响运行，但易误导审计/评审（建议清理）。
4. **V2.6 特性已混入 V2.5 HEAD**：`config.py:81,83,109`（`enable_thinking_mode_off`、`truncation_continue_max_rounds`）与 `reasoning_agent_node.py:101-105,284-287` 的注释标注 "V2.6"，说明 HEAD 实际是 V2.5 提交但携带部分 V2.6 改动（提交 d396205 含 llm_retry 增强）。版本边界不干净，评审时需注意。
5. **sympy 短路与 B 路径/投票路径交互**：`_reasoning_agent_impl` 是 B 路径与 N=3 投票的共用实现（`reasoning_agent_node.py:800,853`），sympy 命中时三条路径返回同一结果——B 路径的"异构性"被短路抹平（无正确性影响，但浪费一次扇出成本）。

---

## 总体结论

V2.5（61.61%，69/112）是"机制最丰富"的版本：交叉验证、playoff、Critic、仲裁、预算引擎、反伪造均已就位，且大部分缺陷都带有一层**针对性的缓解**（三路投票、证据优先、contradict 先 playoff）。但十项缺陷中：

- **结构性存在且无有效缓解**（P1）：#3 coordinator 不可信边界、#8 仲裁等权选择 + 锁定、#10 sympy 误触发；
- **存在但有部分缓解**（P1）：#1 同源确认偏误（沙箱执行 + 反伪造部分对冲）、#2 弱验证错锁（契约兜底 + playoff 部分对冲）；
- **缺失或部分存在**（P2）：#4 SC 缺失（仅小 N 投票）、#5 冲突时推理可能覆盖正确代码答案、#6 预算控制基本健全但多进程部署与在途调用越界是隐患、#9 分类器经预算门控间接失分；
- **轻微**（P3）：#7 采样策略（保底链完备）。

**优先级建议**：若要在 V2.5 基础上修复，先做 #10 的 sympy 目标一致性护栏（要求 whole_answer 与题面所求量可对齐，否则不短路）与 #8/#3 的"锁定答案不得绕过证据闸门"与"coordinator 输出必须与 validated 一致"（结构化约束），再考虑 #1 的独立验证（异模型/确定性重算）与 #4 的通用 SC。
