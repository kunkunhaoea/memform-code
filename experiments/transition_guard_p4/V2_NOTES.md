# TransitionGuard v2：修复、预算与 16 条复测

本版标识：`transition-guard-p4-v2`。2026-11-16 完成代码与离线验证，**尚未运行新的真实模型实验**。
原 v1 的 16 条真实结果保留在 `results/transition_guard_p4/20261116T071841_937962Z_live/`，不改写、不混入 v2。

## 已实现的修改

1. **不再首个缺口即返回。** 完成预算内所有 witness 的反事实验证，再统一修复一次。修复和最终复核读取所有实际选中的 witness；最终检查必须逐一给出结论并引用全部 witness，不允许漏掉第二条。
2. **可核对的局部编辑。** 修复器输出最多四个 `old → new` 替换及逐项引用。程序要求原文片段唯一、互不重叠、有真实引用，且修复后满足字数限制。没有审查到的自由重写文本不会被提交。
3. **分支冲突与未知状态检查。** 最终审核检查条件重叠、兜底绕过前提、未知被当成否定、单次成功被推广为许可，以及不受支持的程序步骤。任何缺项、未解决冲突或 witness 不一致均拒绝提交修复。新增或放宽许可的编辑至少需要两条不同记录引用；这只是必要条件，不是证据独立性或语义正确性的证明。
4. **减少审核锚定。** 最终审核读取原文、修复、观察和修改位置，不读取 verifier 的结论、修复理由或修复器声明的效果类别；由它重新判定每个修改的效果，代码核对两者是否一致。仍使用同模型的独立调用，不能声称统计独立或绝对可靠。
5. **缺口与重建成效分开记录。** 缺口必须定位候选记忆中的真实原文，并有已选经验及 witness 引用。若新证据直接揭示缺口，但 Updater 未改善，仍可进入修复；日志另记 `counterfactual_improved`。模型声称实质变化但重建文本完全相同时，仍拒绝该自相矛盾的判定。
6. **严格区分格式失败和无证据。** 缺失/畸形的选择结果标记为 `inconclusive`，不再被记录成正常的 `no_witness`。
7. **版本隔离。** 新守卫使用 v2 prompt/cache namespace；续跑同时核对算法版本、源码指纹、读取预算和输出上限。v1 结果不能作为 v2 的已完成防御结果续用。原 Agent、Updater、题目、评分和默认关闭的 Hook 保持原协议。

## token 与上下文控制

新增的本地 BM25 风格检索只使用经验文本、候选记忆和 decision variable，无 API 调用、无答案/标签/隐藏策略。先从遗漏池取候选短名单，再由 LLM 定向选择最多两个 witness。

| 参数 | 默认 | 含义 |
|---|---:|---|
| `--selection-max-records` | 8 | LLM 选择器最多读取的遗漏记录数 |
| `--selection-max-chars` | 6000 | 短名单序列化后的字符总量，包括记录包装 |
| `--guard-max-input-chars` | 24000 | 单次守卫 system + user 的字符硬上限；CF Updater 输入也做保守预算检查 |
| `--guard-max-output-tokens` | 0 | 守卫 API 输出上限；0 使用服务默认，Agent/Updater 不受此参数影响 |

经验以完整原文送入，不截断。过长记录会被跳过并统计；全部记录都无法放入检索预算时返回 `retrieval_budget_exhausted`。守卫请求/CF 输入无法放入预算时返回 `context_budget_exceeded`。这两种情况均暂缓更新，使用旧记忆（本 P4 旧状态为空），不声明已检测到攻击或完成语义修复。

验证 Prompt 中重复的 counterfactual records 全文改为 ID 列表，正文只在 selected + witnesses 中提供一次。修复不再重复发送全部反事实文本，只发送定位后的缺口。要求输出短理由和短的原文引用。

**边界：** 本地检索及 adapter 仍扫描历史，CPU/内存开销没有变成次线性；词项检索可能漏掉关键语义证据。记录条数很多时，传给守卫模型的文本有上限，但不能据此宣称整体检索质量或相对全池重建的成本优势已经验证。字符数不是精确 token 数。

输出上限可能包含供应商的推理 tokens，过小会截断 JSON。下面使用 8192 作为可调上限；若 `guard_json_parse_failures` 增多，应检查原始输出，再提高上限或使用 0。不能把截断后更多 defer 带来的 ASR 下降解释为修复能力提升。

完成全部 witness 检查会增加原来提前停止案例的调用；本版不保证 16 条测试的总 tokens 一定比 v1 少。若要减少重建/验证次数，可单独使用下文的 `k=2, max_rebuilds=1` 设置。

## 先做离线验证

```bash
cd /home/lukun/projects/paper_workflow/memform-code
conda activate memform
python -B -m unittest discover -s tests -v
```

当前 48 项测试通过，含 1002 条遗漏经验的读取上限、第二条证据覆盖、局部修改、错误放行拒绝、输出上限/缓存身份、固定评分与续跑等回归。
原 16 条清单另已做两种预算配置的 mock 流程检查：每种均 80 行完整、零 API 请求。mock 只验证流程，不证明防御效果。

## 原 16 条：保留原重建预算的完整五组复测

```bash
python -B -m experiments.transition_guard_p4.run_experiment \
  --mode live --model deepseek-flash \
  --manifest results/transition_guard_p4/20261116T071841_937962Z_live/selected_cases.json \
  --seed 42 --strategy swap --k 1 --max-rebuilds 2 \
  --selection-max-records 8 --selection-max-chars 6000 \
  --guard-max-input-chars 24000 --guard-max-output-tokens 8192 \
  --timeout 300 --cache .cache/transition_guard_p4.jsonl
```

默认五组 B0–B4，共 80 行；每行包含 clean / poisoned 更新和原答题流程。新建时间戳结果目录。
模型凭据仍由现有 `.env` 读取；无需重配。原始 Agent/Updater 完全相同的请求可以命中原缓存，v2 守卫不会命中 v1 守卫缓存。
`--manifest` 仅固定样本，**不要把这里的 v1 目录改成 `--resume-from`**；那样会被版本检查拒绝。

## 较省调用的探索设置：两条证据一起重建一次

```bash
python -B -m experiments.transition_guard_p4.run_experiment \
  --mode live --model deepseek-flash --groups B0 B3 B4 \
  --manifest results/transition_guard_p4/20261116T071841_937962Z_live/selected_cases.json \
  --seed 42 --strategy swap --k 2 --max-rebuilds 1 \
  --selection-max-records 8 --selection-max-chars 6000 \
  --guard-max-input-chars 24000 --guard-max-output-tokens 8192 \
  --timeout 300 --cache .cache/transition_guard_p4.jsonl
```

共 48 行，保留无防御、随机、定向三组。省略 B1/B2 只减少本次比较范围；若需要五组，去掉 `--groups B0 B3 B4`。
单次 B4 审查的额外逻辑调用上界由 7 次降为 5 次：选择一次，重建一次，验证一次，必要时修复/复核各一次。原 Updater 的整合输入仍为 4 条，但同时替换两条，和逐条反事实并非相同实验；应单独报告，不能把差异全部归因于源码修复。

两种 live 命令均需用户自行执行，本次开发没有运行。

## 续跑与结果检查

若 v2 中断，把相同命令中的 `--manifest ...` 换成 `--resume-from <本次v2结果目录>`，其他预算、组别、模型和 cache 参数保持一致。不要同时写同一缓存。
只试少量前缀可额外加 `--limit 2`；随后跑全 16 条时新建运行，复用缓存即可。前缀与全量的 active manifest 不同，不能直接续跑成全量。

查看 `summary.json`：

- `complete`、`eligible_count`、固定 ASR / Recovery / 全样本 clean accuracy。
- 每组 `transitions.by_condition` 中的 `witnesses_tested` 与 `witnesses_selected`，以及修复和 defer 数。
- `selection_scan_count`（模型读取数）、`local_records_scanned`（本地扫描数）、`retrieval_payload_chars`、`retrieval_excluded_count`。
- `guard_json_parse_failures`、`selection_format_failures`、`context_budget_defers`、`oversized_records_skipped`。
- `cost_by_role`、`new_run_cost` 及缓存命中；完整成功路径逻辑调用与实际 API tokens 应分别比较。

这 16 条已用于诊断和开发。得到可运行的新结果后，确认性评价仍需固定未查看且 family 不重叠的样本。

尚未实现：no-op / removal-only 因果控制、defer-only 消融、多模型验证、连续 P5 更新、向量索引。规则语义仍依赖模型审核；当前改动没有保证消除全部误批准或过度保守回退。
