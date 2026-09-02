CONFIG = {
    "model": "intern-s2-preview-397b",
    "max_retries_per_node": 2, "llm_max_retries": 3, "backoff_factor": 2.0,
    # 各节点的超时上限是"最后一道防卡死闸"：TimeBudget.timeout_for() 会把它
    # 再按题目剩余时限收窄，真正起约束作用的是题目时限而不是这里的上限。所以
    # 这些值都定得接近题目预算——上限过紧会在健康工作完成前把节点切断，返回
    # 空答案比慢一步返回可用答案更糟，只有时限本身才能叫停有效工作。
    # `classifier` 必须大于 classify_domain._PLAIN_CALL_SECONDS(150)：非 prefill
    # 回退一次调用实测 130s 上下，120s 的上限会切断已付费的调用，连带丢弃可用的
    # prefill 结果。
    "node_timeouts": {"input": 5, "classifier": 200, "solving": 1150,
                      "reasoning_agent": 1100, "python_agent": 1100,
                      "python_mcp_execute": 60, "cross_validator": 15,
                      "reconciliation": 10, "arbiter": 150, "coordinator": 420},
    "classifier_confidence_threshold": 0.85,
    # 零成本的确定性排序只作为 LLM 分类失败/不可解析时的兜底，不再缩窄 LLM
    # 的候选清单（全部 18 个领域在单次 prefill 调用里给全）。
    "classifier_top_k": 3,
    "computation_tolerance": 1e-6, "proof_confidence_threshold": 0.7,
    "temperatures": {"classifier": 0.1, "reasoning": 0.8, "objective_reasoning": 0.2,
                     "python": 0.6,
                     "reconciliation": 0.2, "arbiter": 0.1, "coordinator": 0.4},
    # prefill 选择类调用（classifier/arbiter）因助手种子抑制推理，只回 4~12 个
    # completion token，小上限只在 prefill 下安全：缺 prefill 时推理模型的私有
    # 推理会占满 max_tokens，小上限在答案出现前就截断。*_fallback 预算是给
    # 非 prefill 重试用的，必须容纳完整私有推理。
    #
    # 单次额度 32768 在奥赛题上会被私有 reasoning_content 整体耗尽（~600s），
    # 四个章节全空，且耗尽后重试往往已无时间。改为分级熔断：首轮后耗尽改发
    # prefill 压缩重试（助手种子抑制私有推理，全部 token 用于可见章节）。
    #
    # 2026-08-22 官方评测复盘（112 题）：684 次请求中 truncated_count=286
    # （约 42% 的响应被 max_tokens 截断），说明首轮额度偏紧、压缩重试额度
    # 也偏小（复杂多问/枚举答案在 8192 内写不完）。据此上调：
    #   * 首轮 reasoning/python 24576 -> 30720（缓解首轮截断，仍保留重试窗口）
    #   * 压缩重试 reasoning/python 8192 -> 16384（复杂答案有完整落点空间）
    #   * coordinator 16384 -> 24576（长证明/多问项拼装不再截断）
    # 首轮 reasoning 从 30720 → 16384 → 8192 的分级下调：实测 30720 让私有
    # reasoning_content 膨胀到 ~560s 逼近节点 1100s 上限（30/99 题超时）；16384
    # 在 20 路并发（~15 tok/s）下仍需 ~1092s，仍被 550s 单次墙钟上限切断、拿不到
    # 任何已生成内容，等于白跑。8192（~546s）让首轮在 max_tokens 处正常截断
    # （finish_reason=="length" 可读），配合断点续写（复用已生成的结论速览/步骤）
    # 补全剩余章节——这是 gbq "小额度 + 断点续写" 三件套的核心。复杂多问/枚举
    # 答案由续写兜底，不再靠单次大额度硬撑。
    "max_tokens": {"classifier": 96, "classifier_fallback": 8192,
                   "objective_reasoning": 4096,
                   "reasoning": 8192, "python": 16384,
                   "reasoning_compressed": 16384, "python_compressed": 16384,
                   "reconciliation": 32768,
                   "arbiter": 96, "arbiter_fallback": 4096,
                   "coordinator": 24576, "emergency_answer": 1280},
    "reconciliation_max_rounds": 2,
    "token_budget_max": 256000, "token_budget_warn_ratio": 0.9,
    # 每题的墙钟预算。平台硬限 20 分钟；safeguard 保证在硬杀前仍能交出真实
    # 答案，fast_path_threshold 是可选阶段被跳前再走一次完整 LLM 往返所需的空间。
    #
    # safeguard 必须大于一次进行中的调用：软时限过线时已发出的调用无法取消。
    # 四路并行下最慢实测：推理 225s、Python 236s、拼装 98s。300s 吸收最慢
    # 调用后仍给拼装留出产出答案的余量。
    "problem_time_budget_s": 1200,
    "time_safeguard_s": 300,
    "time_fast_path_threshold_s": 300,
    # safeguard 给语义仲裁与应急直答划出固定配额，杜绝硬时限压顶时仲裁被跳过
    # （旧实现按软预算判断，越难的题越先耗尽预算、恰恰最需要复核却全部跳过）。
    # 硬上限前剩余 ≥ 该值时 prefill 仲裁(~10s)与应急直答(~30s)仍放行；再低
    # 只做确定性兜底。
    "arbiter_safeguard_s": 75,
    "emergency_safeguard_s": 90,
    # 压缩重试(prefill)与完整重试分开定价。压缩重试按 safeguard 模式放行——
    # 软预算已尽也可执行，只要求硬上限前剩余 ≥ 压缩估时 + 本余量（余量覆盖
    # 仲裁 prefill、应急直答与确定性拼装；node_wrapper 的硬超时兜底是最坏情况）。
    # 压缩重试额度上调到 16384 后估时相应上升，故余量从 150 提到 250。
    "compressed_safeguard_s": 250,
    # 首轮推理/Python 调用的单次墙钟上限。难题上首轮会把整个节点 1100s 上限
    # 吃光、被 node_wrapper 掐断，压缩重试（真正的挽救手段）永远没机会触发
    # （曾实测 idx 0/7/11/12/13 均报 "operation timed out after 1100s"、
    # attempts=0、无压缩重试记录，最终落 emergency_direct_answer 错答）。把它
    # 压到 550s，保证首轮 + 压缩重试(350s 估时) + 余量(250s) 仍能放进 1200s
    # 硬时限（1200-550=650 ≥ 350+250=600），让压缩重试真正可达。
    "first_attempt_timeout_s": 550,
    "log_level": "INFO", "log_dir": "logs",
}

# ============ 版本标记（评测日志/trace 溯源用） ============
# 每轮代码改动后更新此值；solve() 的 trace 会携带它，配合评测日志的
# Commit ID 即可确定某次出分对应哪一版代码。
AGENT_VERSION = "v71.43base+fix5(小数/sympify/rstrip/choice/multipart)+drift-guard+diag-probe 2026-09-02"
