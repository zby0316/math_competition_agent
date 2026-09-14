# A/B 基线分析（09-14 夜，18 题信号集）

信号集 = `_tmp_classroom_ab.py` 的 MISSES(13) + GUARDS(5)，用于对比四个单变量臂。
基线 = main 副本未改动代码（f3d8525），2026-09-14 21:47→23:0x，并发 3，**17/18 全错**。

## 逐题（idx | 交付来源 | 交叉验证 | Python 分支 | 交付长度 | 判分理由）

```
  0 | compressed_prefill | mismatch_arbitrating  | True/inconclusive | 368 | 解析/格式
  3 | fallback_line      | uncertain_arbitrating | True/inconclusive |   9 | 数值/语义不同
  4 | compressed_prefill | mismatch_arbitrating  | True/support      | 102 | 数值/语义不同
  7 | fallback_line      | uncertain_arbitrating | False/inconclusive| 112 | 解析/格式
  8 | fallback_line      | uncertain_arbitrating | True/inconclusive |  11 | 数值/语义不同
 10 | quick_conclusion   | match                 | True/support      |   6 | 数值/语义不同
 13 | compressed_prefill | mismatch_arbitrating  | True/inconclusive |   7 | 数值/语义不同
 15 | quick_conclusion   | mismatch_arbitrating  | True/contradict   |  54 | 解析/格式
 16 | fallback_line      | uncertain_arbitrating | True/inconclusive |   7 | 数值/语义不同
 17 | ?                  | uncertain_arbitrating | True/inconclusive |   6 | 数值/语义不同
 18 | salvaged_prose     | mismatch_arbitrating  | True/support      |  15 | 数值/语义不同
 23 | fallback_line      | uncertain_arbitrating | True/support      |   8 | 数值/语义不同
 24 | ?                  | uncertain_arbitrating | False/inconclusive|   9 | 数值/语义不同
 43 | ?                  | mismatch_arbitrating  | True/support      |  75 | 数值/语义不同
 48 | ?                  | uncertain_arbitrating | True/inconclusive |   6 | 数值/语义不同
 50 | compressed_prefill | mismatch_arbitrating  | True/support      |  68 | 数值/语义不同
 69 | compressed_prefill | uncertain_arbitrating | True/inconclusive | 317 | 数值/语义不同
```

## 结论（供下一轮迭代选点）

1. **交付来源分布**：`fallback_line` 5 题 + `salvaged_prose` 1 题 = **6/17（35%）的交付答案
   来自文本搜救层**（生成端两次尝试都没写出四章节/结论速览）。这是"交付质量"问题的规模。
2. **失败性质**：14/17 是"数值/语义不同"（模型确实算错，多为差 1~6 的计数题），
   仅 3 题是"解析/格式"。**所以提分主战场是推理质量与时间预算，而不是交付格式**——
   但搜救层交付（35%）里若有非答案句子，属于"白丢"，narration_gate 臂即测此项。
3. **Python 分支 coverage 可疑**：6 题 `True/support`（代码自称支持推理候选）最终仍错。
   说明"support"信号强度有限（生成代码可能只是部分验证）。
4. **本地判分偏严**：idx 15 交付 "仅正整数 n=2 满足条件"（期望 2）被判"解析/格式"错——
   官方模型判分大概率判对。本地 A/B 的数字只能当**下界**看。
5. 墙钟：单题 559~1102s，多题 `att=2`（两轮尝试）——说明截断是常态而非例外。

## 四个单变量臂（23:08 启动，并发 1，各 18 题）

| 臂 | 开关 | 产物 | 假设 |
|---|---|---|---|
| kb | `kb_sanitize=True` | `_ab_kb.json` | 净化知识库答案锚点；合规优先，分数期望持平 |
| ev | `evidence_select=True` | `_ab_ev.json` | 证据评分替掉焦着时的默认偏好 |
| evr | `evidence_select=True` + `recheck.enabled=True` | `_ab_evr.json` | 盲目独立复核 + 证据评分的联合效应 |
| narr | `narration_gate=True` | `_ab_narr.json` | 拒交"过程自述"类非答案 |

判定：`python _tmp_ab_report.py --base _ab_base.json --arm kb=_ab_kb.json --arm ev=_ab_ev.json --arm evr=_ab_evr.json --arm narr=_ab_narr.json`
门禁：回归集（本次基线答对题）0 回归 且 机会集净增 ≥ +2。
注意：本次基线在信号集上 0/18，**回归集为空 → 门禁实际只剩"净增 ≥2"**，证据强度有限，
需结合诊断（rck=/ev=）与人工复核预测文本来判断。
