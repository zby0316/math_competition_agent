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
                      "critic": 120, "playoff": 300, "substitute": 240,
                      "solver_decision": 5},
    "classifier_confidence_threshold": 0.85,
    # V2 M2 置信门控：最少资源高准确率
    #   high(≥0.90) → fast 单路径快速答（省 Python/双路径）
    #   low(<0.70)  → deep 深思考通道（M3 DeepSolver）
    #   中间        → standard 双路验证（现状）
    "confidence_gate": {"high": 0.90, "low": 0.70},
    # V2 M3 DeepSolver 深思考通道触发的领域（科研级超难题高发）
    # V2.6.1 修正："数论/组合数学"不是官方 18 领域名（分类器只会输出知识库
    # 文件夹名，这两项是死配置永不命中）；官方题集中的数论/组合内容落在
    # "离散数学"/"非基础及进阶课程"下，改对齐后可真正触发深思考 B 路径。
    "deep_solver_domains": ["离散数学", "非基础及进阶课程", "高等代数", "抽象代数"],
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
    # V2.6.1 注：以上是 V2.1 历史记录，当前实际值已被 V2.4 恢复为难度分级
    # （easy 8192 / medium 12288 / hard 16384，见 reasoning_tokens_by_difficulty），
    # reasoning_compressed 当前=12288（压缩重试估时按 12288 计价，见
    # nodes/reasoning_agent_node._COMPRESSED_CALL_ESTIMATE_S=260）。
    "max_tokens": {"classifier": 96, "classifier_fallback": 8192,
                   "objective_reasoning": 4096,
                   "reasoning": 12288, "python": 8192,
                   "reasoning_compressed": 12288, "python_compressed": 8192,
                   "reconciliation": 32768,
                   "semantic_arbiter": 96, "semantic_arbiter_fallback": 4096,
                   # V3.0 短输出化：coordinator 16384→8192（probe 实测长输出是
                   # 官方慢 API 的主因；prefill + 8K 叙述足够，post_process 兜底）
                   "coordinator": 8192, "emergency_answer": 1280},
    # finish_reason=length 截断时的续写补全 max_tokens。
    # V2.6 从 2048 上调：官方实测 truncated=203（V2.5，14.5%）暴露"答案超预算"
    # 截断，2048 续写在已写满上下文下极易再次截断造成雪球。按难度分级给足，
    # 一次续写尽量覆盖答案尾部；配合轮数=1（见下），不再循环累积截断计数。
    "truncation_continue_tokens": 4096,
    "truncation_continue_tokens_hard": 8192,
    # V2.6 截断根治：thinking_mode=False 确定性关"深度思考"（长 CoT）。截断根因
    # 是 CoT 计入 max_tokens、难题上吃光预算导致答案没写完。V2.4 用 prefill 变相
    # 关 CoT 拿到 59.82 最佳分（对比 V1 开 CoT 53.53），证明数学场景关 CoT 反而
    # 更好。官方原生 thinking_mode=False 比 prefill hack 更可靠。默认开启；
    # 若实测降质，改回 False 退回纯 prefill。
    "enable_thinking_mode_off": True,
    # 截断续写最大轮次（V2.6 修正：官方实测 truncated=203 雪球来自"续写→再截断
    # →再续写"循环累积，2 轮让截断计数翻倍且请求膨胀 57%。改为 1 轮：一次续写
    # 不够就走压缩重试（答案先行，max_tokens 12288），比循环续写更有效）。
    "truncation_continue_max_rounds": 1,
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
    # V2.6.2 单题 LLM 请求硬上限：防御深度保险（图内各循环已有结构性终止，
    # 此上限只拦"失控路径"）。官方实测均值 12.5 次/题、最重题 29 次；24 给
    # 合法路径留足余量，只截断雪球类失控。0 = 不限制。emergency_answer 豁免。
    # V2.7.1 审计微调 24→26：V2.7 新增投票（+2~3）与双方法守卫（+1）后，
    # 最重合法题可能触到 24；26 仍拦 30+ 雪球且不误伤合法难题。
    "max_llm_calls_per_problem": 26,
    # V2 M1 全卷完成率引擎（PaperPacer）：平台 6h 全卷硬限（秒）。
    # 官方评测实证：V1 每题固定 1200s 软预算导致 112 题只完成 16 题（14.29%）。
    # PaperPacer 按"剩余全卷时间 ÷ 剩余题数"动态收紧每题软预算，保证
    # 6h 内 112 题全部产出答案（完成率 100% > 单题完美）。
    #
    # V2.4 余量冗余（铁律⑨）：目标 5h 完成（18000s），留 1h 余量给官方 API 波动
    # （V1 评测 15.9s/请求 → V2.1 评测 35s/请求，慢一倍多）。PaperPacer 以 5h 为
    # 目标收紧，即使 API 变慢也有 1h 缓冲，杜绝 V2.1 的 19 题超时被强杀。
    # V2.6.3 收紧 18000→16200（4.5h）：V2.6.2 官方实测 API 59s/请求（再翻一倍），
    # 墙钟 5h57m——PaperPacer 只能按"题开始时"收紧，最后一波 3 题在飞时最多可
    # 再拖 20min，5h 目标实际滑到 5h57m 逼近 6h 悬崖。目标提到 4.5h 给尾波留
    # 1.5h 余量；API 快时 pacer 健康态仍给满 IDEAL 预算，不影响解题质量。
    "paper_total_seconds": 16200,
    # ==================== VeritasMath 升级配置 ====================
    # 难度感知软预算（全局约束：112 题 / 平台并发 3 / 6h 上限 ⇒ 平均每题 ~9.6min）。
    # 分类节点在同一 prefill 调用里顺带输出难度（边际成本≈0），TimeBudget 据此
    # 调整 soft_total：客观题/简单题让出资源，难题拿满全预算。hard_total 永远是
    # 1200s 平台硬限；软预算只决定"可选阶段"的购买力，不会杀死进行中的工作。
    # V2.6.2：easy 480→600。可选工作窗口 = soft_total − reserve(300)，480 时只有
    # 180s——误判为 easy 的难题 180s 后停购一切 LLM 调用（只剩 reserve 配额通道），
    # 安全垫太薄；600 给 300s 窗口，与 fast_path 阈值一致。
    # V2.6.3 回滚 600→480：官方实测 API 59s/请求（2 倍慢）下，easy 题多出的
    # 120s 购买力变成 +2 次调用的纯增量墙钟（~50 个 easy 题 ≈ +30min 墙钟），
    # 挤占难题预算；误分类风险已由分类器修复缓解。API 快时再考虑恢复。
    "difficulty_soft_budgets": {"easy": 480, "medium": 840, "hard": 1200},
    # 异构双方法推理：首轮只在高风险领域（组合/竞赛构造类单样本错误率最高）
    # 并行扇出第二条推理路径；其余题目在首轮不一致/不确定时由调解阶段以
    # "换方法"提示触发第二路径，不在 happy path 上增加成本。
    "enable_diverse_reasoning": True,
    # V3.0 python-first：python 确定性成功（success + evidence support/pass +
    # 完整答案 + 非近似算法题）时，推理降级为单次独立复核（light），省采样
    # 投票/重试/压缩通道——计算题从 ~12 次调用压到 ~4 次，把预算还给证明题。
    # 交叉验证/Critic/仲裁全保留；light 推理与 python 不一致仍走 mismatch 全链。
    "enable_python_first": True,
    # light 推理的 max_tokens（独立复核只需结论+简要推导，无需深探索）
    "reasoning_light_tokens": 8192,
    # V3.2：hard 题 python 代码生成最大尝试次数。实测 2 次在慢 API 下是负资产
    # （调用数 14→28、超时反噬）；回退 1（预算门禁仍保护）。
    "python_retries_hard": 1,
    # V2.6.1 对齐官方 18 领域分布：数值分析（13 题，11.61%，第二大池子）首轮
    # 也扇出异构第二路径——数值分析几乎全是可确定性核验的计算题（插值/迭代/
    # 求积/条件数），且评委历史错题（Hilbert 条件数/拉格朗日插值）正出自此。
    "high_risk_domains": ["离散数学", "数值分析", "非基础及进阶课程", "微分几何", "测度积分"],
    # 过程审计智能体：对已定候选做题面契约完整性 + 关键计算抽核。
    # prefill JSON 判定 ~10-20s；不完整且可负担时回调解做定向补算。
    "enable_critic": True,
    "critic_reserve_margin_s": 60,
    # 确定性复算季后赛：两路数值冲突时把候选代回原题复算/小规模枚举对照，
    # 替代"只能二选一"的仲裁。一次 playoff ≈ 一次 LLM 生成 + 一次代码执行，
    # 远低于整轮 solving 子图重跑。
    "enable_playoff": True,
    # V2.6.1 单候选代回核验（对标榜一 sympy 硬校验，V2.5 主线 2 补课）：
    # match 后 medium/hard 计算题把已一致答案代回题面条件做确定性核验，
    # 戳穿"两路同源一致地错"。PASS→critic；FAIL→带定向提示回调解重算；
    # 预算不足/无法机械核验→放行（锦上添花，不阻塞出答案）。
    # V2.6.3 默认关闭：V2.6.2 官方实测 57.14 分（64/112，V2.5 为 61.61）——
    # 官方 API 本轮 59s/请求（V2.5 时的 2 倍），核验每题 +1 次调用是纯增量
    # 成本，墙钟 5h57m 逼近 6h 悬崖且 4 题 invalid（疑似 20min 强杀）。
    # 代码保留，API 快（≤35s/请求）时改回 True 即可。
    "enable_substitution_check": False,
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
    # V3.0：contract probe 默认关闭——官方 runner 每进程解一题时类级缓存失效，
    # 每题白花 2 次调用（112 题 ≈ 12min 墙钟）；需要诊断官方 client 行为时开。
    "enable_contract_probe": False,
    # ==================== V3.3 多模型复核通道 ====================
    # 官方知识库 FAQ（约 3113-3116 行）明确允许"按需选择或组合多个 Intern-S
    # 系列模型"。第二模型只在**分歧路径**介入（语义仲裁选择候选、客观题低置信
    # 复核投票），主解题链路仍由主模型负责；与候选生成模型跨模型独立，分歧
    # 信号比同模型自评更可信。任何不可用（模型名无效/不支持逐调用覆盖/配额
    # 尽/调用失败）都静默回退主 client，与单模型行为完全一致。
    # 激活条件（满足其一）：① 官方注入 client 的 chat 接受 model= 参数（自动，
    # 零配置）；② 设环境变量 INTERN_REVIEW_MODEL + INTERN_API_KEY（本地调试）。
    "enable_review_model": True,
    # 复核模型名。提交前请在评分系统模型选择下拉里核对可用名（intern-s1/
    # intern-s1-pro/intern-s2-preview 等），以控制台实际列表为准。
    "review_model": "intern-s1-pro",
    # 每题复核调用封顶（防慢 API 下复核拖垮墙钟；超出后回退主 client）。
    "review_max_calls_per_problem": 3,
    # ==================== V2.7 快速采样投票（第三证据） ====================
    # V2.7.1：投票节点位于外图，match 出厂前与语义仲裁选定后都触发（真机
    # 实证主流路径是 uncertain_arbitrating——仲裁单选，投票必须跟在仲裁后）。
    # prefill 答案先行的独立短采样（~10-20s/次，只输出最终答案）做多数确认/
    # 反证；反证（≥半数样本聚类同一反方答案）→ 票选赢家作为第 4 候选交仲裁
    # 重选一次（answer_vote_max_rounds 轮次守卫防循环）。
    # N 按难度+剩余硬预算+API 实测速度自适应（慢 API 自动降 N），不牺牲完成率。
    "enable_answer_vote": True,
    # V2.7.1 审计微调：hard 4→3——投票发生在仲裁/验证之后（出厂前最后一道），
    # 59s/请求最坏下 4 次采样≈340s 会挤占收尾预留；_vote_n 自适应降 N 保留。
    "answer_vote_n": {"medium": 2, "hard": 3},
    "answer_vote_estimate_s": 20,
    "answer_vote_max_tokens": 512,
    # 每题投票-仲裁循环轮次上限（1 = 反证后仲裁重选一次即止）
    "answer_vote_max_rounds": 1,
    # ==================== V2.7 证明题深度思考 ====================
    # 官方 FAQ：judger 对证明/推导题"结合参考答案与解题过程综合判断"——
    # 证明质量 = 思考链深度。计算题保持 thinking OFF（快+防截断），证明/推导
    # 题在预算宽裕（硬上限剩余 ≥ proof_deep_min_remaining_s）时走原生 CoT
    # 路径（无 prefill、大额度）；预算不足自动回落标准 prefill 路径，绝不
    # 牺牲完成率。
    "enable_proof_deep_thinking": True,
    # question_mode 全集为 {proof, computation, choice, true_false, fill}
    # （utils/problem_profile.QUESTION_MODES）——"derivation" 等值永不出现，
    # 推导类题统一由 proof 覆盖。
    "proof_modes": ["proof"],
    "proof_tokens": 16384,
    "proof_deep_min_remaining_s": 900,
    # ==================== V2.7 确定性求解器矩阵 ====================
    # utils/det_solver.py：12+ 条通用确定性管线（枚举/模周期/递推/特征值/
    # 插值/求积/图优化/计数/概率/ODE/BVP/留数/级数/方程组/期望），每条强制
    # 自验（自验不过即放弃）。解出验证通过的结果 → 以 answer_source=det_solver
    # 短路 LLM 求解（与 sympy_pre_solve 同权重的确定性直通）。
    "enable_det_solver": True,
    # det_solver 单管线最长运行秒数（保守上限，防止个别管线卡住整题）
    "det_solver_timeout_s": 15,
    # ==================== V2.7 数值双方法自洽守卫 ====================
    # hard 计算题的 python 成功产出数值后，用独立第二方法复算（LLM 短调用）。
    # 一致 → 双方法自洽留证；不一致 → 证据降级 contradict，下游 playoff/仲裁
    # 重查。与 V3.2 无条件 hard 重试的区别：只在成功后触发 + 慢 API/预算紧时
    # 完全不触发（零回归风险，V3.2 8/12→7/12 教训）。
    "enable_dual_method_check": True,
    "dual_method_estimate_s": 40,
    # 观测 API 单次调用均价超过该值（秒）→ 不触发（慢 API 保护）
    "dual_method_max_api_s": 45,
    # 硬上限剩余低于该值（秒）→ 不触发（留足收尾）
    "dual_method_min_remaining_s": 700,
}
