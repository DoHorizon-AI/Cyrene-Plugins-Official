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