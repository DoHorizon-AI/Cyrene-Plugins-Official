# Media Library / 媒体目录

The media package provides a local persistent image catalog, SHA-256 blob
deduplication, verified reads, soft deletion and restoration, bounded listing,
and actual PNG card rendering. It stores content beneath one operator-selected
state directory; requests carry asset IDs and image bytes, never host paths.

The library owns blob storage and content integrity. Navigator owns persona,
conversation, group, and recipient permissions. A caller must authorize before
requesting an asset and again before sending it.

The DirectPluginRuntime adapter publishes the fixed signed tools
media.asset.import, media.asset.search, media.asset.get, media.asset.delete,
media.asset.restore, and media.card.render. Import and export use only
actor-scoped artifact IDs through the Navigator task-scoped loopback bridge.
The host verifies that an input artifact is attached to the current authorized
source before invoking the provider. Card rendering writes a Work artifact and
never sends a message. The bridge, not this package, holds Work access tokens.

Pillow is required for image validation and rendering. The package does not fetch
fonts or other assets. A host font may be configured with
CYRENE_MEDIA_FONT; Latin fallback fonts are discovered from fixed system paths.
If no font can render the requested text, card rendering fails instead of
returning an empty or placeholder image.

## Local use

    python -m pip install -e '.[test]'
    pytest -q

Configure trusted workload scopes pinned to issuer, organization, and workspace,
plus a fixed artifact_bridge_origin and hostSecretRefs for the caller
verification key, private state directory, and binding-scoped loopback bridge
token. Each signed call supplies its subject and request ID; Navigator verifies
that subject against the active task and routes the bridge request to that
task's Work client. Client requests cannot select paths, endpoints, subjects,
or Work credentials.
Individual assets are limited to 10 MiB, matching the Work attachment transport.

## Files / 文件

| File | Responsibility |
| --- | --- |
| src/cyrene_media/library.py | SQLite catalog, content-addressed blobs, integrity, and PNG cards. |
| tests/ | Deduplication, recovery, integrity, and PNG output checks. |

<!-- Chinese Translation / 中文翻译 -->

## 中文说明

此媒体包提供本地持久图片目录、SHA-256 blob 去重、读取校验、软删除和恢复、有界列表及
真实 PNG 卡片渲染。所有文件只写入运维配置的状态目录；请求只传 asset ID 和图像字节，
不会传入宿主文件路径。

插件负责 blob 存储和内容完整性。人格、会话、群组及接收人权限由 Navigator 管理。取用
媒体前应由调用方授权，发送前再次检查权限。

工具通过固定的签名操作暴露；导入/导出经Navigator任务范围的loopback附件桥。每次请求都
附带签名caller context，Navigator按request ID找到当前任务，再核对subject并检查来源授权。
桥接token只认证provider binding，不授予Work权限，也不允许模型选择端点、subject或token。

图片校验和渲染需要 Pillow。包不会下载字体或其他资产。可通过 CYRENE_MEDIA_FONT 配置
宿主字体；没有可渲染所需文本的字体时，卡片生成会明确失败，不返回空白或占位图片。
