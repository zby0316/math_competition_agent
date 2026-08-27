CONFIG = {
    "model": "intern-s2-preview-397b",
    # V2.4 单题时间硬约束：官方慢 API（35s/请求）下，每次重试都是一次超时风险。
    # 3 次重试最坏 3×120s=360s 打水漂，是 V2.1 官方评测 19 题 error（单题超时被
    # 强杀）的重要推手。降到 1 次：快失败快降级，用"现有最佳答案成稿"而非死磕。
    "max_retries_per_node": 1, "llm_max_retries": 1, "backoff_factor": 1.5,
    # Ceilings are a hang detector of LAST resort; TimeBudget.timeout_for clamps
    # each to whatever the problem deadline still allows, and that clamp is the real
    # bound. They are near the full budget on purpose.
    #
    # Learned the hard way (2026-07-29 run): tighter ceilings of 720s/300s KILLED
    # healthy work — Q1's reasoning was cut at 720s and returned nothing, Q2's Python
    # at exactly 300.0s likewise. A severed node yields an empty answer, while a slow
    # node still yields a usable one, so a ceiling that fires before the deadline is
    # strictly worse than no ceiling at all. Only the deadline may stop real work.
    # `classifier` must exceed nodes.classifier_node._PLAIN_CALL_SECONDS (150): the
    # unprefilled fallback measured 129.5s and 133.3s on a hard problem, so a 120s
    # ceiling killed a call that had already been paid for and discarded the usable
    # prefill result with it (game-theory olympiad problem, 2026-07-29).
    "node_timeouts": {"input": 5, "classifier": 200, "solving": 1150,
                      "reasoning_agent": 1100, "python_agent": 1100,
                      "python_mcp_execute": 60, "cross_validator": 15,
                      "reconciliation": 10, "semantic_arbiter": 150, "coordinator": 420,
                      "critic": 120, "playoff": 300},
    "classifier_confidence_threshold": 0.85,
    # V2 M2 置信门控：最少资源高准确率
    #   high(≥0.90) → fast 单路径快速答（省 Python/双路径）
    #   low(<0.70)  → deep 深思考通道（M3 DeepSolver）
    #   中间        → standard 双路验证（现状）
    "confidence_gate": {"high": 0.90, "low": 0.70},
    # V2 M3 DeepSolver 深思考通道触发的领域（科研级超难题高发）
    "deep_solver_domains": ["数论", "组合数学", "高等代数", "抽象代数"],
    # Zero-cost deterministic ranking is retained purely as a fallback for a
    # failed/unparseable LLM classification; it no longer narrows the LLM's
    # candidate list (all 18 domains go in one prefilled call).
    "classifier_top_k": 3,
    "temperatures": {"classifier": 0.1, "reasoning": 0.8, "objective_reasoning": 0.2,
                     "python": 0.6,
                     "reconciliation": 0.2, "semantic_arbiter": 0.1, "coordinator": 0.4},
    # V2.5 小 N 自洽投票（克制版）：只在 hard 难度或分类低置信的计算题首轮触发，
    # 用 prefill 短答案做 N=3 次多温度采样 + 多数投票。与 V2.9 大 N 采样的关键
    # 区别：N=3（非 6~8）、prefill 抑制 CoT（非 thinking）、token 沿用难度分级
    # （非 32768 统一）——三者叠加正是 V2.9 truncated 91→302 反噬的根因，这里
    # 全部规避。仅当多数票（≥2/3）一致时采用，避免"模型系统性错时投票放大错误"。
    "sample_temperatures": [0.4, 0.8, 1.0],
    "sample_vote_min_confidence": 0.85,
    "sample_vote_consensus": 2,
    # Prefilled selection calls (classifier/semantic_arbiter) emit 4-12 completion
    # tokens because the assistant seed suppresses reasoning entirely. The small
    # caps below are safe ONLY with prefill: without it, this reasoning model bills
    # CoT against max_tokens and a small cap truncates before any answer appears.
    # The *_fallback budgets fund the non-prefill retry, which must fit full CoT.
    #
    # 2026-08-09 评委报告模式 A（约 35 题）：32768 的单次额度在奥赛题上被私有
    # reasoning_content 整体耗尽（~600s），四章节全空，且耗尽后重试"常已无时间"。
    # 现改为分级熔断：首轮 24576（~450s 封顶），耗尽后改发 prefill 压缩重试
    # （*_compressed，助手种子抑制私有推理，全部 token 用于可见章节，~150s）。
    # 首轮 24576-32768 区间的成功解极少（中档题实测 5-7k token，奥赛题贴 32768
    # 也多为耗尽），降低上限牺牲的成功区间可忽略，换来的重试窗口是净收益。
    #
    # 2026-08-12 官方评测防御性下调（eval_log_c0a00cb…）：官方评测 11h19m 远超
    # 6h 上限、最终 accuracy 0.1429 + status degraded + runner failed——官方 API
    # 单次推理响应慢，24576 全量思维链在官方 client 下大概率触发请求超时/截断，
    # 大量题无法产出有效答案。首轮降至 8192（中档题实测 5-7k token 覆盖大部分），
    # 配合 *_compressed 8192 双通道，单次推理时长显著缩短，官方环境下更可能在
    # 超时前完成。牺牲的仅是极少数"贴 24576 上限"的奥赛题成功区间。
    "max_tokens": {"classifier": 96, "classifier_fallback": 8192,
                   "objective_reasoning": 4096,
                   "reasoning": 12288, "python": 8192,
                   "reasoning_compressed": 12288, "python_compressed": 8192,
                   "reconciliation": 32768,
                   "semantic_arbiter": 96, "semantic_arbiter_fallback": 4096,
                   "coordinator": 16384, "emergency_answer": 1280},
    # finish_reason=length 截断时的续写补全 max_tokens（只续一次，prefill 抑制 CoT）。
    # V2.5 修复（官方 truncated_count=203）：2048→4096——难题截断缺口常超 2048，
    # 单轮补不完；4096 在 prefill 抑制 CoT 下耗时 ~90s，预算可承受。
    "truncation_continue_tokens": 4096,
    # V2.6 截断根治：thinking_mode=False 确定性关"深度思考"（长 CoT）。截断根因
    # 是 CoT 计入 max_tokens、难题上吃光预算导致答案没写完。V2.4 用 prefill 变相
    # 关 CoT 拿到 59.82 最佳分（对比 V1 开 CoT 53.53），证明数学场景关 CoT 反而
    # 更好。官方原生 thinking_mode=False 比 prefill hack 更可靠。默认开启；
    # 若实测降质，改回 False 退回纯 prefill。
    "enable_thinking_mode_off": True,
    # 截断续写最大轮次（V2.6 从"只续一次"升级为"续到完整，最多 N 次"）。
    # V2.5 修复（官方 truncated_count=203）：2→4——难题（奥赛级）截断缺口
    # 常需 3-4 轮 4096 续写才能完整；每轮前有预算闸门，不会无界烧时。
    "truncation_continue_max_rounds": 4,
    # V2.4 token 分级恢复：V2.1 把 reasoning 一刀切到 8192 导致难题推理被截断
    # （正确率 60→45）。恢复为按难度分级：简单题省 token（快），难题给足（推理
    # 充分）。hard 恢复到 V1 峰值 24576（V1 正是用 24576 拿到 60 correct），
    # 配合"提分内化到提示词 + 调用精简"不加剧超时。
    # V2.4 真机验证（10 题 L4）：hard 24576 使单次 reasoning 143-231s，且
    # reconciliation 重算 3 次 → 单题 7-8min → 全卷墙钟逼近 6h。回落：
    # hard 16384（单次 reasoning 实测 ~34s）、medium 12288。16384 对难题已够
    # （V1 每题均 3.8K token，峰值 24576 是极少数题）。
    "reasoning_tokens_by_difficulty": {"easy": 8192, "medium": 12288, "hard": 16384},
    "reconciliation_max_rounds": 2,
    "token_budget_max": 256000, "token_budget_warn_ratio": 0.9,
    # Wall-clock budget per problem. The platform's hard limit is 20 min; the
    # reserve is what guarantees we still emit a real answer instead of being
    # killed mid-call, and fast_path_threshold is the room needed for one more
    # full LLM round trip before optional stages are skipped.
    #
    # The reserve must exceed one full in-flight call: a call already issued when the
    # soft deadline passes cannot be cancelled. The forced-budget stress run
    # (2026-07-29) overshot the soft deadline by 27-42s for exactly that reason.
    # Worst cases measured under four-way parallel load: reasoning 225s, Python 236s,
    # coordinator 98s. 300s absorbs the slowest observed call and still leaves the
    # coordinator room to emit an answer.
    "problem_time_budget_s": 1200,
    "time_reserve_s": 300,
    "time_fast_path_threshold_s": 300,
    # V2 M1 全卷完成率引擎（PaperPacer）：平台 6h 全卷硬限（秒）。
    # 官方评测实证：V1 每题固定 1200s 软预算导致 112 题只完成 16 题（14.29%）。
    # PaperPacer 按"剩余全卷时间 ÷ 剩余题数"动态收紧每题软预算，保证
    # 6h 内 112 题全部产出答案（完成率 100% > 单题完美）。
    #
    # V2.4 余量冗余（铁律⑨）：目标 5h 完成（18000s），留 1h 余量给官方 API 波动
    # （V1 评测 15.9s/请求 → V2.1 评测 35s/请求，慢一倍多）。PaperPacer 以 5h 为
    # 目标收紧，即使 API 变慢也有 1h 缓冲，杜绝 V2.1 的 19 题超时被强杀。
    "paper_total_seconds": 18000,
    # ==================== VeritasMath 升级配置 ====================
    # 难度感知软预算（全局约束：112 题 / 平台并发 3 / 6h 上限 ⇒ 平均每题 ~9.6min）。
    # 分类节点在同一 prefill 调用里顺带输出难度（边际成本≈0），TimeBudget 据此
    # 调整 soft_total：客观题/简单题让出资源，难题拿满全预算。hard_total 永远是
    # 1200s 平台硬限；软预算只决定"可选阶段"的购买力，不会杀死进行中的工作。
    "difficulty_soft_budgets": {"easy": 480, "medium": 840, "hard": 1200},
    # 异构双方法推理：首轮只在高风险领域（组合/竞赛构造类单样本错误率最高）
    # 并行扇出第二条推理路径；其余题目在首轮不一致/不确定时由调解阶段以
    # "换方法"提示触发第二路径，不在 happy path 上增加成本。
    "enable_diverse_reasoning": True,
    "high_risk_domains": ["离散数学", "非基础及进阶课程", "微分几何", "测度积分"],
    # 过程审计智能体：对已定候选做题面契约完整性 + 关键计算抽核。
    # prefill JSON 判定 ~10-20s；不完整且可负担时回调解做定向补算。
    "enable_critic": True,
    "critic_reserve_margin_s": 60,
    # 确定性复算季后赛：两路数值冲突时把候选代回原题复算/小规模枚举对照，
    # 替代"只能二选一"的仲裁。一次 playoff ≈ 一次 LLM 生成 + 一次代码执行，
    # 远低于整轮 solving 子图重跑。
    "enable_playoff": True,
    # 客观题低置信复核投票：仅当首次抽取置信不足（空位数不足/来源为兜底）时
    # 追加一次独立短采样比对；一致则提置信，不一致则携带双候选交仲裁。
    "enable_objective_revote": True,
    # 判断题双向确认（移植启元实证机制）：Intern-S2 对"是否"题系统性偏"否"
    # （90% 判断错题同根因）。结论→温度0独立自证→一致采纳/反向温度0重解。
    "enable_judge_confirm": True,
    # 计数题枚举对照守护（冲刺满分 M6 实证）：组合计数是
    # LLM 最弱项。检出计数题则强制小规模枚举对照 + 代码静态核查枚举证据。
    "enable_counting_guard": True,
    # 符号等价判定加固：simplify 限时（秒）、自由符号对齐映射、采样维度上限。
    "matcher_simplify_timeout_s": 8,
    "matcher_max_sample_symbols": 4,
    # 模结构守护：检出 F_2 / Z_m / 模运算语境时向 Python 提示注入"结构内聚合"
    # 强制条款，并对生成代码做确定性静态核查（评委报告 ：F_2 六值被按
    # 普通整数相加得 3，应模 2 得 1）。
    "enable_modular_guard": True,
    # V2 M4 答案形式对齐：数学对但形式不合（答区间而非半长）时，
    # 低成本 LLM 重述修正（~512 token）。关闭即跳过。
    "enable_form_align": True,
    # V2.1 M7 ProofDeepener：证明/推导题结构补强（定理陈述→步骤链→结论），
    # 解决 L3/L4 证明题"内容对但结构不被 judger 认可"的失分。
    "enable_proof_deepener": True,
    # V2.3 M12 证明自检：审稿查逻辑漏洞（prefill 低成本），有实质漏洞补一轮。
    "enable_proof_self_critique": True,
    # V2.3 M14 难题兜底：放弃型答案触发部分求解（特殊情形+思路框架+部分结论）。
    "enable_hard_fallback": True,
    # 评委建议 5：reserve 划出固定配额给语义仲裁与应急直答，杜绝
    # `arbiter=skipped(0.0s)`。含义：硬上限前剩余 ≥ 该值时，prefill 仲裁
    # （~10s）与应急直答（~30s）仍然放行；再低则只做确定性兜底。
    "arbiter_reserve_quota_s": 75,
    "emergency_reserve_quota_s": 90,
    # 2026-08-10 评委建议 2：压缩重试（prefill，~150s）与完整重试（~700s）
    # 分开定价。压缩重试按 reserve_margin 模式放行——软预算已尽也可执行，
    # 只要求硬上限前剩余 ≥ 压缩估时 + 本余量（余量覆盖仲裁 prefill、应急
    # 直答与确定性拼装；node_wrapper 的硬超时兜底最坏情况）。评委实测：按
    # 软预算定价时压缩重试 30 题 0 次放行，6 题以捞回残片出厂。
    "compressed_reserve_margin_s": 150,
    "log_level": "INFO", "log_dir": "logs",
    # ==================== V2.5 确定性求解器矩阵（移植 backup-v255） ====================
    # utils/det_solver.py：15 条通用确定性管线（枚举/模周期/递推/特征值/插值/
    # 求积/图优化/计数/概率/ODE/BVP/留数/级数/方程组/期望），每条强制自验 +
    # 问目标守卫（题面顺带出现 C(n,k)/矩阵/方程组但题问别的时绝不劫持）。
    # 命中且自验通过 → answer_source=det_solver 作为推理候选进入 V2.5 原生
    # 交叉验证流程（不锁定、不跳过反证——easy112 实测 45% 错题为计算错，
    # 正是该确定性管线的靶心）。
    "enable_det_solver": True,
    "det_solver_timeout_s": 15,
    # V2.5 解集完整性守护（easy112 错题归因 P1）：Find all/所有解/最小保证
    # 类题强制解集枚举对照——idx111 漏周期解 5π/4、idx11 鸽巢差 1 即此类。
    "enable_solution_guard": True,
}
