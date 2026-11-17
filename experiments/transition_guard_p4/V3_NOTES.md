# TransitionGuard：当前v5，以下保留v3/v4历史使用说明

**当前源码为 `transition-guard-p4-v5`，审核协议为 `repair-audit-v2`。** 最新完整真实结果是 `20261116T155512_434220Z_live` 的v5混合思考16条五组。现新增B5 Always-defer及全样本配对统计；B5和144条六组只有离线验证，不能当作已有防御效果。最新结果、协议、命令和工作边界以[根目录handoff.md](../../handoff.md)为准。

v5新增有限的观察引用句首大小写规范化（保留raw与修正日志）、独立selected_evidence_review、覆盖整份策略的policy_units逐分支支持检查，以及同一不确定性原文的嵌套引用定位。每个通用许可，包括保留的原有fallback，都需两条不同的正向支持记录及语义支持；单次成功可以保留为描述性观察。没有增加审核调用或自动重试，但更严格的许可审核不保证回退率下降。

守卫Prompt、版本、缓存namespace和源码指纹已更新；原Agent/Updater同请求缓存可合法复用。不要resume旧v4结果，也不要清空原缓存。默认思考仍为Agent=on、原始/CF/B2 Updater=on、guard=off。v5原18项回归与新增8项B5回归均通过，全套119 tests通过；144条六组mock和validate均为零API，不是正式防御结果。B5扩展未改变既有v5守卫Prompt或缓存namespace。

**下面内容是历史说明。** 其中旧版本状态、旧全局审核字段或“尚无混合模式结果”等描述不代表当前状态；命令参数仍可参考，完整最新命令见handoff第8节。

## 固定工作节奏

用户要求：先做离线验证，真实效果先看 B4；B4 稳定后再运行少量多组比较，最后才考虑全量。不要每修一个问题就自动跑16条五组，或自动扩大实验。

实验入口默认已改为 `--groups B4`。每条仍分别审查 clean / poisoned 记忆并运行原答题入口；B4 单独运行仍需要多次模型调用。

用户本轮明确要求提供原16条的B0–B4命令（由用户执行）：

```bash
python -B -m experiments.transition_guard_p4.run_experiment \
  --mode live --groups B0 B1 B2 B3 B4 \
  --manifest results/transition_guard_p4/20261116T071841_937962Z_live/selected_cases.json \
  --seed 42 --agent-thinking on --memory-thinking on --guard-thinking off \
  --strategy swap --k 1 --max-rebuilds 2 \
  --guard-max-output-tokens 8192 --timeout 300 \
  --cache .cache/transition_guard_p4.jsonl
```

继续读取当前.env的地址/模型/密钥，仍共用一个端点。不要带旧的`--thinking off`来替代上述角色开关；也不要续跑旧全非思考结果。旧统一开关`--thinking on/off/default`仍支持，逐角色显式设置优先覆盖它。v4本轮已修复审核协议与机械拒绝，真实语义审核质量与提交率仍需新实验确认。

## 本次修复

- **效果类别只是描述。** 不再要求修复器和复核器的 `replace/expand` 等名字完全相同。最终由独立复核器判断 `introduces_permission`；新增通用动作许可仍需至少两条真实记录引用及语义支持。类别不同单独记录，不直接拒绝。
- **历史冲突与策略冲突分开。** `evidence_conflicts` 可以存在，但必须有修复记忆中的真实不确定性原文引用，且策略没有授权互相冲突的动作。`policy_conflicts` 必须为空，`policy_conflict_free` 必须为true；未知状态、引用、witness覆盖等检查保留。此修改不保证模型语义审核永不误判。
- **组合后超长允许一次缩短。** 初次修复提示完整记忆的净字数预算。仅当片段替换合法、组合结果超字数时，默认最多增加一次修复调用，之后再对最终文本做完整复核。不会直接截断，不做无限重试。`--repair-length-retries 0` 可关闭额外缩短调用。
- **供应商输出限制。** 在真实 HTTP 序列化边界修正字段：DeepSeek官方用 `max_tokens`，阿里云用 `max_completion_tokens`，只发送一个限制字段。其他兼容接口默认后者，可用 `--guard-output-token-field` 显式覆盖。无输出上限的原Agent/Updater调用与缓存键保持原协议；有上限的缓存键包含实际字段。
- **生成元信息。** 缓存和调用日志新增 `finish_reason`、`reasoning_tokens`、请求上限/字段以及超限标志。达到长度限制或用量异常超限的守卫响应不能批准修复，即使正文恰好能解析为JSON。缺失的历史reasoning分项不伪造为已知0。

输出限制依据：[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)、[阿里云兼容参数](https://help.aliyun.com/en/model-studio/qwen-api-via-openai-chat-completions)。阿里云文档说明总输出含思考和答案，实际值可能与上限相差最多10 tokens；诊断按这个容差判断。参数映射已离线验证，**两家服务器是否实际遵守仍需各自的一次live探测确认**。

## 第一步：零 API 的实际请求体检查

```bash
conda activate memform
cd /home/lukun/projects/paper_workflow/memform-code
python -B -m experiments.transition_guard_p4.check_output_limit
```

使用真正的 LangChain/OpenAI SDK，只将传输替换成本地 `httpx.MockTransport`。默认检查两家，输出应分别包含 `max_tokens: 64` 和 `max_completion_tokens: 64`，`actual_api_calls: 0`。
这证明字段发出正确，不证明真实服务端执行上限。

## 第二步：每个平台只发一次小请求确认限制

运行时选择平台，无需来回修改 `.env`：

```bash
python -B -m experiments.transition_guard_p4.check_output_limit \
  --mode live --max-output-tokens 64 --ask-key
```

随后显示 `1. DeepSeek 官方`、`2. 阿里云 DeepSeek`，输入1或2，再隐藏输入对应平台密钥。1固定官方地址和文档API ID `deepseek-flash`；2使用 `ALIYUN_BASE_URL` 或当前 `.env` 的阿里云地址，模型默认 `deepseek-v4.1-flash-1017`。若未配置阿里云地址，会提示输入工作空间地址。不会继承另一平台的模型或密钥，也不改 `.env`。

可以用 `--provider 1` / `--provider 2` 跳过菜单；用 `--provider env` 明确沿用当前 `.env` 的完整地址与模型组合。显式 `--model` 始终可覆盖所选平台默认模型，但须自行确认该API ID可用。若希望在自动化中使用本地密钥，去掉 `--ask-key`；仅当地址匹配时才复用 `OPENAI_API_KEY`，也支持分别配置 `DEEPSEEK_API_KEY` / `ALIYUN_API_KEY`。菜单模式总是重新隐藏输入该平台密钥，不保存或打印。

用户已实测阿里云：上限64、实际输出64、思考20、finish_reason=length，验证成功。官方服务端上限仍待实测；不要重复测试已通过的阿里云。
请求只要求列出1到100，上限64，客户端重试0；不会自动再试一次。输出显示实际参数、输入/输出tokens、推理分项、finish_reason。不要要求服务生成无限长内容来测试限制。

成功标准：`status: verified_binding_limit`，实际输出不超预算（阿里云按文档容差），且 `finish_reason: length`。此时正文为空也可能是正常的思考阶段被截断；这不是答题效果测试。
若没有触及上限，状态为 `inconclusive_limit_not_reached`，不能声称验证成功；若明显超限则停止扩大实验并检查接口兼容性。

无需修改当前 `.env`，单独检查官方接口：

```bash
python -B -m experiments.transition_guard_p4.check_output_limit \
  --mode live --max-output-tokens 64 --ask-key \
  --base-url https://api.deepseek.com --model deepseek-flash
```

阿里云使用当前工作空间 `.env` 配置，或显式传入它的 `--base-url` 和小写 `--model deepseek-v4.1-flash-1017`。两家密钥不能互换。
可额外选择 `--thinking off` 或 `--thinking on` 检查相应模式；每执行一次命令都会新增一次请求。本次未执行任何live探测。

## 第三步：B4少量诊断，再扩大到16条B4

当前 `.env` 接口，先跑固定清单前两条：

```bash
python -B -m experiments.transition_guard_p4.run_experiment \
  --mode live --groups B4 \
  --agent-thinking on --memory-thinking on --guard-thinking off \
  --manifest results/transition_guard_p4/20261116T071841_937962Z_live/selected_cases.json \
  --limit 2 --seed 42 --strategy swap --k 1 --max-rebuilds 2 \
  --guard-max-output-tokens 8192 --timeout 300 \
  --cache .cache/transition_guard_p4.jsonl
```

模型和endpoint由现有 `.env` 读取。若已改用阿里云，不要在命令里保留 `--model deepseek-flash`。
若仅修复后少量复测，不需要增加B1/B2/B3。删除 `--limit 2` 可新建一次16条B4运行；前缀和全16条active集合不同，不能直接通过 `--resume-from` 扩容，但同请求缓存可以复用。

**没有同接口B0时是诊断模式。** `complete` 只表示请求组结果完整，`diagnostic_only=true`；正常/投毒任务准确率、修复/defer数及成本可看，固定ASR/Recovery为null，不能将空eligible集合当成0%ASR。

## 复用已有同接口B0，不重新执行其他组

建立同接口、同模型、相同Agent与Updater思考模式的B0后，可用 `--baseline-from <该B0结果目录>` 复用。此前v1/v2属于平台默认思考设置，不能自动视为显式on的混合模式B0；20261116T131416只有B4没有B0，全非思考B0也不能用于当前配置。

它只读取已有B0行到 `baseline_results.jsonl`，不执行B0、不复制历史调用开销到本轮账单。校验endpoint、模型、Agent与Updater模式、温度、字数、冻结数据及cache；防御思考模式本身不影响B0复用。切到阿里云会拒绝复用官方B0。原候选记忆还要与该B0精确匹配。

同供应商以后只需保存一次有效B0；切接口后如需正式固定ASR，须先在该接口建立自己的B0。用户可在准备好正式比较时显式运行 `--groups B0 B4`，无需直接跑五组。

v3同配置中断可正常 `--resume-from <v3目录>`；源码、预算、思考强度或样本改变则新建运行。不要对v1/v2目录直接续跑v3。

## 成本控制与结果解读

- 当前防御默认关闭思考，不传 `--guard-reasoning-effort`。该参数仅在实际guard模式为on/default时可用；Agent/Updater开启思考不改变此限制。
- 8192上限真正生效后可能截断原来很长的推理。关注 `guard_truncated_responses`、`guard_output_limit_violations`、`length_limited_responses`，避免把截断和语义拒绝混为一谈。
- `--k 2 --max-rebuilds 1` 仍是另一种更少重建/验证调用的探索设置；它与逐条替换不同，单独比较，不作为本次三项修复的唯一验证。
- 关注全样本clean准确率、修复恢复与defer恢复的拆分，不只看ASR。v2虽然固定ASR为0，但29/32次defer，不能视作高质量记忆修复。
- 本轮只跑便宜的离线回归。不要为了验证代码接线自动做真实16条五组或全P4。
