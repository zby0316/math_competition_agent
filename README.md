# 数学推理智能体系统

面向数学题自动求解的多智能体系统，基于状态图流水线编排，将解题拆分为领域分类、LLM 推理、Python/SymPy 验证、答案交叉校验、语义仲裁、冲突调解、最终协调输出等阶段，稳定返回结构化、可判分的最终答案。

## 核心能力

- 支持计算题、证明题、选择题、判断题、多问/枚举题等
- 覆盖 18 个数学领域（抽象代数、高等代数、数学分析、常微分方程、偏微分方程、复分析、拓扑学、微分几何、泛函分析、测度积分、概率论、统计推断、线性回归、运筹学、离散数学、数值分析、随机过程等）
- LLM 推理 + Python/SymPy 双路并行求解，交叉验证提高正确率
- 语义仲裁：只从既有候选中选择完整答案，不凭空改写
- 冲突调解与重试：两路答案不一致时定向重跑
- 墙钟预算管理：单题 20 分钟硬限制内分级降级，不交白卷
- 答案前置 + 断点续写：截断也不丢答案
- 完整可审计 trace，支持断点续跑

## 环境要求

- Python 3.10+
- 依赖：见 `requirements.txt`

## 快速开始

### 1. 配置 API

复制 `.env.example` 为 `.env`，填入：

```dotenv
INTERN_API_KEY=你的API密钥
INTERN_API_BASE=https://chat.intern-ai.org.cn/api/v1/chat/completions
INTERN_MODEL=intern-s2-preview
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

Windows 环境若将文件按 GBK 读取而报编码错误，可用 `python -X utf8 -m pip install -r requirements.txt`。

### 3. 批量运行

```bash
python main.py --input_file input.jsonl --output_dir outputs
```

输入为 JSONL，每行一道题：

```json
{"idx": 0, "problem": "求模 $20$ 的整数环 $\\mathbb{Z}_{20}$ 中乘法可逆元的个数。"}
```

输出写入 `outputs/{idx}.json`，包含 `final_response` 和 `trace`。已存在的非空结果会自动跳过，支持断点续跑。

### 4. 评测入口

平台通过 `user_agent.py` 的 `ReasoningAgent.solve(problem, metadata)` 调用，返回符合评测契约的字典：

```python
{
    "final_response": "8",
    "trace": [...]
}
```

### 5. 本地诊断题集

`benchmarks/umath_text24.jsonl` 是固定的 24 道 U-MATH 纯文本题，用于比较不同代码版本的逐题表现。来源、重建方式及自动判分限制见 [诊断集说明](benchmarks/README.md)。运行评测会调用模型 API；仅重建数据不会。

拓扑、测度与泛函分析另有新编的 12 道证明题及人工清单，均已分批试跑；首批暴露的问题修复后，原失败题另复跑一次，剩余六题严格核对为 3 通过、3 需修正。见[证明题说明](benchmarks/拓扑测度泛函证明题说明.md)、[首批报告](benchmarks/证明题首批六题评测报告.md)和[后续报告](benchmarks/证明问题修复与剩余六题评测报告.md)；这些结果不代表官方提分。

## 目录结构

```
├── user_agent.py          # 评测入口（ReasoningAgent.solve）
├── main.py                # 本地批量运行
├── agent_pipeline.py      # 主图构建
├── llm_client.py          # API 客户端
├── config.py              # 全局配置
├── graph/                 # solving 子图
├── nodes/                 # 各阶段节点
├── state/                 # 共享状态
├── utils/                 # 工具函数
├── exec_server/           # Python 执行
└── knowledge/             # 18 个数学领域知识库
```
