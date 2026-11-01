# SenseVoice Provider Tests / SenseVoice 服务测试

These tests exercise request validation, bounded resource handling, cancellation,
artifact resolution, and explicit missing-model behavior without pretending a
mock recognizer is real speech acceptance. Real transcription requires a
provisioned SenseVoice model, sherpa-onnx, FFmpeg, and a separately licensed test
audio sample. 本测试不会把模拟识别器当作真实语音验收。
