# SenseVoice Speech Provider / SenseVoice 语音服务

cyrene.providers.speech-sensevoice implements the versioned
speech.provider.v1.transcribe DirectPluginRuntime method with local CPU
inference through sherpa-onnx SenseVoice and FFmpeg decoding. The runtime never
downloads models or accepts a URL or filesystem path from a request. Configure
the model and token files through the trusted binding environment and provision
them separately.

The provider accepts bounded audio bytes. artifact_id is accepted only when a
trusted, binding-scoped artifact resolver is injected; the default runtime has
no resolver and fails closed. Unsupported media, missing model files, missing
runtime dependencies, provider failures, quota violations, and cancellation do
not return a successful transcript. Product ownership, speaker identity,
conversation policy, and media permissions remain with Navigator.

## Runtime configuration

| Activation field | Default | Purpose |
| --- | --- | --- |
| model_path | unset | Absolute operator-provisioned SenseVoice ONNX model |
| tokens_path | unset | Absolute operator-provisioned tokens.txt |
| model_id | sensevoice-local | Non-secret model label returned in receipts |
| num_threads | 2 | CPU inference thread count, 1–8 |
| max_input_bytes | 25165824 | Maximum compressed audio input |
| max_duration_seconds | 300 | Maximum decoded duration |
| ffmpeg_path | ffmpeg | Fixed executable name or operator-configured path |

Install the optional inference extra and provide FFmpeg separately:

    python -m pip install -e '.[speech,test]'
    export CYRENE_CAPABILITY_BINDING_ID=speech-local
    export CYRENE_CAPABILITY_CONFIGURATION_JSON='{"model_path":"/var/lib/cyrene/speech/model.int8.onnx","tokens_path":"/var/lib/cyrene/speech/tokens.txt"}'

The model files are never included in the package. When a model or dependency is
unavailable, the plugin reports a typed failure instead of returning a mock
transcript.

The real WSL CPU smoke used the official sherpa-onnx Chinese sample. The model
returned “开饭时间早上9点至下午5点。” for the sample whose reference text is
“开放时间早上9点至下午5点。”; the provider preserves this genuine model output.
SenseVoice emits language markers such as `<|zh|>`; recognized markers are
normalized to short language codes (`zh`, `en`, `ja`, `ko`, `yue`), and unknown
markers are omitted.

## Checks

    pytest -q

## Files / 文件

| File | Responsibility |
| --- | --- |
| src/speech_sensevoice/plugin.py | Versioned DirectPluginRuntime adapter and typed errors. |
| src/speech_sensevoice/service.py | Bounded FFmpeg decode and local SenseVoice inference. |
| configuration.schema.json | Operator-only model and resource binding schema. |
| tests/ | Request, quota, cancellation, and unavailable-model behavior. |

<!-- Chinese Translation / 中文翻译 -->

## 中文说明

cyrene.providers.speech-sensevoice 通过本地 CPU sherpa-onnx SenseVoice 与
FFmpeg 解码实现 speech.provider.v1.transcribe。运行时不会下载模型，请通过受信任的
binding activation JSON 配置绝对模型路径和 tokens 文件，并由运维单独提供模型文件。

服务接收有界音频字节。只有注入 binding 范围内的受信任 artifact resolver 后，才会解析
artifact_id；默认运行时没有 resolver，因而会 fail-closed。媒体不支持、模型或依赖缺失、
推理失败、超额输入和取消都不会返回成功转写。说话人身份、会话策略和媒体权限仍由
Navigator 管理。
