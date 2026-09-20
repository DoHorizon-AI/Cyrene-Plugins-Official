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

`compile` 会写入 `train_config.json` 与 `dataset_info.json`，并返回 argv、挂载与资源请求；
设备分配、挂载与进程生命周期由 Kernel 执行器负责。

## Configuration / 配置

| Variable | Purpose |
| --- | --- |
| `CYRENE_LLAMA_FACTORY_ENTRYPOINT` | Full launcher command, for example `/opt/venv/bin/llamafactory-cli` |
| `CYRENE_LLAMA_FACTORY_PYTHON` | Interpreter used as `<python> -m llamafactory.cli` when no entrypoint is set |
| `CYRENE_LLAMA_FACTORY_VERSION` | Version reported by `inspect` |

Verification: `python -m pytest plugins/training/llama-factory/tests`.
Real CUDA training is not verified by these tests; see `contracts/capability-verification.json`.
