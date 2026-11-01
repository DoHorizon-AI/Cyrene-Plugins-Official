# SenseVoice Runtime Modules / SenseVoice 运行模块

| File | Responsibility |
| --- | --- |
| service.py | Bounded FFmpeg audio decoding, CPU model loading, and transcription. |
| plugin.py | Typed speech.provider.v1 DirectPluginRuntime adapter. |
| __init__.py | Package metadata only; no import-time model or contract side effects. |

Suggested reading order: service.py → plugin.py. 推荐阅读顺序：推理服务、协议适配器。
