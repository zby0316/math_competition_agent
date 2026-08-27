# VeritasMath V2.5（HEAD d396205）安全与合规审计报告

- 审计对象：`C:\Users\34707\Desktop\math_competition_agent (1) - 副本`（挑战杯数学智能体项目）
- 审计版本：git HEAD = `d396205`（main 分支，V2.5，拟提交版本）
- 审计时间：2026-08-27
- 审计方法：静态代码审查（grep 交叉验证 + 沙箱绕过载荷实测 + 端到端执行验证 + git 历史/忽略规则核对），未修改任何项目代码
- 官方红线（据 `参赛资料/_extracted/初赛要求.txt`）：评测仓库不得含评测日志/题集/标准答案；不硬编码 API key；联网面仅官方 client；选手代码不得依赖 answer/标准答案/隐藏评测数据

---

## 审计结论摘要

| 严重度 | 数量 | 一句话结论 |
|---|---|---|
| P0 致命 | 0 | 当前提交面（main@d396205）未发现必然触发红线的已提交内容 |
| P1 高危 | 2 | ① Python 执行沙箱可被系统性绕过（任意文件读已实证，命令执行/写文件/联网载荷全部通过静态扫描）；② `参赛资料/`（含官方评测日志）未被 .gitignore 覆盖，`git add .` 即入库 |
| P2 中危 | 2 | ③ 执行器进程内回退路径 `_exec_in_process` 零防护；④ requirements 携带 requests + llm_client 硬编码 API base/model 留在提交面 |
| P3 低危 | 6 | 提示词硬编码模型名、skills_loader 路径拼接无内建白名单、`__init__` 未兜底、client 属性篡改、分支历史污染、未跟踪文件 |
| 建议 | 4 | .gitignore 补漏、沙箱改 AST 白名单、只推 main、提交前核对 |

---

## 1. 硬编码 API key / token / 密钥 —— ✅ 未发现真实密钥

**证据**：全仓（含 .workbuddy/参赛资料）grep `sk-[A-Za-z0-9]{16,}`、`api_key=`、`Bearer `、`token=`：

- `llm_client.py:36-40` 唯一密钥来源是环境变量，无硬编码：
  ```python
  raw_api_key = os.environ.get("INTERN_API_KEY")
  if not raw_api_key:
      raise RuntimeError("Missing API key. Set INTERN_API_KEY.")
  self.authorization = (raw_api_key if raw_api_key.startswith("Bearer ") else f"Bearer {raw_api_key}")
  ```
- `README.md:211`、`benchmark/README.md:13`、`docs/提交前自检清单.md:51`、`.workbuddy/output/material_digest.md:78`、`参赛资料/_extracted/*.txt` 中的 `sk-...` / `YOUR_API_TOKEN` 均为**文档占位符**，非真实密钥。

**影响**：无密钥泄露。平台路径（user_agent → graph → utils）不读任何环境变量密钥。

**建议**：维持现状；提交前按 `docs/提交前自检清单.md:64` 抽查 trace 输出不含密钥。

---

## 2. 绝对路径依赖 —— ✅ 无

**证据**：tracked `*.py` 中 grep `C:\Users`、`C:/Users`、`/home/`、`/opt/`、`/root/`、盘符 等 **0 命中**。

- `user_agent.py:28-38` sys.path 自举用 `__file__`/`os.getcwd()` 双候选相对定位（平台沙箱加载器无 `__file__` 时优雅降级）；
- `utils/skills_loader.py:128` 用 `Path(__file__).resolve().parent.parent / "skills_pythonscripts"` 相对仓库定位；
- `tests/test_platform_defenses.py:92-127` 的 `os.path` 拼接仅测试自用，非运行路径。

**影响**：在 `/opt/atomgit` 下可正常运行。

---

## 3. 读取本地私有文件 / 标准答案 / 评测数据 —— ✅ 运行代码不读取，但沙箱可越权读（见 §4）

**证据**：tracked `*.py` 中 grep `local_test`、`参赛资料`、`omni_math`、`tiku`、`jiexi`、`fuce`、`official_style`、`.jsonl` **0 命中**；`main.py:17` 读取的是本地 runner 命令行传入的 `--input_file`。

**影响**：代码本体合规（对应红线"不得依赖 answer、标准答案或隐藏评测数据"）。

**注意**：`mcp_servers/python_executor/server.py` 的执行沙箱可读容器内任意文件（§4 有实证），平台容器内若存在任何敏感文件（含 `/proc/<pid>/environ`）均可被模型生成的代码读到。

---

## 4. Python 执行沙箱安全性 —— 🔴 P1 高危（可绕过，已实证）

### 4.1 现状：纯正则黑名单，非 AST 白名单

`mcp_servers/python_executor/server.py:26-36`：

```python
_BLOCKED_MODULE_RE = re.compile(
    r"^\s*(?:import|from)\s+(subprocess|socket|requests|urllib|httpx|ctypes|"
    r"shutil|multiprocessing|threading|signal|webbrowser|ftplib|telnetlib)\b", ...)
_BLOCKED_CALL_RE = re.compile(
    r"\bos\s*\.\s*(system|popen|remove|unlink|rmdir|removedirs|rename|replace|"
    r"kill|killpg|environ)\b|"
    r"\b(eval|exec)\s*\(|__import__\s*\(|"
    r"\bopen\s*\([^)]*[\"'][wa]\+?[\"']", ...)
```

### 4.2 绕过载荷实测（对 `_static_safety_scan` 逐一验证，16 个绕过载荷 **全部通过** 扫描，拦截率为 0）

| # | 载荷 | 扫描结果 | 能力 |
|---|---|---|---|
| A | `from os import system` → `system('id')` | ✅ 通过 | 任意命令执行（`from X import Y` 不在黑名单形态内） |
| B | `getattr(__builtins__, '__import__')('subprocess').Popen(...)` | ✅ 通过 | 任意导入 + 子进程 |
| C | `__builtins__['__import__']('os').system('id')` | ✅ 通过 | 任意命令执行（避开 `__import__(` 字面量） |
| D | `open('/etc/passwd').read()` | ✅ 通过 | **任意文件读取**（只读模式不在拦截范围） |
| E | `import pathlib; pathlib.Path('/etc/passwd').read_text()` / `write_text()` | ✅ 通过 | 任意文件读写（pathlib 不在模块黑名单） |
| F | `os.open('/tmp/x', O_WRONLY\|O_CREAT)` + `os.write` | ✅ 通过 | 任意文件写（os.open 不在调用黑名单） |
| G | `from os import environ` | ✅ 通过 | 读取环境变量（绕 `os.environ` 拦截） |
| H | `os.execl('/bin/sh','sh')` / `os.fork()` | ✅ 通过 | 进程接管 / fork（不在调用黑名单） |
| I | `import smtplib` / `import http.client` / `import pty` | ✅ 通过 | 网络连接 / 伪终端（模块不在黑名单） |
| J | `getattr(__builtins__, 'ex'+'ec')('print(1)')` | ✅ 通过 | 任意代码执行（绕 `exec(` 字面量） |
| K | `import numpy as np; np.fromfile(...)` | ✅ 通过 | 任意文件读取 |
| （对照） | `import subprocess` / `os.system` / `eval(` / `open(...,'w')` | ❌ 拦截 | 常规写法被拦（证明黑名单仅挡字面量形态） |

### 4.3 端到端实证（真实 `execute_python` 子进程路径，只读验证）

```
BENIGN: True '最终答案: 4\n' answer= 4 backend= mcp        # 正常数学代码可执行
READ_GIT_CONFIG success: True  stdout head: '最终答案: [core] repositoryformatversion = 0 ...'
READ_WIN_INI   success: True  stdout head: '最终答案: ; for 16-bit app support ...'
```
→ **沙箱内子进程可读取仓库外/系统级文件**（.git/config、C:/Windows/win.ini），写文件、命令执行载荷同理可绕过（§4.2）。

### 4.4 进程内回退路径 `_exec_in_process` —— 🔴 P1（零防护）

`mcp_servers/python_executor/server.py:165-204` + `utils/python_mcp_client.py:17-25`：

```python
def execute(self, code, timeout=30):
    try:
        result = _server.execute_python(code, timeout)   # 静态扫描 + 子进程
        ...
    except Exception:
        result = _server._exec_in_process(code, timeout) # ← 任何异常即回退
```

- `_exec_in_process` 的命名空间是 `{"__builtins__": __builtins__}`（**全量内建**，含 `__import__`/`open`/`exec`），`exec(code, namespace)` 直接执行，**无静态扫描、无 RLIMIT、线程超时不可杀**；
- 触发条件：`execute_python` 内部异常（如 `tempfile.NamedTemporaryFile` 失败、subprocess 基础设施异常）。一旦触发 = **在智能体宿主进程内执行任意代码**（可读平台进程环境、改内存态、干扰并发题）。

### 4.5 其他弱化点

- 子进程**继承父进程 cwd**（`server.py:131-136` 未设 `cwd=`），即仓库目录，配合 §4.2 写文件载荷可污染仓库文件；
- `env` 已裁剪为 5 个变量（`server.py:113-120`，好缓解），但 `/proc/<parent>/environ` 等仍可读；
- `RLIMIT_AS/RLIMIT_CPU`（`server.py:60-61`）仅 POSIX 生效，Windows 平台无资源限制，只有 30-60s 墙钟。

### 4.6 可利用性评估

威胁模型：**题目文本不可信**（可能含 prompt 注入）→ LLM 生成的验证代码可被诱导包含上述绕过载荷 → 执行器执行。评测容器若断网，外联受限，但**容器内任意文件读写、命令执行、并发题干扰、资源耗尽**均可达成；若容器有代理出口（如 pip 源），配合 `requests`（requirements 已声明）可外联。

### 4.7 修复建议

1. 改为 **AST 白名单**：`ast.parse` 后仅放行纯计算节点子集（`Expr/Assign/AnnAssign/For/While/If/Return/Print(限定)/BinOp/UnaryOp/Compare/BoolOp/Name/Constant/Call(仅白名单函数与 sympy/numpy 数学 API)/Attribute(禁止 `__` 前缀属性链)`），禁 `import`/`from`/`exec`/`eval`/`compile`/`open`/`getattr`/`globals`/`locals`/`type`/`__builtins__` 访问；
2. 子进程加固：`cwd=` 指向私有临时目录、POSIX 降权 uid/gid、`close_fds=True`、全平台 RLIMIT（Windows 用 job object 或降级策略）、默认拒绝出站网络（容器防火墙）；
3. `_exec_in_process` 删除或复用同一 AST 白名单校验后再执行，禁止以全量内建命名空间 exec；
4. 生成代码时在提示词中强制"仅允许纯数值计算，禁止文件/网络/系统调用"，但**不依赖提示词**（白名单兜底）。

---

## 5. 调试痕迹 / 敏感输出 —— ✅ 基本合规（P3 提醒）

**证据**：

- `utils/logger.py:16-26`：日志仅 `StreamHandler`（stderr），`get_log_dir()`（`logger.py:29`）**无任何调用者**；全仓无 `FileHandler`/`basicConfig`/`RotatingFileHandler` → 不写日志文件，不会在评测容器留日志文件；
- `user_agent.py:82-87, 110-112`：INFO 日志仅记录 client 类型/签名（探针信息），非密钥；
- trace 中 `raw_response_excerpt`（`user_agent.py:169-170`，头 600 + 尾 1400 字符）、python `stdout/stderr`（`user_agent.py:218-219`）会携带模型原始输出与执行输出——按官方 FAQ/PDF，trace 作为 judger 输入的一部分**要求记录候选与验证过程**（git 历史 `bbc0200` "回退 trace 脱敏——PDF 明确要求记录候选答案/验证过程"），属合规设计；
- `main.py:88/106/119/122` 仅打印本地进度（本地 runner）。

**影响**：无日志文件、无密钥泄露。唯一提醒：trace 的摘录内容是模型输出，若模型输出里混入异常内容会被如实记录（概率极低）。

---

## 6. .gitignore 与 git ls-files —— 🔴 P1（提交风险）

### 6.1 已正确忽略的部分 ✅

`.gitignore:1-42` 已排除：`__pycache__/`、`*.py[cod]`、`*.log`、`logs/`、`outputs/`、`sample_outputs/`、`test/`、`competition/`、`local_test/`、`.workbuddy/`、`.playwright-mcp/`、`reports/`、`review_results.json`、`.pytest_cache/` 等。`git ls-files` = 148 个文件，**不含** local_test/参赛资料/reports/.workbuddy/日志/答案文件 → 当前提交面干净。

### 6.2 未被忽略的敏感目录 —— 🔴 `参赛资料/`

- `git status --porcelain` 显示 `?? 参赛资料/`（untracked）；
- **`git add -n .`（干跑实证）会暂存 12 个文件**，其中 10 个属于 `参赛资料/`：
  - `参赛资料/版本日志/eval_log_*.log`（**官方平台评测日志**——直接命中红线"评测仓库不得含评测日志"）
  - `参赛资料/初赛要求.pdf`、`赛题简介.pdf`、`赛题与赛程概述.pdf`、`_extracted/*.txt`（赛题资料）
- 若提交前执行 `git add . && git commit && git push`，即触发官方红线，可能导致整仓不合规。
- ⚠️ 提醒：本机 git 2.54 上 `git check-ignore '参赛资料/'`（**带尾斜杠**）会对**任意目录**误报"已忽略"（对全新 `zzz_quirk_test/` 同样误报 `.gitignore:39` 命中），判断忽略状态请以 `git add -n .` / `git status --porcelain` 为准。

### 6.3 git 历史污染（非 main 分支）⚠️

- **main 分支历史干净**：`git log main --name-only` 无 local_test/参赛资料/answers 相关文件；
- 但 **`origin/dev` 分支历史含 `standard_answers.json`（commit d08b44e）、`answers.csv`、`solve_log.jsonl`、`_archive/平台提交包/answers_map/*`**（`git branch -a --contains d08b44e` → origin/dev）；`v32/v33/backup-v255/backup-v272` 分支含大量 local_test 数据与 V5.x 评测产物。
- 官方只拉取 main 分支则无碍；**若误把全部分支 push 到官方仓库，即构成题集/标准答案/评测日志入库**。

### 6.4 修复建议

1. `.gitignore` 追加 `参赛资料/`、`docs/architecture.png`（如需保留则显式 `git add -f`）；
2. 提交前固定执行 `git add -n .` 核对暂存清单；
3. 推送时**只推 main**：`git push origin main`；不要 `--all` / `--mirror`；
4. 可选：对 origin/dev 做历史清理（rebase/force-push 移除 d08b44e 等敏感提交）或直接删除 dev/v32/v33/backup-* 分支。

---

## 7. 网络面 —— ✅ 平台路径仅官方 client（2 个注意点）

**证据**：

- 平台可达路径（user_agent → langgraph_math_agent → nodes/utils）中唯一的 chat 出口是 `utils/response_normalize.py:116-158` 的 `chat_compatible(client, ...)`，经 `utils/llm_retry.py:116-117` 单点收口，最终调用**注入的官方 client.chat**，不传 model/base，不读环境变量；
- `requests` 仅出现在 `llm_client.py:6,86`（本地样例客户端），grep 证实**只有 `main.py:8` import llm_client**，`user_agent.py` 及其依赖链不 import → 平台运行时 requests 不可达；
- `utils/client_probe.py:128-169` `probe_contract` 在首题额外发 2 次最小 chat 探测（走官方 client，合规，量极小）。

**注意点**：

1. ⚠️ `requirements.txt:2` 声明 `requests>=2.31.0` → 评测容器会安装一个 HTTP 客户端；配合 §4 沙箱绕过（`__builtins__['__import__']('requests')`）即可外联。建议提交版本**移除 requests 与 llm_client.py**（或明确标注仅本地调试），或在容器层面禁止出站网络（官方断网红线本身就是缓解）。
2. ⚠️ `utils/client_tuning.py:31,59,99` 对**平台注入的 client 做可变属性修改**（`timeout` 提升到 780s、`retry` 降到 1）。best-effort、失败无害，但属"改动官方 client"范畴，建议评审确认平台接受（若平台 client 为只读对象会自动跳过，不影响运行）。

---

## 8. 依赖注入风险 —— 基本安全（2 个低危点）

**证据**：

- `utils/deps.py:5-17`：`Deps` 纯 dataclass，`get_deps(config)` 从 langgraph config 取注入对象，无外部可控输入面；`MockClient` 仅测试用；
- `llm_client.py:11,42-43`：`DEFAULT_API_BASE = "https://chat.intern-ai.org.cn/api/v1/chat/completions"` 与 `DEFAULT_MODEL = CONFIG["model"]`（`config.py:2` = `"intern-s2-preview-397b"`）为本地样例硬编码——仅本地可达，但**留在提交面**，若评审做静态扫描"硬编码 API base/model"可能误判，建议本地化处理；
- `utils/python_mcp_client.py:22-24`：异常回退 `_exec_in_process`（零防护）——已列入 §4.4；
- `utils/skills_loader.py:158-168`：`get_skill_document(category)` / `get_validation_script(category)` 用 `self.base_path / category / f"{category}skill.md"` 拼路径且**无内建白名单**。当前被 `nodes/classifier_node.py:77-127` `_resolve_category` 严格归一化到真实目录名缓解（返回值 ∈ 实际文件夹名），目录穿越不可达；但 loader 自身不防御，未来其他调用方传原始字符串可读任意 `<名字>skill.md` 文件（P3）。

---

## 9. 官方平台合规 —— 基本合规（3 个 P3）

| 检查项 | 结论 | 证据 |
|---|---|---|
| user_agent 硬编码 model/温度/API base | ✅ 无 | `user_agent.py` 全文件无 model/base/温度硬编码；温度与 max_tokens 来自 CONFIG 作为 chat 参数透传，官方 client 不支持时 `chat_compatible` 三级降级（`response_normalize.py:135-156`） |
| solve 抛异常 | ✅ 稳健 | `user_agent.py:104-127` 主体全在 try/except 内，异常返回"最终答案：未能求解"+ trace；fallback backend（`user_agent.py:455-565`）自包含、不 import 项目模块；`_validate_output` 的 assert 也在 try 内 |
| 并发/超时 | ✅ 合规 | `config.py:106` `problem_time_budget_s=1200` 匹配平台单题硬限；`config.py:117` `paper_total_seconds=18000`（5h 目标 + 1h 余量）；`utils/timeout_control.py:45-59` 节点 ceiling 钳制；python 执行 60s（`config.py:22`）；并发 3 由平台控制，agent 内 Send 扇出为进程内并行 |

**P3 提醒**：

1. `utils/prompt_templates.py:49`：`REASONING_PROMPT` 提示词文本硬编码"使用intern-s2-preview-397b模型"——若官方模型不同，仅文案误导，建议删除或参数化；
2. `user_agent.py:69-92`：`ReasoningAgent.__init__` **未包 try/except**——若 `MathAgentGraph()`/`SkillsLoader()` 构造抛异常（如 langgraph 版本不匹配），平台在构造 agent 时直接失败 → 全卷 0 分（fallback 仅在 import 失败时生效，不覆盖构造失败）。本地测试通过，风险低，建议把构造也兜底；
3. `config.py:2` `model` 字段仅本地 llm_client 使用，平台路径不读——建议在提交文档中说明，避免评审误判为"硬编码模型"。

---

## 问题清单（按严重度）

### P0 致命
无。

### P1 高危

**P1-1 Python 执行沙箱可绕过（任意文件读已实证，命令执行/写文件/联网载荷全部通过静态扫描；进程内回退路径零防护）**
- 证据：`mcp_servers/python_executor/server.py:26-36`（正则黑名单）、`:165-204`（`_exec_in_process` 全量 `__builtins__`）；`utils/python_mcp_client.py:22-24`（异常即回退）；实测 16/16 绕过载荷全部通过 `_static_safety_scan`；端到端读取 `.git/config`、`C:/Windows/win.ini` 成功
- 问题描述：见 §4.2-4.4。威胁模型为题目文本 prompt 注入 → LLM 生成恶意代码 → 执行器执行；`from os import system`、`getattr(__builtins__,'__import__')`、`pathlib`、`os.open`、`smtplib`/`http.client` 等均可绕过
- 实际影响：评测容器内任意文件读写、命令执行、写仓库文件、资源耗尽、干扰并发题；若容器有出口则可外联；`_exec_in_process` 触发时可在宿主进程内执行任意代码
- 修复建议：AST 白名单（§4.7）；子进程 cwd 隔离 + 降权 + 全平台资源限制 + 禁出站网络；`_exec_in_process` 复用同一白名单或删除

**P1-2 `参赛资料/` 未被 .gitignore 覆盖，`git add .` 将官方评测日志/赛题入库**
- 证据：`.gitignore:1-42` 无 `参赛资料/` 条目；`git status --porcelain` → `?? 参赛资料/`；`git add -n .` 实证暂存 12 文件（含 `参赛资料/版本日志/eval_log_*.log`）
- 问题描述：见 §6.2。当前提交面干净，但一次 `git add .` 即触发"评测仓库不得含评测日志/题集"红线
- 实际影响：提交不合规，可能被拒评或判违规
- 修复建议：`.gitignore` 追加 `参赛资料/`；提交前 `git add -n .` 核对；只推 main

### P2 中危

**P2-1 `_exec_in_process` 回退路径零防护**（可并入 P1-1，单列强调）
- 证据：`server.py:174` `namespace = {"__builtins__": __builtins__}`；`python_mcp_client.py:22-24` 任何异常触发
- 影响：宿主进程内任意代码执行（无沙箱、无资源限制）
- 修复：删除该回退，或经 AST 白名单 + 受限命名空间后再执行

**P2-2 requirements 携带 requests；llm_client.py 硬编码 API base/model 留在提交面**
- 证据：`requirements.txt:2`；`llm_client.py:11,42-43`；`config.py:2`
- 影响：容器内存在 HTTP 客户端（配合沙箱绕过可外联）；静态扫描可能误判"硬编码模型/API base"
- 修复：提交版本移除 `requests`/`llm_client.py`（或声明仅本地），或容器层禁出站网络

### P3 低危

| # | 问题 | 证据 | 建议 |
|---|---|---|---|
| P3-1 | 提示词硬编码模型名文案 | `prompt_templates.py:49` | 删除或参数化 |
| P3-2 | skills_loader 路径拼接无内建白名单 | `skills_loader.py:158-168`（当前被 `classifier_node.py:77-127` 归一化缓解） | loader 内加 allowlist 校验 |
| P3-3 | `ReasoningAgent.__init__` 未兜底 | `user_agent.py:69-92`（fallback 仅覆盖 import 失败） | 构造异常也降级到 fallback |
| P3-4 | 修改平台注入 client 属性（timeout=780s/retry=1） | `client_tuning.py:31,59,99` | 评审确认平台接受；失败无害 |
| P3-5 | 分支历史污染 | `origin/dev`（d08b44e 含 standard_answers.json/answers.csv/solve_log.jsonl）、v32/v33/backup-*（local_test/V5.x 产物） | 只推 main；清理/删除敏感分支 |
| P3-6 | 未跟踪文件会被 `git add .` 带上 | `docs/architecture.png`、`docs/audit_v25_mechanism.md` | 决定保留（`git add -f`）或忽略 |

### 建议（非缺陷）

1. 提交前固定流程：`git add -n .` → 核对清单 → `git status --porcelain` → 跑 `tests/`（含 `test_platform_defenses.py`）→ 按 `docs/提交前自检清单.md` 逐项打勾；
2. 沙箱安全投入优先级：AST 白名单 > 子进程隔离/降权 > 禁出站网络（对评测场景，容器断网是最终兜底）；
3. 若官方允许，提交面可考虑删除 `llm_client.py`、`main.py`、`benchmark/` 等纯本地件，只保留平台路径所需（user_agent.py 及其依赖链 + skills_pythonscripts + requirements）——注意 main.py 引用了 llm_client，删除需同步调整，务必先跑通 tests；
4. 合规自检脚本化：把 §6.2 的 `git add -n` 检查、密钥扫描（`sk-[A-Za-z0-9]{20,}`）、绝对路径扫描写进一个 `local_test/合规检查` 或 CI 脚本（不随提交入库）。

---

## 附：审计过程关键命令（可复现）

```powershell
# 密钥/路径/网络面
git grep -nE 'sk-[A-Za-z0-9]{16,}|api_key=|Bearer ' -- '*.py'
git grep -nE 'C:\\Users|/home/|/opt/|/root/' -- '*.py'
git grep -nE 'requests\.|urllib|socket\.|http\.client|smtplib' -- '*.py'

# 沙箱绕过（静态扫描 + 端到端只读验证）
python D:\temp\...\sandbox_scan_test.py   # 16 个绕过载荷全部通过扫描（拦截率 0）
python D:\temp\...\sandbox_exec_test.py   # pathlib 读取 .git/config、C:/Windows/win.ini 成功

# 提交面核对（权威依据，勿用带尾斜杠的 check-ignore——本机 git 2.54 对任意目录误报）
git status --porcelain
git add -n .
git ls-files

# 分支污染
git log main --pretty=format: --name-only | findstr /i "local_test answers jsonl omni tiku jiexi fuce"
git branch -a --contains d08b44e
```
