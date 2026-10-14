# LLaMA-Factory Trainer / 训练能力实现

Canonical `training.llama-factory.v1` implementation owned by Plugins.

Plugins 拥有的 `training.llama-factory.v1` 唯一实现。

| Method | Responsibility | 职责 |
| --- | --- | --- |
| `inspect` | Declare the trainer surface without probing devices | 声明训练器能力，不探测设备 |
| `compile` | Materialize a LLaMA-Factory run directory and executor-agnostic launch | 生成训练运行目录与执行器无关的启动描述 |
| `parse_event` | Classify one trainer output line | 解析训练输出行 |

`compile` writes `train_config.json` and `dataset_info.json` into the run directory and
returns the argv, mounts, and resource request. It never assigns devices: the Kernel
executor owns `CUDA_VISIBLE_DEVICES`, mounts, and process lifetime.

`dataset.schema` selects the loader mapping: `instruction` preserves the legacy
Alpaca columns, `instruction_history` maps system prompts and prior
`[user, assistant]` turns, `messages` maps ordered OpenAI-style roles through
the ShareGPT tags, and `prompt_completion` maps prompt/completion columns. For
backward compatibility, an `instruction` DatasetRef probes only the first
bounded JSONL row and adds system/history mappings when those columns exist.

`compile` 会写入 `train_config.json` 与 `dataset_info.json`，并返回 argv、挂载与资源请求；
设备分配、挂载与进程生命周期由 Kernel 执行器负责。

## Configuration / 配置

| Variable | Purpose |
| --- | --- |
| `CYRENE_LLAMA_FACTORY_ENTRYPOINT` | Full launcher command, for example `/opt/venv/bin/llamafactory-cli` |
| `CYRENE_LLAMA_FACTORY_PYTHON` | Interpreter used as `<python> -m llamafactory.cli` when no entrypoint is set |
| `CYRENE_LLAMA_FACTORY_VERSION` | Version reported by `inspect` |

Verification: `cd plugins/training/llama-factory && python -m pytest tests`.
Real CUDA training is not verified by these tests; see `contracts/capability-verification.json`.
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# LLaMA-Factory Trainer

这是由 Plugins 所有的 training.llama-factory.v1 规范实现。

| Method | 职责 |
| --- | --- |
| inspect | 声明 trainer 能力，但不探测设备。 |
| compile | 物化 LLaMA-Factory run 目录，并生成与 executor 无关的启动配置。 |
| parse_event | 对一行 trainer 输出进行分类。 |

compile 会将 train_config.json 和 dataset_info.json 写入 run 目录，并返回 argv、mount 和 resource request。它绝不分配设备：CUDA_VISIBLE_DEVICES、mount 和进程生命周期都由 Kernel executor 负责。

`dataset.schema` 选择 loader 映射：`instruction` 保留旧版 Alpaca 字段，
`instruction_history` 映射 system prompt 和历史 `[user, assistant]` 轮次，
`messages` 通过 ShareGPT tags 映射 OpenAI 风格有序角色，`prompt_completion` 映射
prompt/completion 字段。为兼容旧版 `instruction` DatasetRef，插件只探测首条有大小上限的
JSONL 记录，并在存在时映射 system/history 列。

## 配置

| 变量 | 用途 |
| --- | --- |
| CYRENE_LLAMA_FACTORY_ENTRYPOINT | 完整 launcher 命令，例如 /opt/venv/bin/llamafactory-cli。 |
| CYRENE_LLAMA_FACTORY_PYTHON | 未设置 entrypoint 时，用作 <python> -m llamafactory.cli 的解释器。 |
| CYRENE_LLAMA_FACTORY_VERSION | inspect 报告的版本。 |

验证方式：进入 plugins/training/llama-factory 并运行 python -m pytest tests。这些测试没有验证真实 CUDA 训练；请参阅 contracts/capability-verification.json。
