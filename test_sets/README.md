# test_sets/ —— 本地离线验证基准（目录内容不入库）

本目录存放**只用于本地离线测量**的带答案评测集。判分系统克隆的是仓库
main 分支，因此**任何含官方评测题/答案的文件都绝不能提交**（官方规则：
禁止标准答案入包）。目录里带答案的文件一律被 .gitignore 排除。

## 离线基准获取方式（合规：仅本地使用，不提交）

- `eval_112.json`：112 题带答案标签的本地验证集，从 awa 团队的公开仓库
  [sentuyu/Challenge-Cup-2026-math-agent](https://gitcode.com/sentuyu/Challenge-Cup-2026-math-agent)
  的 `eval_112.json` 复制而来（其 README 注明为挑战杯离线验证集）。
- 用途：
  1. 本地跑通管道并粗略判分（见仓库根目录 `eval_local.py`）；
  2. `python tools/probe_qbank.py` 测量公开题库对本地题集的相似题命中率——
     在花真机提交额度前，给出检索机制收益的诚实预期。

## 已提交的合规部分

- `qbank/`：NuminaMath-CoT（公开数据集）olympiads 子集（公开题+公开解答），
  由 `tools/build_qbank.py` 构建——公开数据检索是官方明文鼓励的优化方向。
