# easy/medium 全证据出厂门（Evidence Exit Gate, V6.0）机制设计

> 目标：**以后新抽题的 easy（1.0–3.5）与 medium（4.0–5.5）题求解 100% 正确**——
> 更准确地说：**"系统会做的题"不许错**。出厂答案必须携带充分、**方法论独立**的证据；
> 证据不足时按成本阶梯升级，直到拿到确定性证据或预算耗尽（此时才允许 best-effort 出厂并留证）。
>
> 本设计基于 round4–6 全部 119 道 easy/medium 错题的出厂时证据回放（`local_test/analyze_easy_medium.py` +
> 逐题 trace 深挖），所有触发条件、证据矩阵、预算数字均有实证支撑。

---

## 1. 实证基础：119 道错题的出厂证据缺口

### 1.1 归因分布（round4–6 汇总，119 道）

| 类别 | easy | medium | 合计 | 出厂时发生了什么 |
|---|---|---|---|---|
| arbitration（仲裁选错） | 5 | 47 | **52** | validation=uncertain → LLM 仲裁从候选中选一个出厂，**无任何确定性证据** |
| conflict（冲突选错） | 1 | 40 | **41** | mismatch/contradict → playoff 复算（部分 decisive）→ 仲裁；**playoff 裁决或仲裁仍错** |
| same_source_match（同源同错） | 3 | 5 | **8** | reasoning==python 数值一致 → match 直接出厂，**无独立通道**（r3 回放：双闭式等价 6/6 同错） |
| reconciliation_exhausted（重推耗尽） | 2 | 8 | **10** | 轮次耗尽 → 仲裁选错（归因脚本计入 other） |
| give_up（假性/真性放弃） | 3 | 3 | **6** | 出厂"无法确定/缺少条件" |
| emergency（应急裸答） | 0 | 2 | 2 | 双分支全灭后的裸答 |

**核心结论：119 道错题中 62 道（arbitration 52 + exhausted 10）出厂时证据分数≈0——答案完全由 LLM 仲裁从无验证的候选中挑出。** 这是最大的单一漏洞。

### 1.2 逐题证据回放的关键发现

**(a) SC 不是出厂保险，且存在"SC 被覆盖"bug（2 例实证）：**
- r5 #83：SC 推理采样 2/3 一致锁定 **100（=官方）**，但下游仲裁仍从旧候选池选 **44** 覆盖之 → 出厂 44。
- r6 #96：SC 锁定 **20/31（=官方）**，出厂却为 50/57。
- 根因：`sc_consensus_node` 锁定时**不改 next_node**，`route_after_solving` 仍按 cross_validator 留下的 `next_node=semantic_arbiter` 路由；仲裁候选池（reasoning/python/alt）**不含 SC 答案**，直接覆盖。

**(b) SC 单通道共识对 medium 经常同错（实证 ~30 例）：**
- r6 #18：SC 锁定 10（官方 4）；r5 #9：SC 锁定 509（官方 516）；r5 #88：SC 锁定 4037（官方 2^2018−1）。
- ⇒ "SC ≥2 路一致"对 medium **不是充分证据**，只算一个弱通道；easy 上命中率高，可放宽。

**(c) 同源 match 直接出厂是 8 道错题的路径：**
- r6 #0（Wason 卡片，官方 3）：reasoning=2、python support=2 → match confidence 1.0 出厂；两者同读错题。
- 单代码（B）、双闭式等价（A+B）均不可信（round3 回放 9/12 错、6/6 同错）；**唯一可靠确定性证据是"不同方法的多份独立代码"或"独立验证代码双 PASS"**（AIMO2 路线）。

**(d) easy 错题去重后仅 ~6 个不同问题，其中 ~3 个是"题面丢失图形"的不可解题：**
- 数轴题（r4 #24 / r5 #14 / r6 #13，官方 55）：题面只有 "Numbers m and n are on the number line"，**无图无数值**——模型答"无法确定"是字面正确的，官方 55 来自丢失的图形。
- 饼图题（r4 #110 / r5 #45 / r6 #42，官方 100）：题面 "circle graph shows the results"，**百分比在图中**，文本不可解。
- Wason 卡片（r4 #0 / r6 #0 + r5 #0 变体，官方 3）、足球文本题（r4 #100 / r5 #44）、转盘概率题（r4 #55 / r5 #31 / r6 #29，官方 2/3 与文本推导 5/9 不一致，疑为官方 key 与图有关）——**文本可解**，是出厂门的真实靶点。
- ⇒ 出厂门必须**识别"图依赖/图丢失"题并跳过重型升级**，把预算花在真正可救的题上。

**(e) 预算现实：** easy 错题 12–28 calls、63–468 api_sec；medium 错题 12–42 calls、157–3780 api_sec。easy 主链路 ~60–250s，软预算 480s 内余量充足；medium 部分题已贴近 1200s 硬限——**升级必须按 remaining_hard 闸门化**。

---

## 2. 证据通道定义与独立度评分

一个"通道"= 一个**方法论独立**的证据来源。同模型同路径的重复不算新通道。

| 通道 | 来源（state 字段） | 独立度/分 | 成本 | 已知失效模式 |
|---|---|---|---|---|
| **A** 推理闭式 | `reasoning_result.answer` 闭式（`is_closed_form_answer`） | 0.5 | 已发生 | 单样本 LLM 算错 |
| **B** Python 闭式 support | `python_output.evidence_status=="support"` 且闭式、非伪造、非 skipped | 1.0 | 已发生 | 代码算错（r3 回放 9/12） |
| **C** SC 采样共识 | `sc_consensus.r_consensus` 或 `c_consensus`（2/3）；**双通道一致 r∧c 记 2.0** | 1.0 / 2.0 | 2–6 calls | 同源错（r6 #18 SC=10 官方=4） |
| **D** 程序化验证器双 PASS | `verify_candidate(...).strong==True`（2 份独立验证代码都 PASS + 数值对照证据） | **2.0** | 2 calls + 2 exec | 验证代码同错（罕见） |
| **E** 双独立代码等价 | `second_python_done` 且锁定（`python_double_verification_locked` / `rule_judgeable_double_python_locked` / `python_support_double_locked`） | **2.0** | 1 call + 1 exec | 两代码同错（罕见） |
| **F** playoff decisive | `playoff_status=="decisive"` 且锁答案 | 1.0 | 已发生 | 复算代码错（r6 #18 NEITHER 后仲裁错） |
| **G** 客观题复核/判断确认 | `objective_revote_trace.agree==True` 或 judge_confirm=="confirm" | 1.0 | 1 call | 双票同错 |
| **H** 异构第二推理路 match | `reasoning_result_alt` 与 Python match（`adopted_source=="reasoning_alt"`） | 1.0 | 已发生 | 异构路同错 |

**同源扣减（关键纪律）**：A+B 数值等价（match）时 B 降为 0.5——推理与 Python 都是同一 397B 生成，会"一致地错"（round3 回放双闭式等价 6/6 同错）。**match 基础分 = 1.0，永远不够出厂。**

---

## 3. 出厂证据矩阵（触发条件：难度∈{easy,medium}、非 proof、有候选、remaining_hard>闸门阈值）

### 3.1 出厂门槛

- **easy**：证据总分 ≥ 2.0，且不允许"A+B 同源 match"单独达标。
- **medium**：证据总分 ≥ 2.0，**且必须含 ≥1 个强确定性通道（D 或 E）**；无 D/E 时需 ≥3.0 且含 B+C双通道。

### 3.2 GREEN 判定表（出厂放行）

| # | 证据组合 | 总分 | easy | medium |
|---|---|---|---|---|
| 1 | **E** 双独立代码等价 | 2.0 | ✅ | ✅ |
| 2 | **D** 验证器双 PASS | 2.0 | ✅ | ✅ |
| 3 | B + C双通道一致 | 1+2=3.0 | ✅ | ✅ |
| 4 | B + F（python support + playoff decisive） | 2.0 | ✅ | ❌（需再 +D 或 C） |
| 5 | C双通道 + F | 2+1=3.0 | ✅ | ✅ |
| 6 | A + B + C单通道 | 0.5+0.5+1=2.0 | ✅ | ❌ |
| 7 | A + B（同源 match） | 1.0 | ❌ | ❌ |
| 8 | B 单独 | 1.0 | ❌ | ❌ |
| 9 | C 单通道 | 1.0 | ❌ | ❌ |
| 10 | 无任何通道（仲裁输出） | 0 | ❌ | ❌ |
| 11 | **G**（客观题：复核投票一致/判断确认） | 1.0 | ✅（保持现状短路径） | ✅ |

> 说明：#4 中 F 的 playoff 复算代码是单份生成代码，与 B 同信任级；easy 允许 B+F，medium 强制再加一道。
> 任何 GREEN 都要求答案 C 与证据指向一致（例如 SC 锁定值 ≠ 出厂候选时不构成 C 通道）。

---

## 4. 证据不足时的升级路径（按成本递增，每级一次性标志防重入）

升级只在 `remaining_hard > min_hard` 且未进 fast_path 时执行；任一动作后**重算证据分数**，达标即停。

```
gate 判定非 GREEN
 ├─ L3 强制第二独立代码 (E)：候选闭式 && python support(B) && 未 second_python_done
 │      → _second_python_check(require_playoff=False)；双代码等价 → 锁 E → GREEN
 ├─ L1 强制 SC (C)：未 sc_consensus_done &&（计算特征 或 强制标志）
 │      → sc_consensus_node(force=True)；adopted 且与候选一致 → +C 通道重算
 ├─ L2 强制验证器双 PASS (D)：候选闭式 && 未 verify_done
 │      → verify_candidate()；strong==True → 锁 D → GREEN（weak_pass 不锁，仅 +0.5 参考）
 ├─ L4 强制重推 (reconciliation)：仍未 GREEN
 │      → next_node=reconciliation + 定向 hint（逐字重读、质疑结论、输出精确答案形式）
 └─ L5 预算闸 / 重推后仍非 GREEN
        → best-effort 出厂 + trace 标记 evidence_gate="deficient"（供错题归因与下一轮校准）
```

**顺序理由（成本×收益）**：E 最便宜（1 call）且最强（双代码）；SC 对 easy 命中率极高但 medium 弱，放第二位；验证器 D 是 medium 的可靠保险（2 calls）；重推最贵（完整 solving 重跑），最后用。

**SC 锁定终态修复（合并进本设计）**：`sc_consensus_node` adopted 时置 `next_node="coordinator"`，且闸门在 SC 之后运行——仲裁**永远不再有机会覆盖 SC 锁定值**（实证 r5 #83 / r6 #96 的 bug 由此根除）。

**循环终止**：`evidence_gate_round` 上限 2；`sc_consensus_done` / `second_python_done` / `verify_done` 一次性标志；全局 `max_llm_calls_per_problem`（V5.3 起按难度 8/14/20，未知难度 fallback 20）与 `evidence_gate_max_calls`（6）双封顶。

---

## 5. 文本答案题（无闭式）的保障

适用：候选非闭式（`is_closed_form_answer=False`）&& 非 proof && 非 objective（choice/judge/fill 已有 revote/judge-confirm 通道 G）。

**规则：文本答案出厂需要 ≥2 个独立文本通道。**

1. **强制 SC 文本投票**（未跑过时）：SC 的 `same_answer` 已含 `_text_equiv` 通道（去空白/标点后一致）；r_answers 2/3 文本一致 → 锁定，记 +C(文本) 1.0。
2. **独立复核投票**（SC 未锁或需第二通道）：复用 `_objective_revote` 逻辑但用通用 prompt（"逐项核对题面条件后作答，输出 结论：<答案>"，~45s，1 call）——与 SC 采样相互独立。
   - 一致 → +G 1.0 → GREEN；
   - 分歧 → **不采纳任何一方**，走 L4 重推（hint：答案必须给出精确形态，如 "0 wins, 1 loss, 2 ties" 这种 X wins/Y losses/Z ties 形式）。
3. **禁止无通道文本出厂**：文本候选若既无 SC 共识也无复核一致 → 重推，绝不让 LLM 仲裁直接选（r4 #100 实证：官方是 "0 wins, 1 loss, 2 ties" 三元组，模型输出散文讨论）。
4. 判断题/选择/填空：维持现状（双向确认 / 复核投票），闸门只做"已过门"确认。

---

## 6. 图依赖 / 图丢失题检测（新增 utils/image_dependency.py）

**特征**（题面命中任一且答案不可由文本唯一确定）：
`number line` / `circle graph` / `pie chart` / `spinner` / `diagram` / `figure` / `image` / `1024 pixels` / `如图` / `图表` / `图像` / `数轴` / `扇形图` 等，且上下文无对应数值。

**处理**：
- 标记 `image_dependent=True`，**跳过 L1/L3 重型升级**（实证：数轴题、饼图题 SC 采样基本全部"无法确定"，烧预算无收益——r6 #13 SC 推理 3 采样全放弃、r5 #14 推理 2 采样放弃 + 代码 3 采样中 2 个放弃 1 个给出无依据的 13/4）。
- 出厂前若候选是 give-up 文本 → 至多 1 次"图中信息推断"SC（cap 2 calls，提示模型从题面线索推断图中数据），仍失败则 best-effort 出厂并标记。
- 错题归因单列"image_dependent"，不纳入出厂门 KPI（这些题对任何纯文本求解器不可解）。

**give_up 检测**：任何 easy/medium 候选命中 `looks_like_give_up`（"无法确定/缺少条件/未能求解"）→ 非图题强制走 V5.2 已有 give_up_forced_sc；图题走上图分支。

---

## 7. 预算成本估算与官方 6h 兼容性

### 7.1 每动作成本（官方慢 API 约 60s/LLM call，代码执行 60s，并发 3 → 每 call 摊墙钟 20s）

| 动作 | 每题额外 calls | 每题额外墙钟 |
|---|---|---|
| L1 SC（早停 2 一致） | 2–6 | 40–120s |
| L2 验证器双 PASS | 2 + 2 exec | ~120s |
| L3 第二独立代码 | 1 + 1 exec | ~60s |
| L4 重推（reasoning+python） | 2 | ~60–180s |

### 7.2 按轮次估算（112 题，~60 道 easy/medium，模式 B：只升级薄弱题 + easy 全量）

| 触发面 | 数量/轮 | 每题额外 calls | 小计 calls | 小计墙钟 |
|---|---|---|---|---|
| easy 全量过闸（match→L1 两采样；薄弱→L2/L3） | 13 | +2~4 | ~35 | ~12min |
| medium match+B → L3（最便宜升级） | ~22 | +1 | ~22 | ~7min |
| medium 薄弱题（arbitration/conflict/give_up）→ L1/L2/L4 | ~25 | +2~5 | ~80 | ~27min |
| 文本题/客观题 SC 或复核 | ~10 | +1~3 | ~20 | ~7min |
| **合计** | | | **~157** | **~53min/轮** |

相对 round6 实测 2627 calls/轮：**+6%** 调用量。PaperPacer 目标 5h/轮 → 5h+53min ≈ **5h53m < 6h** ✅（余 7min 缓冲）。

### 7.3 兼容性纪律

1. **正确题不加价**：GREEN 题（含已双代码/已验证器/双通道 SC 的题）零升级、零额外调用。
2. **预算闸**：`remaining_hard < {easy:400, medium:500}` 或 `fast_path()` → 跳过升级直接出厂（deadline guard 优先，绝不烧穿墙钟）。
3. **全局封顶**：`evidence_gate_max_calls=6`/题，叠加 `max_llm_calls_per_problem`（V5.3 起按难度 8/14/20）；SC 早停与 `sc_end`（V5.1）保持不变。
4. **降级开关**：`evidence_gate_mode = "full" | "easy_only" | "off"`——官方实测若贴 6h，一键只护 easy 或全关（回到 V5.2 行为）。
5. 验证器/SC/双代码全部走既有 API + MCP 沙箱路径，**官方合规**（与 python_agent/playoff 同机制）。

---

## 8. 具体实现建议（文件 / 函数 / 拓扑 / 配置）

### 8.1 新增 `nodes/evidence_gate_node.py`（出厂门主体）

```
evidence_gate_node(state, config) -> dict
├─ 前置过滤：难度非 easy/medium、proof、无候选、fast_path → 直接放行（零行为变化）
├─ _evidence_score(state) -> {"score", "channels": {...}, "reasons": []}
│     从 state 读取 A/B/C/D/E/F/G/H 通道（D/E 需从 second_python_done、verify_evidence 读取）
├─ _is_green(state, score) -> bool        # 按 §3 矩阵
├─ _escalate(state, config) -> dict       # §4 阶梯，直接调用：
│     sc_consensus_node(state, config)    # L1（force 标志绕过非计算条件，保留预算闸）
│     verify_candidate(client, deps, problem, cand, config)  # L2
│     _second_python_check(state, config, require_playoff=False)  # L3（import 自 cross_validator）
│     reconciliation 路由 + 定向 hint     # L4
├─ _image_dependent / _looks_give_up      # §5/§6
└─ 返回 {"next_node": "critic"|"reconciliation",
         "evidence_gate": {"decision": green|escalated|deficient|skipped,
                           "score": ..., "channels": ..., "trace": [...]}}
```

### 8.2 图拓扑（`langgraph_math_agent.py`）

```
solving ──(默认/SC锁定)──► evidence_gate ──(GREEN)──► critic ──► coordinator
                              │
                              └──(非 GREEN)──► reconciliation ──► solving（重推后再过闸）
semantic_arbiter ──► evidence_gate          # 仲裁输出必须过闸（62 道错题的漏洞点）
playoff(decisive) ──► evidence_gate         # medium 的 decisive 答案加 D 通道保险
route_after_solving：next_node∈{playoff,reconciliation,semantic_arbiter} 保持直通（升级路径）；
                   其余（match/SC锁定/默认）一律先进 evidence_gate
```

### 8.3 修改点

| 文件 | 改动 |
|---|---|
| `nodes/evidence_gate_node.py` | **新增**（见 8.1） |
| `langgraph_math_agent.py` | add_node("evidence_gate")；solving 默认路径、semantic_arbiter、playoff(decisive) 都先过闸；route_after_evidence_gate → critic/reconciliation |
| `nodes/sc_consensus_node.py` | ① `_wants_sc` 增加 `force`（或读 `state["evidence_gate_force_sc"]`）绕过计算特征条件（保留 MockClient/预算闸）；② **adopted 时 `out["next_node"]="coordinator"`**（修 SC 被仲裁覆盖 bug）；③ V4.9 验证器结果回写 `out["sc_consensus"]["verify"]`（供闸门读 D 通道） |
| `utils/answer_verifier.py` | 接口不变；闸门调用后把结果写 `state["verify_evidence"]={candidate, verified, strong, verdicts}` |
| `utils/image_dependency.py` | **新增** `detect_figure_lost(problem) -> bool`（§6 特征词） |
| `nodes/cross_validator_node.py` | match 结果补 `match_result["same_source_only"]=True`（A+B 无独立通道，供闸门直接读取）；`verify_gate` 摘要补 `evidence_gate` 字段 |
| `config.py` | `enable_evidence_gate=True`；`evidence_gate_min_hard_s={"easy":400,"medium":500}`；`evidence_gate_max_calls=6`；`evidence_gate_mode="full"` |
| `local_test/analyze_easy_medium.py` | 错题清单增加 evidence_gate 列（decision/score/channels），回归验证用 |

### 8.4 回归与验收

1. 单测：`tests/` 加 `test_evidence_gate`（GREEN 组合表驱动：构造 11 种证据组合 state → 断言放行/升级）。
2. round7 起启用 `full`；对比 round6 同题（去重后）正确率；错题回放出厂时证据分数校准阈值。
3. 验收口径：easy/medium 错题出厂时 `evidence_gate.decision` 不得为 `green`（GREEN 放过即回归 bug）；`deficient`（预算不足）允许但需单列统计。
4. 官方合规冒烟：verify/SC/第二代码均走既有 API+沙箱；确认无新增外部依赖。

---

## 9. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 全量过闸把 easy 单题时间 +50–150s | easy 仅 8–13 题/轮（round4–6 实测 13/10/8）；`evidence_gate_mode="easy_only"` 一键降级 |
| 验证器双 PASS 假锁（验证代码同错） | 维持 V5.0-fix3 纪律：仅双 PASS + 数值对照证据才锁；D 不与同源 B 叠加为 4.0 |
| 闸门-重推循环 | `evidence_gate_round≤2` + 一次性标志 + 全局调用 cap |
| 文本题 SC 2/3 同错 | 文本题强制双通道（SC + 独立复核），分歧走重推不采纳 |
| 官方 API 更慢贴 6h | PaperPacer 5h 目标 + 闸门预算闸 + `mode="off"` 完全回滚到 V5.2 |
| 图丢失题无法救 | `image_dependent` 检测跳过重型升级，不烧预算，KPI 单列 |

---

## 10. 一句话总结

**出厂门 = "同源 match 不许直接出厂 + 仲裁输出必须过证据闸 + 文本答案要双通道 + 图题不烧预算 + 预算闸封顶"**：
- 62 道仲裁/耗尽错题 → 闸门拦下，先补 SC/验证器/双代码再出厂；
- 8 道同源 match 错题 → match 降为 1.0 分，强制补一个独立通道（L3/L1）；
- 41 道 conflict 错题 → playoff decisive 对 medium 再补 D 通道；
- 2 例 SC 被覆盖 → SC 锁定即终态；
- 每轮 +~6% 调用（~53min 墙钟），6h 内可承受，且带 `off` 总开关。
