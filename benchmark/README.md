# 基准测试模块（U-02 校准）

> 用途：用官方 18 子领域样例题，实测当前模型（intern-s2-preview）的基线正确率，校准"93/112 题是否在模型能力内"这一假设（U-02）。

## 背景

架构专家团冻结了"方法天花板，非模型天花板"的判断（依据 Intern-S1 CMO 102/126）。但 intern-s2-preview 与 CMO 版 Intern-S1 的能力差距无公开量化数据（U-02）。本模块用本地样例题实测，把"模型能力上限"从假设变成数据。

## 运行方式

```bash
# 1. 求解（用 V2.5 当前代码，跑 18 子领域样例题）
INTERN_API_KEY="sk-..." INTERN_MODEL="intern-s2-preview" \
  python local_test/run_tests.py \
    --input local_test/data/v25_112.jsonl \
    --output local_test/results/benchmark_v25.json \
    --concurrency 3 --live

# 2. LLM 判分（对齐官方 judger 口径，Intern-S 模型综合判定）
python local_test/grade_llm.py \
  --input local_test/results/benchmark_v25.json \
  --output local_test/results/benchmark_v25_graded.json \
  --concurrency 8
```

## 校准逻辑

| 实测基线 | 结论 | 后续动作 |
|---------|------|---------|
| ≥ 93/112（83%） | 93 题在模型能力内，P0/P1 改造是"榨干正确率" | 全力冲客观 50 分 |
| 75~90（67~80%） | 能力内可榨到 ~90，93 有压力 | 聚焦 P0 确定性校验 + P1 定理 |
| 67~75（60~67%） | 接近历史最高，天花板明显 | 回归 U-02，评估换更强模型 |
| < 67 | 样例题集比官方题集简单（历史本地 98% vs 官方 59% 教训） | 换更难的题重测，勿信本地高分 |

## 重要提醒

**历史教训**：本地自出题集曾判 110/112=98.2%，官方实测只有 67/112=59.8%（落差 43 题）。本地样例题**远简单于官方闭源题集**。本基准测试只用于**相对校准**（当前模型 vs 假设的 93 题），**不能线性外推官方分数**。
