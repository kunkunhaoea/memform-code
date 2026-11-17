# MemForm + TransitionGuard：Conda 环境配置与测试指南

**当前 v3：优先离线检查、B4少量诊断，再决定是否扩大。默认已只跑 B4；官方/阿里云输出限制和最新命令以 [V3_NOTES.md](experiments/transition_guard_p4/V3_NOTES.md) 为准。**

**v2 更新（2026-11-16）：环境和 API 配置保持不变。新版已完成源码与离线验证；16 条复测命令、上下文预算及 v1/v2 隔离说明见 [V2_NOTES.md](experiments/transition_guard_p4/V2_NOTES.md)。本文后面的原始算法说明和真实结果属于 v1。**

项目位置：`/home/lukun/projects/paper_workflow/memform-code`。

本文从尚未配置 MemForm 的状态开始，依次完成 Conda 环境、依赖、API、离线验证和真实实验。
以下命令均在 Linux / Bash 下执行。标为 `YOUR_MODEL_ID` 的内容必须替换为你的 API 服务支持的真实模型名称。

## 1. 本次代码修改

本轮实现仅覆盖 **P4 Selection Poisoning、MCQ、LangChain**。

| 文件 | 修改或新增内容 |
|---|---|
| `memform/freeform/__init__.py` | 将未发布的 `freeform.build` 改为按需导入，修复原 MCQ 入口启动失败；保留 Free-form Evaluator 的正常导入 |
| `memform/eval/core.py` | 增加默认关闭的后整合 Hook；实验可关闭冗余机制对照；补充 token usage 及缓存 usage 元数据 |
| `memform/defenses/transition_guard/interface.py` | 定义 Evidence、MemoryTransition、UpdateContext 和三个提交状态 |
| `memform/defenses/transition_guard/adapter.py` | 从真实 P4 数据提取白名单信息、匿名化证据 ID，提供原 Memory Updater 的回调 |
| `memform/defenses/transition_guard/guard.py` | 实现随机/定向证据选择、swap/augment、反事实验证、证据约束修复和支持复核 |
| `memform/defenses/transition_guard/__init__.py` | 导出守卫与接口 |
| `experiments/transition_guard_p4/run_experiment.py` | 五组统一入口、固定抽样、逐样本结果、独立输出目录 |
| `experiments/transition_guard_p4/runtime.py` | 复用原 CachedChatModel；记录逐调用成本；提供明确标注的 mock backend |
| `experiments/transition_guard_p4/analyze_results.py` | 复用原固定 eligibility 评分，生成比较、统计和案例分析 |
| `experiments/transition_guard_p4/README.md` | 详细设计、协议、结果和研究限制 |
| `tests/test_transition_guard.py` | 32 项离线单元、集成、续跑及回归测试 |

原始候选记忆和反事实记忆都使用 MemForm 原来的 `build_batch_memory()` 和 Prompt。
下游继续使用原 Agent、MCQ Prompt、答案解析和评分。没有修改冻结数据，也没有新增训练过程。
P3 `count_matched_clean` 执行缺口和原 CLI 中未发布构建命令的帮助残留，本轮没有修改。

```text
原 P4 被保留的经验
  → 原 Memory Updater
  → 候选记忆
  → TransitionGuard 验证 / 必要时修复
  → commit_original / commit_repaired / defer
  → 原 Agent 执行原 MCQ 任务
  → 原评分与固定 B0 eligibility
```

`defer` 会使用旧记忆；当前单次 batch 场景下为原工程的空策略文本，仍继续答题。
不会把暂缓记忆提交直接算作成功防御。

## 2. 创建 Conda 环境

本次检查时系统已有 Conda，但默认 Python 是 3.14.8。
建议为本项目单独创建 Python 3.11 环境；这是环境配置选择，并非已经验证过的完整依赖锁定组合。
本项目声明的最低版本是 Python 3.10。

```bash
cd /home/lukun/projects/paper_workflow/memform-code

conda create -n memform python=3.11 pip -y
conda activate memform

python --version
python -c "import sys; print(sys.executable)"
```

期望 Python 显示 `3.11.x`，解释器路径中包含 `/envs/memform/`。
此项目通过远程 LLM API 运行，本轮不需要本地 GPU、CUDA、模型权重或 PyTorch。

如果 `conda activate` 提示 shell 未初始化，可先在当前终端执行：

```bash
source /home/lukun/miniconda3/etc/profile.d/conda.sh
conda activate memform
```

该路径对应本机观察到的 Miniconda 安装位置；如果你另行安装了 Conda，应替换为自己的路径。

## 3. 安装原 MemForm 及依赖

确认终端已经激活 `memform` 环境，然后执行：

```bash
cd /home/lukun/projects/paper_workflow/memform-code

python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .
python -m pip check
```

`-e .` 按当前仓库的 `pyproject.toml` 安装项目与全部原有依赖，包括 LangChain、LlamaIndex、
OpenAI SDK、Pydantic、python-dotenv、tiktoken。无需再重复安装 `requirements.txt`。
`python -m pip` 可确保依赖安装到当前 Python 所属环境。

检查关键模块是否能导入：

```bash
python -B - <<'PY'
import memform.common
import memform.eval.core
import memform.eval.llamaindex
import memform.freeform.evaluator
from langchain_openai import ChatOpenAI
from memform.defenses.transition_guard import TransitionGuard
print("MemForm / LangChain / TransitionGuard imports OK")
PY
```

这一步只检查导入，不调用 API。

## 4. 先运行不需要 API 的测试

### 4.1 验证原 MemForm P4 入口

```bash
python -B -m memform run-langchain \
  --cases outputs/mcq_item_audit/accepted_cases.jsonl \
  --interfaces P4 \
  --phase learned \
  --max-cases 16 \
  --validate-only
```

期望输出 `cases: 16`、`invalid_cases: 0`。
`--validate-only` 不调用模型，不能作为 API 已连通的证明。

### 4.2 运行离线单元与集成测试

```bash
python -B -m unittest discover -s tests -v
```

当前测试集预期为 `Ran 32 tests`、`OK`，不需要安装 pytest，也不会调用真实 API。

### 4.3 在真实 P4 数据上跑五组 mock

```bash
python -B -m experiments.transition_guard_p4.run_experiment \
  --mode mock \
  --sample-size 16 \
  --seed 42 \
  --strategy swap \
  --k 1 \
  --max-rebuilds 2 \
  --cache .cache/transition_guard_p4_mock.jsonl
```

期望生成 16 个案例 × 5 个组 = 80 条结果。运行结束会打印结果目录。
这使用真实冻结输入，但模型响应是脚本模拟；其 ASR、Accuracy 等数字不能用来评价防御效果。

## 5. 配置 API

### 5.1 创建本地 .env

在项目根目录执行。下列命令不会覆盖已有 `.env`：

```bash
if [ ! -f .env ]; then
  cp .env.example .env
fi
```

使用编辑器打开：

```bash
nano .env
```

项目会自动加载根目录 `.env`，无需手动 `source .env`。
当前加载函数默认 `override=True`，所以 `.env` 中同名配置会覆盖已有环境变量；显式 CLI 参数再覆盖默认配置。
`.env` 已在原项目 `.gitignore` 中，真实密钥只填写在本地，不要写入源码或实验报告。

### 5.2 需要填写的配置

本轮最少需要：**API Key、服务支持的模型 ID，以及非默认服务的 Base URL**。

| 配置 | 是否必需 | 用途 |
|---|---|---|
| `OPENAI_API_KEY` | live 必需 | 原 Agent、Memory Updater 和本轮 TransitionGuard 的 API 凭据 |
| `OPENAI_BASE_URL` | 使用第三方兼容服务时需要 | 服务商给出的 API 根地址；须支持 OpenAI Chat Completions 的标准请求/响应 |
| `MEMFORM_MODEL` | 建议填写 | 新增实验入口的默认模型 ID；也可直接使用 `--model` |
| `JUDGE_OPENAI_API_KEY` / `JUDGE_OPENAI_BASE_URL` | 本轮不需要 | 原 MemForm 的诊断 Judge / Free-form Evaluator 使用 |
| `DEFENSE_OPENAI_API_KEY` / `DEFENSE_OPENAI_BASE_URL` | 本轮不需要 | 原 `run-defense` 使用；本轮新入口没有读取它们 |
| `FREEFORM_PARSER_MODEL` / `FREEFORM_JUDGE_MODEL` | 本轮不需要 | Free-form 专用，本轮只跑 MCQ |

本轮 TransitionGuard 默认与目标模型共用 endpoint、API Key 和模型。
可用 `--guard-model OTHER_MODEL_ID` 指定同一端点上的另一个审查模型；当前没有独立 guard endpoint/key 参数。

使用默认 OpenAI 服务时，`.env` 的相关内容可写成：

```dotenv
OPENAI_API_KEY=替换为你自己的真实密钥
MEMFORM_MODEL=替换为你有权限使用的模型ID

# 使用默认服务时可不设置 OPENAI_BASE_URL。
```

使用 OpenAI 兼容服务时：

```dotenv
OPENAI_API_KEY=替换为服务商提供的真实密钥
OPENAI_BASE_URL=https://替换为服务商域名/v1
MEMFORM_MODEL=替换为服务商支持的模型ID
```

Base URL 应以服务商文档为准，不能把网页地址当 API 地址；不要填写完整的 `/chat/completions` 路径。
`/v1` 只是常见路径，部分服务使用不同前缀。
当前 LangChain 评测客户端会原样传入你配置的非空 Base URL，不会替你补路径。
兼容服务的标准响应字段可被使用，额外的推理字段等不保证由 ChatOpenAI 保留。

`MEMFORM_MODEL` 只影响**新增实验入口**。运行原 `python -m memform run-langchain` 时，仍请显式传 `--model`。
模型 ID 不是展示名称，也不是 API Key；需与服务实际支持的名称一致。

### 5.3 检查配置是否存在，不显示密钥

```bash
python -B - <<'PY'
import os
from memform.common import load_env
load_env()
print("API Key 已配置:", bool(os.environ.get("OPENAI_API_KEY")))
print("自定义 Base URL 已配置:", bool(os.environ.get("OPENAI_BASE_URL")))
print("实验默认模型:", os.environ.get("MEMFORM_MODEL", "未设置，请使用 --model"))
PY
```

### 5.4 可选：只发一次最小 API 请求

这是本指南中第一条实际调用付费模型的命令。它不运行基准，只检查当前配置和模型能否响应。
需要先在 `.env` 中设置 `MEMFORM_MODEL`。

```bash
python -B - <<'PY'
import os
from langchain_openai import ChatOpenAI
from memform.common import load_env

load_env()
if not os.environ.get("OPENAI_API_KEY"):
    raise SystemExit("请先在 .env 中设置 OPENAI_API_KEY")
if not os.environ.get("MEMFORM_MODEL"):
    raise SystemExit("请先在 .env 中设置 MEMFORM_MODEL")

options = {
    "model": os.environ["MEMFORM_MODEL"],
    "api_key": os.environ["OPENAI_API_KEY"],
    "temperature": 0.0,
    "timeout": 30,
    "max_retries": 0,
}
if os.environ.get("OPENAI_BASE_URL"):
    options["base_url"] = os.environ["OPENAI_BASE_URL"]
response = ChatOpenAI(**options).invoke([("human", "Reply with OK only.")])
print("API 调用成功，返回:", response.content)
PY
```

### 5.5 使用 DeepSeek 官方 API

如果你直连 DeepSeek 官方 API，沿用现有 OpenAI 兼容客户端即可，不需要额外安装 `langchain-deepseek`。
本项目读取的是 `OPENAI_API_KEY`，所以应将 DeepSeek API Key 填在这个变量中，而不是只设置 `DEEPSEEK_API_KEY`。

```dotenv
OPENAI_API_KEY=替换为你的DeepSeek API Key
OPENAI_BASE_URL=https://api.deepseek.com
MEMFORM_MODEL=deepseek-flash
```

以上基于 2026-11-15 查阅的 [DeepSeek 官方快速开始](https://api-docs.deepseek.com/)：
当前列出的模型 ID 包括 `deepseek-flash` 和 `deepseek-v4-pro`。
若通过其他云平台调用 DeepSeek，应使用该平台提供的 URL、密钥与模型 ID，不能直接套用官方地址。

本轮五组实验无需配置 `JUDGE_*`、`DEFENSE_*`；guard 默认与 Agent / Memory Updater 使用同一模型和端点。
运行原 `run-langchain` 时要显式指定 `--model deepseek-flash`。
本轮不启用 `--target-alignment`，因为原实现该诊断使用固定的另一款 Judge 模型，而不是自动跟随 DeepSeek。

当前 [官方思考模式说明](https://api-docs.deepseek.com/guides/thinking_mode/) 指出：默认开启思考，
思考模式下 `temperature` 会被忽略，因此不能把本项目发送的 `temperature=0` 解释为确定性保证。
本项目目前没有从 `.env` 读取 DeepSeek thinking 开关；只添加一个同名环境变量不会改变请求。
默认模式可先用于连通性和闭环测试；后续若比较思考/非思考模式，需要额外暴露并记录请求参数。

可以用以下命令先运行一个样本的五组实验；较长超时是给思考请求留余量，不是完成时限保证：

```bash
python -B -m experiments.transition_guard_p4.run_experiment \
  --mode live \
  --model deepseek-flash \
  --sample-size 16 \
  --seed 42 \
  --strategy swap \
  --k 1 \
  --max-rebuilds 2 \
  --limit 1 \
  --timeout 300
```

此命令会调用真实 API。DeepSeek 思考内容的额外字段未单独存入本实验日志，评分使用最终 `content`；
成本记录仍以接口提供的 token usage 为准。上述配置尚未通过真实 DeepSeek API 请求验证。

## 6. 先验证原 MemForm 的真实 P4 基线

把 `YOUR_MODEL_ID` 替换为与 `.env` 中相同的模型 ID：

```bash
python -B -m memform run-langchain \
  --cases outputs/mcq_item_audit/accepted_cases.jsonl \
  --interfaces P4 \
  --phase learned \
  --max-cases 1 \
  --model YOUR_MODEL_ID \
  --output-dir "results/langchain_p4_smoke_$(date +%Y%m%d_%H%M%S)"
```

这是原评测入口，会调用真实 API。原 P4 learned 流程还会执行 full-pool 机制对照。
它用于验证原项目，不是后续五组实验的固定抽样基线文件；五组入口会在自己的同一清单上重新建立 B0。

完成后查看该目录的 `case_results.jsonl`、`summary.json`，以及是否有失败记录。

## 7. 运行 TransitionGuard 五组真实实验

| 组 | 方法 |
|---|---|
| B0 | No Defense |
| B1 | Final-only Judge：只查看候选记忆和已选经验 |
| B2 | Full-Pool Replay：复用原 full-pool 对照 |
| B3 | Random Counterfactual |
| B4 | Guided Counterfactual / TransitionGuard |

### 7.1 一个案例的五组 smoke test

```bash
python -B -m experiments.transition_guard_p4.run_experiment \
  --mode live \
  --model YOUR_MODEL_ID \
  --sample-size 16 \
  --seed 42 \
  --strategy swap \
  --k 1 \
  --max-rebuilds 2 \
  --limit 1
```

该命令保存完整的 16 条固定清单，但只运行第一条的五组实验。输出目录在结束时打印。
先检查 `errors.jsonl`（如存在）、`summary.json`、`comparison.md` 和 `call_events.jsonl`。
如果 B0–B4 均完整完成、答题可解析，再扩展运行。

### 7.2 复用同一清单运行 16 条

将 `SMOKE_DIR` 替换为上一条命令打印的目录名，例如 `<时间戳>_live`：

```bash
python -B -m experiments.transition_guard_p4.run_experiment \
  --mode live \
  --model YOUR_MODEL_ID \
  --manifest results/transition_guard_p4/SMOKE_DIR/selected_cases.json \
  --seed 42 \
  --strategy swap \
  --k 1 \
  --max-rebuilds 2
```

所有组使用同样的案例、目标模型和原决策 Prompt，且都测试 clean / poisoned 两种条件。
ASR 和固定集合 Clean Accuracy 的分母只从 B0 决定；防御破坏 clean 结果不会缩小分母。
Recovery 只统计 B0-eligible 集合中原投毒记忆答错、现在被恢复为正确答案的任务，分母为 0 时显示 N/A。

默认输出到新时间戳目录，不覆盖之前实验；原模型调用缓存可继续复用。
想重新发起模型请求时，应显式用一个新的 `--cache` 文件，不必删除已有缓存或结果。

### 7.3 使用同一清单比较 augment

保持上面的 manifest、seed、模型、K 和最大重建次数不变，只改：

```text
--strategy augment
```

augment 会增加经验数，swap 保持选择预算；两者应作为不同设置分别报告。
本轮优先查看 swap，暂时不需要扩展全 P4。后续扩展可去掉 `--manifest` 并用 `--sample-size 0`。

## 8. 在哪里看结果

每次新实验位于 `results/transition_guard_p4/<时间戳>_<mode>/`：

| 文件 | 优先查看内容 |
|---|---|
| `selected_cases.json` | 固定 case/family 列表、seed、源数据 SHA-256 |
| `run_config.json` | mode、模型、样本规模、swap/augment、K 等配置 |
| `case_results.jsonl` | 每个案例/组的原始与最终记忆、证据、反事实、验证、修复、提交决策及真实答题结果 |
| `call_events.jsonl` | 每次调用的角色、Prompt、响应、缓存命中、token usage、延迟 |
| `errors.jsonl` | 失败案例及组别；没有错误时可能不生成 |
| `summary.json` | 固定 eligibility、ASR、Accuracy、Recovery、修复和成本统计 |
| `comparison.md` | 五组可读比较；先确认是否标为 mock |
| `case_studies.md` | 最多三个可读案例，保留失败/不确定的情况 |
| `online_status.json` | 因凭据或依赖缺失而未启动 live 的原因；适用时生成 |

没有 token usage 的请求会记录为 null，不会伪造 token 数。
API 异常或答题解析失败不会被当成防御成功；应先处理不完整结果，再解释主要指标。

如需从原始日志重新生成派生报告：

```bash
python -B -m experiments.transition_guard_p4.analyze_results \
  results/transition_guard_p4/RUN_DIR
```

将 `RUN_DIR` 替换为实际目录。该命令更新该目录的 summary 和 Markdown，不修改原始逐样本和调用日志。

## 9. 常见问题

| 现象 | 检查方式 |
|---|---|
| `No module named langchain_openai` | `conda activate memform` 后重新 `python -m pip install -e .`；核对 `sys.executable` |
| `No API credential` | 检查根目录 `.env` 的 `OPENAI_API_KEY`，不是其他目录的 `.env` |
| 401 / 403 | 核对密钥是否属于该 endpoint，以及模型访问权限 |
| 404 / model not found | 核对 Base URL 路径和真实模型 ID，尤其不要填展示名称 |
| 429 | 检查服务商限额/额度；保留日志和缓存后再跑小样本 |
| 超时 / 连接错误 | 检查本机到 API 服务的网络、地址及代理；不要先扩大实验规模 |
| 不支持 temperature 等参数 | 当前实验使用 `temperature=0.0`，需选用支持当前 Chat Completions 请求参数的模型/服务 |
| 输出目录已存在 | 换一个新目录，或不传 `--output-dir` 让程序自动创建 |
| Summary 中 ASR 为 null | 检查 eligibility 是否为 0，或是否存在缺失结果/解析失败；不要手动改分母 |
| CLI 显示 generate / compile 但执行失败 | 评测发布版未提供构建模块；本轮使用 run-langchain 和新增实验入口 |

## 10. 已完成与尚待完成

初版完成了 25 项离线测试、原入口 validate-only，以及固定 16 条案例的 swap/augment 五组 mock。
已有本地结果可见 `results/transition_guard_p4/mock_seed42_16_swap_k1_final/` 和对应 augment 目录。
这些 mock 结果仅证明流程闭环。

随后用户已配置 Conda / DeepSeek，并执行单样本和固定 16 条真实 pilot。
最新分析位于 `results/transition_guard_p4/20261116T061612_858191Z_live/pilot_analysis.md`。
该轮仍有 B3 1 条、B4 9 条连接失败，不能宣称完整效果已得到验证。
目前测试集扩展为 32 项，新增按 case/group 续跑与异常原因链记录，未改变算法 Prompt。

依赖文件使用版本下限而非完整锁定；首次安装成功后可记录环境，便于后续复现：

```bash
mkdir -p results/environment
python -m pip freeze > "results/environment/pip_$(date +%Y%m%d_%H%M%S).txt"
conda env export --no-builds > "results/environment/conda_$(date +%Y%m%d_%H%M%S).yml"
```

详细研究设计见 [实验 README](experiments/transition_guard_p4/README.md)。配置命令参考
[Conda 官方环境管理文档](https://docs.conda.io/projects/conda/en/stable/user-guide/tasks/manage-environments.html)，
ChatOpenAI 的标准 API 配置与兼容性参考
[LangChain 官方文档](https://docs.langchain.com/oss/python/integrations/chat/openai)。

## 11. 补跑失败任务与 DeepSeek 代理分流

### 11.1 当前版本怎样补跑

当前入口已新增 **`--resume-from`**，可读取已结束的运行目录：

- 成功完成且预测格式有效的 case/group 直接复用，**不再次运行这个单元**，无论它答对还是答错。
- 只执行缺失或解析无效的单元；其中已成功的 LLM 步骤仍读取原缓存。
- 自动读取源目录的 `selected_cases.json`，校验模型、方法、seed、K、样本、endpoint 和缓存路径等是否兼容。
- 新建输出目录，自动合并复用结果与新结果，不覆盖源目录。
- 复制源调用事件并保留事件范围，历史错误放入 `prior_errors.jsonl`；当前失败仍写 `errors.jsonl`。
- `summary.total_cost` 包括续跑源链及本次的调用开销；`new_run_cost` 单列本次新增开销。
  不在源链上的更早独立运行，仍需保留并另外累计，不能重复累加已被继承的调用记录。

例如补跑本次 16 条实验：

```bash
python -B -m experiments.transition_guard_p4.run_experiment \
  --mode live \
  --model deepseek-flash \
  --resume-from results/transition_guard_p4/20261116T061612_858191Z_live \
  --seed 42 \
  --strategy swap \
  --k 1 \
  --max-rebuilds 2 \
  --timeout 300 \
  --cache .cache/transition_guard_p4.jsonl
```

针对上述源目录，离线验证计划是 `retained=70 pending=10`：B3 1 条，B4 9 条。
如需仅检查续跑计划，把 `--mode live` 改为 `--mode validate`，不会调用模型。
新目录的 `resume_plan.json` 保存待补跑清单；实际运行中 `reused` 表示直接复用了完整结果，`ok` 表示执行了待补单元。
应在源进程结束后执行，不同时启动两个进程共写这个缓存。
成功补齐后，新目录的 `summary.json` 应为 `complete: true`，五组各有 16 条有效结果。
不要使用 `cat` 拼接两次的 case_results：它会产生相同 case/group 的重复记录。
使用新目录作为完整评测结果。若续跑仍有连接错误，下次将 `--resume-from` 改为这次的新目录，
即可保留这次新增的成功结果；不要一直从更早目录重复构造续跑。
LLM 缓存补跑不会修复已缓存的格式错误响应；若持续出现同一解析错误，需要单独定位，不能无限原样重跑。
当前新增 `error_chain` 仅记录异常类型，例如高层 SDK 错误和底层 ConnectTimeout/ProxyError 的因果链，
不保存可能包含密钥的任意异常消息。没有这一链的历史记录仍保留原样。

### 11.2 不必关闭全部代理，先为 DeepSeek 单独直连

本轮 LLM 请求都使用配置的 DeepSeek endpoint，不需要访问 GitHub、arXiv 或 OpenAI 官网。
依赖安装、其他工具或浏览器是否需要代理，是独立的网络需求。
外国代理出口可能引入额外延迟、连接失败，但现有错误日志不足以证明代理就是原因。
先做直连测试，选择本机更稳定的路径。

如果代理由 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` 环境变量控制，可以在启动下一次实验前执行：

```bash
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}api.deepseek.com,.deepseek.com"
export no_proxy="$NO_PROXY"
```

这样为 DeepSeek 增加直连例外，其他地址仍可使用原代理。
HTTPX 默认读取代理环境变量，`NO_PROXY` 用于指定绕过代理的主机；参见
[HTTPX 官方说明](https://www.python-httpx.org/environment_variables/)。
这两个 export 不会改变已经运行的 Python 进程。
如果代理软件使用 TUN / VPN 全局接管流量，NO_PROXY 不能绕过系统路由；
应在代理软件中把 `api.deepseek.com` 设置为 DIRECT / 直连，或者做一次关闭 TUN 的独立对照。
不要在当前实验尚未结束时切换网络，避免打断正在进行的请求。

### 11.3 无密钥、无模型生成的连通性对照

在另一个终端执行以下两条命令，不带 API Key，也不会提交模型生成任务。
第一条使用 curl 的正常代理配置；第二条要求绕过应用层代理：

```bash
curl -sS -o /dev/null \
  --connect-timeout 10 --max-time 20 \
  -w 'normal: HTTP=%{http_code} connect=%{time_connect}s total=%{time_total}s\n' \
  https://api.deepseek.com/models

curl --noproxy '*' -sS -o /dev/null \
  --connect-timeout 10 --max-time 20 \
  -w 'direct: HTTP=%{http_code} connect=%{time_connect}s total=%{time_total}s\n' \
  https://api.deepseek.com/models
```

HTTP 401 等鉴权响应也说明本次网络请求得到了 HTTP 响应；不代表密钥已验证或后续长请求一定稳定。
若直连明显更稳定，给 DeepSeek 配置直连例外后再补跑。
若两者都失败或都间歇失败，继续检查 DNS、TLS、代理规则和服务状态，不能只靠增加 timeout。
curl 测试仅是辅助对照，不等于验证 Python HTTPX 的完整连接行为；TUN 模式下两条请求也可能走同一路由。

## 12. 五组到底在比较什么，下一步先做什么

用一个示意场景理解：完整池有 9 条真实观察，保留预算为 4 条。
有些观察支持“经过验证可以释放资源”，另一些记录是在特定场景下直接释放也没有出问题。
攻击者改变保留的 4 条，模型可能把局部成功归纳为“没有验证也可以释放”。

| 组别 | 模型看到的额外证据 | 实际操作 | 要回答的问题 |
|---|---|---|---|
| B0 无防御 | 无 | 选中 4 条形成候选记忆，直接交给原 Agent | 原攻击是否有效 |
| B1 最终记忆审查 | 仅候选记忆和已选 4 条 | LLM 判断这 4 条能否支持候选规则；发现缺口时在这 4 条内修复，否则保留；修复不能通过时 defer | 不看遗漏历史，只审查最终状态够不够 |
| B2 全池重建 | 完整 9 条 | 用原 Memory Updater 把完整池重新归纳，直接替换原记忆 | 忽略攻击者的保留结果、恢复完整证据是否有效 |
| B3 随机反事实 | 随机选遗漏记录 | 从未保留的 5 条中固定种子选 witness，每次 swap 1 条；用原 updater 重建，验证证据缺口后才修复 | 随机补少量证据是否已经有帮助 |
| B4 定向反事实 | 根据规则选择的遗漏记录 | LLM 阅读所选与遗漏经验，优先找限制/反驳规则的 witness；后续重建、验证、修复与 B3 一致 | 定向找关键证据是否比随机更有效 |

B1 并不是另一个只能查敏感词的过滤器，它检查候选规则与已选经验的支持关系。
但如果这 4 条都只包含局部成功，它也可能认为泛化成立，无法知道被遗漏的反例。
B2 没有先检测攻击或确认缺口，而是直接重建，属于完整池访问下的强诊断对照。
B3/B4 都不会因为反事实文本变了就自动判异常，且都要经过同样的验证、修复和支持检查。
当前 B3/B4 每次 K=1，最多两次尝试；B4 的整合器仍只吃 4 条，不代表整个防御只读了 4 条——选择器读取完整遗漏池。

**当前优先完成同版本实验，再优化算法。**

1. 用 `--resume-from` 补齐当前 10 个技术失败单元，同时保留成功结果。连接错误不是算法防御失败。
2. 若继续连接失败，利用新增 `error_chain` 排查连接、代理、TLS 等问题，不增加样本规模。
3. 完整后对比 repair recovery / defer recovery，以及 clean 条件的规则放宽；已有错误案例可同时人工审阅。
4. 此后另立版本优化 verifier / repairer 的冲突条件和证据晋升门槛，增加 no-op replay 和 defer-only 消融。
   新方法不和当前冻结版本的旧结果混合。当前不需要推翻框架，也不宜宣称定向方法已优于随机。

需要特别检验 B2：当前池很小、全池重建效果好，因此必须说明 TransitionGuard 的额外价值或适用约束，
不能仅凭更复杂的检查链就声称更强或更省成本。
