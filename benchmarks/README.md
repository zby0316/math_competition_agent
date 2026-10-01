# 公共数学诊断集

## U-MATH 纯文本诊断集

`umath_text24.jsonl` 从 [U-MATH](https://huggingface.co/datasets/toloka/u-math) 的固定版本 `7210f97b3f21122c3a90126935e966ca7d8951b6` 中选出 24 题。六个主题各四题；只选 `has_image=false` 且题目与答案非空的记录。每个主题按 `SHA-256("math-agent-umath-text24-v1:" + uuid)` 排序后取前四题。原始 Parquet 的 SHA-256 为 `9cc4042a4b61a148f4b28c02ffb3bfc11114937e31d9d6ead190f18e0f9da074`。

`umath_pilot6.jsonl` 取上述 24 题中每个主题的第一题（`idx` 0、4、8、12、16、20）；基线运行与逐题复核见 [pilot6_report.md](pilot6_report.md)。

剩余 18 题的逐题运行与人工核对见 [remaining18_report.md](remaining18_report.md)。

题目保留原始英文，不翻译。`source_id` 可追溯到原题；`source_subject` 仅供分析，`main.py` 和 `eval_local.py` 都不会把它传入解题入口。参考答案存于 `answer`，不得用于构造模型提示或领域知识库。

## 重建数据

仅重建数据需要 `pyarrow`：

```bash
pip install pyarrow
python benchmarks/build_umath_text24.py
```

若已下载原始 Parquet，可用 `--source-parquet 路径` 离线重建；脚本始终校验文件哈希。重建过程不调用模型 API。

## 运行与解读

安装项目依赖、配置 `.env` 后，可用本地评测入口做初步比较：

```bash
python eval_local.py --dataset benchmarks/umath_text24.jsonl --out eval_results_版本号_grader_v2.json --concurrency 1
```

每次切换解题代码或判分器版本都要更换 `--out` 文件名；`eval_local.py` 会跳过结果文件中已有的题号，旧版误判不会自动重算。上述命令**会调用付费或限额的模型 API**。`eval_local.py` 保存完整的 `predicted` / `expected`，但只保存 trace 条数；要查看完整 trace，可另用 `main.py --input_file benchmarks/umath_text24.jsonl --output_dir outputs/umath_版本号` 运行（同样会调用模型 API）。本地判分采用三态：确定等价为“对”，确定数值或完整字段不等价为“错”，复杂表达式与证明题为“待复核”。汇总会同时给出自动判定覆盖率；不能把覆盖内正确率当作整套题的正确率。自然语言与多问仍需逐题核查。

这个小样本只用于同一设置下比较代码版本，不能换算成官方成绩 `69.64/100`，也不能覆盖证明题或项目的全部 18 个数学领域。

数据由 Toloka.ai 发布，使用 MIT 许可；许可文本见 [U-MATH-LICENSE](U-MATH-LICENSE)。

## UGMathBench 跨领域诊断集

为进一步定位高难度和跨领域失分点，已准备 [UGMathBench 26 题诊断集](ugmath_hard26_report.md)；题目文件为 [ugmath_hard26.jsonl](ugmath_hard26.jsonl)，重建脚本为 [build_ugmath_hard26.py](build_ugmath_hard26.py)。

首批 6 题的逐题运行与人工复核见 [ugmath_pilot6_report.md](ugmath_pilot6_report.md)。

修复两项离线问题后的第二轮六道 5 级题结果见 [ugmath_level5_report.md](ugmath_level5_report.md)。

## 拓扑、测度与泛函分析证明题

新增 [proof_gap12.jsonl](proof_gap12.jsonl)：三个此前缺少直接证明诊断的领域各四题，共 12 题。题面、参考证明及人工清单由本项目独立编写，围绕标准定理的应用、必要条件和反例；不是官方题或外部数据集抽样。

构建脚本 [build_proof_gap12.py](build_proof_gap12.py) 不联网、不调用模型，同时生成不含答案的六题试跑输入和人工复核模板。来源及验收方案见 [证明题说明](拓扑测度泛函证明题说明.md)。[首批六题](证明题首批六题评测报告.md)为 3 通过、1 不通过、2 需修正；[修复后剩余六题](证明问题修复与剩余六题评测报告.md)为 3 通过、3 需修正，失败题另复跑一次仍需修正。12 道不同题已运行，两批版本不同；不能以结论文本匹配自动判证明正确。
