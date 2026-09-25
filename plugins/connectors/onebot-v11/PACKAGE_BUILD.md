# OneBot v11 package build / OneBot v11 包构建

The formal package is a RID-specific C# Native AOT executable. It contains no
Python runtime and no QQ/IM capability. Build the native candidate after the
corresponding host has been published:

```bash
python3 plugins/connectors/onebot-v11/tools/assemble_native_package.py \
  --repository-root . \
  --binary runtime/dotnet-native-aot/Cyrene.OneBot.V11.Host/bin/Release/net10.0/linux-x64/publish/cyrene-onebot-v11 \
  --rid linux-x64 \
  --output /tmp/cyrene-onebot-v11-native-linux-x64.zip
```

The builder accepts `linux-x64` and `linux-arm64`, copies only the generic
contract files and metadata, and rejects a missing or non-executable binary.
The installed archive must not contain `.py`, `.pyc`, or `src/` files.

The Python package builder is a behavior-reference utility used by
`tools/ci/assemble_onebot_reference.py`; it is not the formal release
entrypoint and does not provide a dual-runtime fallback.
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# OneBot v11 package 构建

正式 package 是按 RID 构建的 C# Native AOT 可执行文件。它不包含 Python runtime，也不包含 QQ/IM capability。对应 host 发布后，再构建原生候选包：

```bash
python3 plugins/connectors/onebot-v11/tools/assemble_native_package.py --repository-root . --binary runtime/dotnet-native-aot/Cyrene.OneBot.V11.Host/bin/Release/net10.0/linux-x64/publish/cyrene-onebot-v11 --rid linux-x64 --output /tmp/cyrene-onebot-v11-native-linux-x64.zip
```

builder 接受 linux-x64 和 linux-arm64，只复制通用契约文件和 metadata；如果 binary 缺失或不可执行，就会拒绝构建。安装后的 archive 不能包含 .py、.pyc 或 src/ 文件。

Python package builder 是 tools/ci/assemble_onebot_reference.py 使用的行为参考工具，不是正式发布入口，也不提供双 runtime 回退。
