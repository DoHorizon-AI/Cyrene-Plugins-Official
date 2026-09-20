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
