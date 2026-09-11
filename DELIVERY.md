# 交付说明（挑战杯·书生赛道 初赛）

## 一、队伍与赛道

| 项目 | 内容 |
| --- | --- |
| 赛道 | 2026 年度中国青年科技创新"揭榜挂帅"擂台赛·书生赛道<br>赛道一《基于 Intern-S 系列大模型的数学智能体设计与推理创新》 |
| 队伍名称 | 牢梁 |
| 团队 ID | 2079565998873833474 |
| 提交人 | qiyuezhuxiang |
| AtomGit 自建组织 | qiyue-math-agent |
| 交付仓库 | https://gitcode.com/qiyue-math-agent/math_competition_agent |
| 评测分支 | `main` |
| 选用模型 | `intern-s2-preview`（由平台官方 Client 注入；代码不持有任何密钥） |
| 入口文件 | `user_agent.py`（`ReasoningAgent`，`__init__(self, client, *args, **kwargs)`、`solve(self, problem: str, metadata: dict) -> dict`） |

## 二、运行方式

评测入口（平台调用，签名不可变）：

```python
from user_agent import ReasoningAgent

agent = ReasoningAgent(client=official_client)
result = agent.solve(problem, metadata)   # -> {"final_response": "...", "trace": [...]}
```

本地批量运行与自测：

```bash
pip install -r requirements.txt
python main.py --input_file input.jsonl --output_dir outputs      # 批量求解（断点续跑）
python eval_local.py --dataset <本地带标签.jsonl> --concurrency 3  # 本地离线对照（工件不入库）
```

## 三、项目结构

```
├── user_agent.py          # 评测入口（ReasoningAgent.solve），返回契约与 trace
├── main.py                # 本地批量运行（断点续跑）
├── agent_pipeline.py      # 主状态图编排
├── llm_client.py          # 官方 Client 适配
├── config.py              # 全局配置与 AGENT_VERSION
├── graph/                 # solving 子图
├── nodes/                 # 各阶段节点（分类/推理/验证/仲裁/装配）
├── state/                 # 共享状态定义
├── utils/                 # 时间预算、答案抽取/净化、证据与裁决工具
├── exec_server/           # Python/SymPy 执行器
└── knowledge/             # 18 个数学领域知识库（方法论速查 + 作答要点）
```

## 四、依赖

见 `requirements.txt`：`requests`、`langgraph`、`langchain-core`、`mcp`、`sympy`、`mpmath`、`numpy`、`scipy`、`scikit-learn`、`python-Levenshtein`、`typing-extensions`、`python-json-logger`。全部为公开 PyPI 包，无本地私有依赖。

## 五、合规声明

- 无硬编码密钥/令牌：密钥仅从环境变量或本地 `.env` 读取，`.env` 未入库（`.gitignore` 排除）。
- 无本机绝对路径：全部文件读取使用相对路径（含知识库与执行器）。
- 仓库不包含平台私有评测集及其答案文件；本地离线测量用的带标签题集仅存于本地，并已在 `.gitignore` 中显式排除（`test_sets/`、`eval_112.*`、`_tmp_*`、`ab_*` 等），防止误入库。
- 不修改、不绕过平台 Client 的模型调用、限流、Token 与超时管控。
- 单题异常被逐层捕获：不使整体崩溃；返回值恒为可 JSON 序列化字典，且 `final_response` 非空（无答案时给出可判分的结论或可读的部分结论）。
- 代码不含破坏性/恶意执行逻辑；Python 执行器仅运行求解所需的符号/数值计算。

## 六、版本与溯源

`config.AGENT_VERSION = v71.43base+fix5+drift-guard+diag-probe+proof-skip+kb9+answer-delivery 2026-09-11`

`solve()` 的 trace 头部携带该版本串，与评测日志中的 Commit ID 相互对照，可确定任一分数对应的代码版本。

## 七、本地验证记录（本版本）

- 全树 `compileall` 通过；`user_agent` 导入与入口签名自检通过（`ReasoningAgent(client, *args, **kwargs)`、`solve(problem, metadata) -> dict`）。
- 新增"答案行交付闸门"单元验证：对截断残段（"First, note that if X i"、"满足条件的 $(m,n)$ 中 $m+n$ 的"、"n (all ones). The game"）判定为不可判分并替换为确定性产物；含具体数值的合法结论（"恒为 0"、"最小总费用为 4202432 硬币"）保持原样；证明题体裁不经此门；剩余硬时限不足 90s 时不触发应急调用。
- 6 道历史难题（idx 4/13/24/48/50/71）本地对照实验（对照 = 同一代码的 09-09 基线记录）：
  - idx 24：基线为截断残段 → 本次输出 `最终答案：2048`（与标签一致）；
  - idx 13/48/50/71：答案行由乱码/残段 → 干净、可判分的短答案；
  - idx 4：与基线一致（同为 44，标签 2024）；
  - 本轮对照中闸门触发 0 次（上述差异来自运行方差，不作为闸门收益证据）；闸门定位为针对已记录失败模式（截断残段整题出厂）的保险。
  本地对照实验的原始工件与带标签题集不入库（见 `.gitignore`）。
