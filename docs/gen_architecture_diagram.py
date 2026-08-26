# -*- coding: utf-8 -*-
"""生成系统架构总览图 docs/architecture.png（纯 matplotlib，离线可用）。

用法：python docs/gen_architecture_diagram.py
输出：docs/architecture.png（约 2800x3360）
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Polygon, Ellipse, FancyArrowPatch
from matplotlib import font_manager

# ── 中文字体 ──────────────────────────────────────────────
FAMILIES = ["Microsoft YaHei", "SimHei", "Microsoft JhengHei", "Arial Unicode MS"]
avail = {f.name for f in font_manager.fontManager.ttflist}
chosen = next((f for f in FAMILIES if f in avail), "DejaVu Sans")
plt.rcParams["font.family"] = [chosen, "sans-serif"]
plt.rcParams["axes.unicode_minus"] = False

EDGE = "#4a4a4a"          # 主箭头颜色
EDGE_D = "#8a8a8a"        # 虚线箭头颜色

def wrap(text, n):
    """按显示宽度（中文=1 字符）折行，保留显式换行。"""
    out = []
    for seg in text.split("\n"):
        cur = ""
        for ch in seg:
            if len(cur) >= n:
                out.append(cur); cur = ""
            cur += ch
        out.append(cur)
    return out

def box(ax, x0, y0, x1, y1, text, fc, ec, fs=10.5, lw=1.5, tc="#1a1a1a",
        width_chars=14, bold_title=None, style="round,pad=0.06", zorder=2):
    """圆角矩形 + 居中多行文本。bold_title 为 None 时整块居中；否则标题在上、正文在下。"""
    p = FancyBboxPatch((x0, y0), x1 - x0, y1 - y0,
                       boxstyle=style, fc=fc, ec=ec, lw=lw, zorder=zorder)
    ax.add_patch(p)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    if bold_title is None:
        ax.text(cx, cy, "\n".join(wrap(text, width_chars)), ha="center", va="center",
                fontsize=fs, color=tc, zorder=zorder + 1, linespacing=1.35)
    else:
        ax.text(cx, y1 - (y1 - y0) * 0.20, bold_title, ha="center", va="center",
                fontsize=fs + 1, color=tc, zorder=zorder + 1, fontweight="bold")
        ax.text(cx, y0 + (y1 - y0) * 0.34, "\n".join(wrap(text, width_chars)),
                ha="center", va="center", fontsize=fs, color=tc, zorder=zorder + 1,
                linespacing=1.25)

def diamond(ax, cx, cy, hw, hh, text, fc, ec, fs=9.5, tc="#1a1a1a", width_chars=10):
    pts = [(cx - hw, cy), (cx, cy + hh), (cx + hw, cy), (cx, cy - hh)]
    ax.add_patch(Polygon(pts, closed=True, fc=fc, ec=ec, lw=1.5, zorder=2))
    ax.text(cx, cy, "\n".join(wrap(text, width_chars)), ha="center", va="center",
            fontsize=fs, color=tc, zorder=3, linespacing=1.3)

def ellipse(ax, cx, cy, w, h, text, fc, ec, fs=10, tc="#1a1a1a"):
    ax.add_patch(Ellipse((cx, cy), w, h, fc=fc, ec=ec, lw=1.5, zorder=2))
    ax.text(cx, cy, text, ha="center", va="center", fontsize=fs, color=tc, zorder=3)

def arrow(ax, x0, y0, x1, y1, label=None, color=EDGE, lw=1.6, ls="-",
          fs=8.5, label_dx=0.0, label_dy=0.0, label_ha="center", zorder=3):
    a = FancyArrowPatch((x0, y0), (x1, y1), arrowstyle="-|>", mutation_scale=13,
                        color=color, lw=lw, linestyle=ls, zorder=zorder,
                        shrinkA=0, shrinkB=0)
    ax.add_patch(a)
    if label:
        ax.text((x0 + x1) / 2 + label_dx, (y0 + y1) / 2 + label_dy, label,
                fontsize=fs, color="#333333", ha=label_ha, va="center",
                zorder=zorder + 1)

def elbow(ax, pts, label=None, color=EDGE, lw=1.6, ls="-", fs=8.5,
          label_at=None, label_dx=0.0, label_dy=0.0, label_ha="center", zorder=3):
    """折线箭头：pts 为顶点序列，末段带箭头。"""
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    ax.plot(xs[:-1], ys[:-1], color=color, lw=lw, ls=ls,
            solid_capstyle="butt", zorder=zorder)
    x0, y0 = pts[-2]; x1, y1 = pts[-1]
    dx, dy = x1 - x0, y1 - y0
    L = (dx * dx + dy * dy) ** 0.5
    if L > 0:
        ux, uy = dx / L, dy / L
        ax.add_patch(FancyArrowPatch((x1 - 0.6 * ux, y1 - 0.6 * uy), (x1, y1),
                                     arrowstyle="-|>", mutation_scale=13,
                                     color=color, lw=lw, linestyle=ls,
                                     zorder=zorder + 1))
    if label:
        if label_at is not None:
            lx, ly = label_at
        else:
            lx, ly = (x0 + x1) / 2 + label_dx, (y0 + y1) / 2 + label_dy
        ax.text(lx, ly, label, fontsize=fs, color="#333333", ha=label_ha,
                va="center", zorder=zorder + 2)

def band(ax, x0, y0, x1, y1, fc, ec, ls="-", lw=1.2):
    ax.add_patch(FancyBboxPatch((x0, y0), x1 - x0, y1 - y0,
                                boxstyle="round,pad=0.04", fc=fc, ec=ec,
                                lw=lw, linestyle=ls, zorder=1))

# ══════════════════════════════════════════════════════════
fig, ax = plt.subplots(figsize=(20, 24), dpi=140)
ax.set_xlim(0, 100); ax.set_ylim(0, 104)
ax.axis("off")

# ── 标题 ──────────────────────────────────────────────────
ax.text(50, 103.1, "起源数智 · VeritasMath 数学智能体 — 系统架构总览",
        ha="center", va="center", fontsize=20, fontweight="bold", color="#12263a")
ax.text(50, 101.5, "LangGraph 多智能体编排 · 异构双路径推理 · 过程审计门 · 确定性复算季后赛 · 难度感知动态预算 · SC 共识",
        ha="center", va="center", fontsize=11.5, color="#5a6b7d")

# ── ① 平台层 ──────────────────────────────────────────────
band(ax, 1.5, 95.5, 98.5, 100, "#E7EDF8", "#3E6391")
ax.text(50, 98.1, "① 竞赛评测平台（官方）", ha="center", va="center",
        fontsize=14.5, fontweight="bold", color="#23466e")
ax.text(50, 96.5, "并发 3 · 单题 20min 硬限 · 全卷 6h · 112 题 · Judger = Intern-S 语义判分（LaTeX 等价判对）· 平台加载 user_agent.py 并以官方 client 构造 ReasoningAgent",
        ha="center", va="center", fontsize=9.5, color="#2c3e50")

# ── ② 入口层 ──────────────────────────────────────────────
band(ax, 1.5, 89, 98.5, 94, "#FDF1DE", "#B97E2E")
ax.text(50, 92.8, "② 入口层 · user_agent.py — ReasoningAgent（平台唯一入口 / 0 分防线）",
        ha="center", va="center", fontsize=13.5, fontweight="bold", color="#7d5417")
ax.text(50, 91.2, "sys.path 自举（无 __file__ 兜底）· 导入守卫 · fallback 直调后端 · solve(problem, metadata=None) 全兼容签名 · __call__/run 别名 · client 契约探测 + socket/重试调优 · trace 构建",
        ha="center", va="center", fontsize=9, color="#5c4320")
ax.text(50, 89.9, "图形后端 MathAgentGraph.run()（主用）  ←—→  直调后端 client.chat（核心导入失败时的保底）",
        ha="center", va="center", fontsize=9, color="#8a5a1d")

# 平台 → 入口
arrow(ax, 50, 95.5, 50, 94.0)
ax.text(66.5, 94.9, "from user_agent import ReasoningAgent\nagent.solve(problem, metadata)",
        fontsize=8.5, color="#333333", ha="left", va="center")

# 入口 → 主图 START
elbow(ax, [(30, 89), (30, 87.4), (4.5, 87.4), (4.5, 81.4)],
      label="create_initial_state → MathAgentGraph.run()\n注入 Deps(client · skills · mcp · 预算)",
      label_at=(30.6, 88.4), label_ha="left", fs=8.5)

# ── ③ 编排层（主图）────────────────────────────────────────
band(ax, 1.5, 30, 98.5, 87, "#FBFDFE", "#9db8d8", ls=(0, (4, 3)), lw=1.2)
ax.text(0.9, 58.5, "③ 编排层", rotation=90, ha="center", va="center",
        fontsize=11, fontweight="bold", color="#23466e")
ax.text(50, 86.7, "③ 编排层 · LangGraph 主图（langgraph_math_agent.py · 路由由 next_node 显式映射驱动 · 所有节点过 node_wrapper 超时/降级）",
        ha="center", va="center", fontsize=11.5, fontweight="bold", color="#23466e")

# 主图节点
ellipse(ax, 4.5, 80, 2.6, 1.5, "START", fc="#DCEAFB", ec="#2E5E9E", fs=8.5)
box(ax, 7, 76, 13.5, 84, "input\n题面解析\n契约锚点\n状态初始化", fc="#DCEAFB", ec="#2E5E9E", fs=10)
box(ax, 15.5, 76, 22.5, 84, "classifier\n18 领域分类 + 难度画像\n（prefill 低成本 · 顺带输出\n置信度与 question_mode）",
    fc="#DCEAFB", ec="#2E5E9E", fs=9.5, width_chars=13)
# solving 子图容器
band(ax, 25, 30.5, 56, 85.5, "#F2F9F3", "#3E7D4E", lw=1.8)
ax.text(40.5, 84.6, "solving 子图（多径扇出 + SC 共识）", ha="center", va="center",
        fontsize=12, fontweight="bold", color="#2c6e3f")
# 路由菱形
diamond(ax, 59.5, 78, 3, 3.5, "路由\nnext_node\n显式映射", fc="#FDEBD0", ec="#B9770E", fs=9)
box(ax, 64, 74, 75, 82, "playoff 确定性复算\n候选代回 · 方程残差\n枚举对照 · 实测目标\n（≤240s · 每题至多一次）",
    fc="#E9DDF1", ec="#7D3C98", fs=9, width_chars=11)
box(ax, 76.5, 74, 87.5, 82, "reconciliation 调解重算\n定向修复提示 · 换方法\n重算 · 轮次上限 2",
    fc="#D6EAF8", ec="#21618C", fs=9, width_chars=11)
box(ax, 90, 74, 99, 82, "semantic_arbiter 语义仲裁\n第三候选 reasoning_alt\nprefill 选择 JSON\n（低成本）",
    fc="#E8DAEF", ec="#6C3483", fs=9, width_chars=11)
box(ax, 66, 52, 77.5, 60, "critic 过程审计门【必经】\n契约完整性（确定性契约\n+ LLM 审计）· 关键计算\n抽核 → calc_error 强信号",
    fc="#FADBD8", ec="#B03A2E", fs=9, width_chars=11)
box(ax, 82, 52, 92, 60, "coordinator 成稿\n结构补强 · 覆盖检查\n形式对齐 · 洁净度门\n（含难题兜底）",
    fc="#D4EFDF", ec="#1E8449", fs=9, width_chars=11)
ellipse(ax, 95.5, 56, 3.0, 1.6, "END", fc="#D4EFDF", ec="#1E8449", fs=8.5)

# 主图连线
arrow(ax, 5.8, 80, 7.0, 80)
arrow(ax, 13.5, 80, 15.5, 80)
arrow(ax, 22.5, 80, 27.1, 80.6)                                   # classifier → fan_out
elbow(ax, [(50, 34.5), (56.3, 34.5), (56.3, 78), (56.5, 78)])     # 子图出口 → 路由
ax.text(57.2, 58, "validation 结果\n→ 路由裁决", fontsize=8, color="#333333",
        ha="center", va="center", rotation=90)
arrow(ax, 61.8, 79.6, 64.0, 77.0, label="mismatch·计算", fs=8,
      label_dx=0, label_dy=1.6)
elbow(ax, [(62.5, 78), (62.5, 73.4), (76.5, 73.4), (76.5, 74.0)],
      label="mismatch·可重试", label_at=(69.5, 72.3), fs=8)
elbow(ax, [(62.5, 78), (62.5, 70.4), (90, 70.4), (90, 74.0)],
      label="uncertain / 预算耗尽", label_at=(76.5, 69.3), fs=8)
elbow(ax, [(59.5, 74.5), (59.5, 56), (66, 56)],
      label="match（必经审计门）", label_at=(61.6, 62.5), fs=8, label_ha="left")
elbow(ax, [(73, 74), (73, 67.6), (90, 67.6), (90, 74)],
      label="INCONCLUSIVE", label_at=(81.5, 66.5), fs=8)
arrow(ax, 68, 74, 68, 60.2, label="A/B/BOTH 锁定", fs=8, label_dx=-1.7, label_dy=0.6)
arrow(ax, 75, 79, 76.5, 79, label="NEITHER", fs=7.5, label_dx=0, label_dy=1.0)
elbow(ax, [(80, 82), (80, 84.2), (56, 84.2)],
      label="换方法重算（定向修复）", label_at=(68, 85.2), fs=8)
elbow(ax, [(80, 74), (80, 58), (82, 58)],
      label="可直通（代码路由允许）", label_at=(80.8, 68), label_ha="left",
      color=EDGE_D, ls=(0, (4, 3)))
arrow(ax, 87.5, 78, 90, 78, label="轮次尽", fs=7.5, label_dx=0, label_dy=1.4)
arrow(ax, 90, 80, 87.5, 80, label="abstain", fs=7.5, label_dx=0, label_dy=-1.4)
elbow(ax, [(93, 74), (93, 64.4), (77.5, 64.4), (77.5, 60)],
      label="selected / 兜底", label_at=(85.3, 63.2), fs=8)
elbow(ax, [(72.5, 60), (72.5, 66.5), (76.5, 66.5), (76.5, 74)],
      label="缺口 + 可负担", label_at=(77.5, 70.2), label_ha="left", fs=8)
arrow(ax, 77.5, 56, 82, 56, label="pass", fs=8, label_dx=0, label_dy=1.4)
arrow(ax, 92, 56, 94.1, 56)

# ── solving 子图内部 ──────────────────────────────────────
diamond(ax, 30.5, 80.5, 3.3, 4, "fan_out\nFastLane 单路径\n高风险/hard 双路\n重算轮一律双路", fc="#E8F6E9", ec="#3E7D4E", fs=8.5, width_chars=11)
box(ax, 25.5, 66, 33.5, 73.5, "reasoning_agent 主路径\nLLM 推理 + skill 检索\nsympy 事前短路\n（thinking_mode=False）",
    fc="#DFF0E2", ec="#3E7D4E", fs=9, width_chars=11)
box(ax, 36.5, 66, 44.5, 73.5, "reasoning_agent 异构 B\nmethod_variant=B\n8 选 1 方法论强制切换",
    fc="#DFF0E2", ec="#3E7D4E", fs=9, width_chars=11)
box(ax, 42, 49.5, 50, 57, "python_agent\n代码生成 + 沙箱执行\n验证 · 失败修复重试",
    fc="#DFF0E2", ec="#3E7D4E", fs=9, width_chars=11)
box(ax, 34.5, 40, 50, 47.5, "cross_validator\n三路比对 · 确定性等价\n低置信复核投票\nmismatch → contradict",
    fc="#DFF0E2", ec="#3E7D4E", fs=9, width_chars=13)
box(ax, 30, 31, 50, 38.5, "sc_consensus（V4.6）\n3 视角推理 × 3 代码投票\n未 match + hard + 时间充足触发\n任通道 ≥2 一致锁定 · 否则走原链路",
    fc="#DFF0E2", ec="#3E7D4E", fs=8.5, width_chars=15)

arrow(ax, 30.5, 78.5, 30.5, 73.8, label="主路径", fs=8, label_dx=1.7, label_dy=0)
arrow(ax, 33.75, 80.5, 36.8, 73.6, label="method_variant=B", fs=8,
      label_dx=0.6, label_dy=-2.3)
elbow(ax, [(33.75, 80.5), (46.8, 80.5), (46.8, 57.4)],
      label="代码验证（sympy/枚举）", label_at=(47.8, 69), label_ha="left", fs=8)
arrow(ax, 30.5, 66, 35.4, 48.2, label="候选比对", fs=8, label_dx=-0.4, label_dy=0.8)
arrow(ax, 40.5, 66, 40.5, 47.8)
arrow(ax, 45.5, 49.5, 45.5, 47.8)
arrow(ax, 41, 40, 41, 38.8)
# ── ④ 支撑层 ─────────────────────────────────────────────
band(ax, 1.5, 9, 98.5, 28, "#F4F4F7", "#6C6C7A")
ax.text(0.9, 18, "④ 支撑层", rotation=90, ha="center", va="center",
        fontsize=11, fontweight="bold", color="#4a4a5a")
ax.text(50, 27.7, "全部 LLM 调用经 chat_with_retry / chat_prefilled（deadline 计价）· 新机制可独立关闭，关闭后图退化为基线拓扑",
        ha="center", va="center", fontsize=9.5, color="#4a4a5a")

groups = [
    (2.5, 10, 26, 27, "LLM 通道", [
        "llm_client（官方 client 透传）",
        "llm_retry：重试/退避/超时",
        "chat_prefilled 抑制 CoT（10×）",
        "thinking_mode=False 关深度思考",
        "截断续写 ≤2 轮 + 压缩重试",
        "response_normalize 全形态归一"]),
    (28, 10, 51, 27, "预算引擎", [
        "TimeBudget 难度软预算 480/840/1200",
        "硬限 1200s + 300s reserve 保底",
        "PaperPacer 全卷 6h 完成率引擎",
        "TokenBudget 分级 8192/12288/16384",
        "ConfidenceGate fast/standard/deep",
        "RetryAffordability 可负担性门"]),
    (53, 10, 76, 27, "确定性工具包", [
        "sympy_pre_solver 事前短路",
        "answer_matcher 限时代简·符号对齐",
        "modular_guard 模结构守护",
        "judge_confirm 判断双向确认",
        "counting_guard 计数枚举对照",
        "numeric_crosscheck 数值比对",
        "form_align · semantic_judge"]),
    (78, 10, 97.5, 27, "执行与知识", [
        "PythonExecutor MCP 沙箱执行",
        "静态安全扫描 + RLIMIT + 60s",
        "SkillsLoader 18 领域手册检索",
        "prompt_templates 集中提示词",
        "state 状态模型 + node_wrapper",
        "answer_cleanliness 洁净度门"]),
]
for gx0, gy0, gx1, gy1, gtitle, items in groups:
    box(ax, gx0, gy0, gx1, gy1, "\n".join(items), fc="#EDEDF2", ec="#8E8E9E",
        fs=8.5, width_chars=18, bold_title=gtitle, lw=1.2)

# 支撑层 → 编排层（虚线依赖注入）
elbow(ax, [(58, 28.3), (58, 30.4)], color=EDGE_D, ls=(0, (4, 3)), lw=1.4)
ax.text(58.6, 29.3, "Deps 注入", fontsize=8, color="#666666", ha="left", va="center")
elbow(ax, [(20, 28.3), (20, 30.4)], color=EDGE_D, ls=(0, (4, 3)), lw=1.4)

# ── ⑤ 本地工具链 ─────────────────────────────────────────
band(ax, 1.5, 1.5, 98.5, 7, "#F9EBEA", "#943126")
ax.text(0.9, 3.8, "⑤ 本地工具链·离线", rotation=90,
        ha="center", va="center", fontsize=11, fontweight="bold", color="#7b2d20")
ax.text(50, 6.0, "⑤ 本地工具链 local_test/ · benchmark/ · reports/（离线 · 不入评测仓库）",
        ha="center", va="center", fontsize=12, fontweight="bold", color="#7b2d20")
ax.text(50, 3.6, "main.py 批量求解（asyncio 并发可配）· run_tests.py 断点续跑 · grade_llm.py LLM 判分 + semantic_judge 交叉复核 · server.py 监控面板 · tests/ 206 项离线回归 · 官方双槽 A/B 实验脚本",
        ha="center", va="center", fontsize=8.8, color="#6e3a2e")

# ── QA：文本包围盒两两碰撞自检 ─────────────────────────────
fig.canvas.draw()
_inv = ax.transData.inverted()
_boxes = []
for _t in ax.texts:
    _bb = _t.get_window_extent()
    _b = tuple(_inv.transform((_bb.x0, _bb.y0))) + tuple(_inv.transform((_bb.x1, _bb.y1)))
    _boxes.append((_t.get_text().replace("\n", " ")[:24], _b))
_qa = []
for i in range(len(_boxes)):
    for j in range(i + 1, len(_boxes)):
        n1, (x0, y0, x1, y1) = _boxes[i]
        n2, (u0, v0, u1, v1) = _boxes[j]
        ix = max(0, min(x1, u1) - max(x0, u0))
        iy = max(0, min(y1, v1) - max(y0, v0))
        a1 = (x1 - x0) * (y1 - y0); a2 = (u1 - u0) * (v1 - v0)
        ov = ix * iy / max(1e-6, min(a1, a2))
        if ov > 0.10:
            _qa.append(f"[OVERLAP {ov:.0%}] '{n1}' box=({x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f})"
                       f" <-> '{n2}' box=({u0:.1f},{v0:.1f},{u1:.1f},{v1:.1f})")
with open("docs/_qa_report.txt", "w", encoding="utf-8") as _f:
    for _n, (x0, y0, x1, y1) in _boxes:
        _f.write(f"{_n!r}: ({x0:.1f}, {y0:.1f}, {x1:.1f}, {y1:.1f})\n")
    _f.write("\n" + ("\n".join(_qa) if _qa else "[QA] 无显著文本重叠\n"))
print(("QA issues: %d (see docs/_qa_report.txt)" % len(_qa)))

fig.savefig("docs/architecture.png", bbox_inches="tight", facecolor="white")
print("saved: docs/architecture.png  (font:", chosen, ")")
