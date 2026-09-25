# training.llama-factory.v1 TCK / 训练契约测试

This TCK validates the `training.llama-factory.v1` payloads against the
owner-scoped schema and the trainer's authority invariants, independent of
Product state and without a GPU.

本 TCK 依据 owner-scoped schema 校验 `training.llama-factory.v1` 载荷，并验证训练器的
权威边界；不读取 Product 状态，也不需要 GPU。

```bash
python3 -m pytest -q contracts/tck/training-llama-factory-v1
```

Coverage map / 覆盖说明:

- Schema conformance: `inspect`, `compile`, and `parse_event` responses validate
  against `plugins/training/llama-factory/contracts/v1/schema.json`.
- Device authority: `compile` rejects any `CUDA_VISIBLE_DEVICES` or machine
  device assignment; the Kernel executor remains the only device authority.
- Identity authority: `compile` rejects overrides of `output_dir` and other
  trainer identity fields through `extra.llamafactory_args`.
- Materialization: the compiled run directory carries `train_config.json` and
  `dataset_info.json` with the canonical dataset identity.
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# training.llama-factory.v1 TCK

本 TCK 根据 owner 范围 schema 验证 training.llama-factory.v1 payload，并检查 trainer 的 authority 不变量；它独立于 Product 状态，也不需要 GPU。

```bash
python3 -m pytest -q contracts/tck/training-llama-factory-v1
```

## 覆盖说明

- **Schema 一致性**：inspect、compile 和 parse_event 响应根据 plugins/training/llama-factory/contracts/v1/schema.json 进行验证。
- **设备权威**：compile 会拒绝任何 CUDA_VISIBLE_DEVICES 或机器级设备分配；Kernel executor 始终是唯一的设备 authority。
- **身份权威**：compile 会拒绝通过 extra.llamafactory_args 覆盖 output_dir 和其他 trainer identity 字段。
- **物化**：编译后的 run 目录包含 train_config.json 和 dataset_info.json，其中记录规范数据集身份。
