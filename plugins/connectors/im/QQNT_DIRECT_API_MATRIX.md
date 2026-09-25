# QQNT Direct API Matrix / QQNT Direct API 映射矩阵

This matrix is the human-readable projection of the executable fixed-operation
table in `src/qq_connector/qqnt_direct_operations.py`. Its capability
source is [`QQ_API_PLAN.md`](QQ_API_PLAN.md). Every operation named
by that plan is represented below; the extra `qq.friend.set_remark` operation is
an explicitly named adapter extension and is not used to hide arbitrary native
calls.

本矩阵是 `src/qq_connector/qqnt_direct_operations.py` 固定操作表的人工投影，能力来源
是 [`QQ_API_PLAN.md`](QQ_API_PLAN.md)。计划中列出的每个操作都在下表出现；额外的
`qq.friend.set_remark` 是显式登记的适配器扩展，不会被用来隐藏任意原生调用。

## Target and evidence policy / 目标与证据策略

The first supported real target is Linux x86_64. A deployment must provide a
written authorization record, the exact QQ client build string, and the exact
Host ABI. Configuration carries the build string as `required_client_version`
and the ABI as `required_host_abi`; the Host hello carries `client_version`
and `abi`. Because this repository does
not contain an authorized QQ build or native Host, every real API row is
currently `NOT_RUN`. Fake Host tests prove only the worker boundary, mapping
mechanics, and failure handling.

首个真实目标是 Linux x86_64。部署必须提供书面授权、准确 QQ client build 字符串和准确 Host
ABI。配置中的 `required_client_version` 固定 build 字符串，`required_host_abi` 固定 Host
ABI，Host hello 返回 `client_version` 与 `abi`。当前仓库没有获授权的 QQ build 或原生
Host，因此所有真实 API 行仍为 `NOT_RUN`；
Fake Host 只证明 worker 边界、映射机制和失败处理。

The request/result columns below describe the normalized `qq.client.v1`
envelope. They are not invented native overload signatures. Before a row can
move to `PASS`, the acceptance ledger must record the exact native signature,
input identifier types, direct return, listener callback, timeout/cancel result,
and observed error for the configured build, without recording credentials,
tickets, session files, or private message bodies.

下表请求/结果列描述规范化的 `qq.client.v1` envelope，不臆造 native overload 签名。某行只有在
验收记录中补齐目标 build 的准确 native signature、输入标识类型、直接返回、Listener 回调、
超时/取消结果和实际错误后才能从 `NOT_RUN` 变为 `PASS`；验收记录不得写入凭据、票据、会话
文件或私聊正文。

The canonical `message.connector.v1` mapper currently has four content kinds:
text, mention, image, and file; replies use a separate reply reference. Native
audio/video elements are not coerced into another kind. They remain available
only through the fixed QQ media/file operation boundary until the canonical
connector contract defines a corresponding content type.

canonical `message.connector.v1` mapper 当前只有四种内容：text、mention、image、file；reply
使用独立的 reply reference。原生 audio/video 不会被强行转换成其他类型；在 canonical connector
契约定义对应内容类型前，它们只保留在固定 QQ media/file 操作边界内。

## Coverage matrix / 覆盖矩阵

| Priority | QQ API row / QQ API 行 | Direct operation(s) / 直连操作 | Native service and method / 原生 Service 与方法 | Request fields / 请求字段 | Result or callback / 结果或回调 | Target / evidence |
| --- | --- | --- | --- | --- | --- | --- |
| P0 | Session lifecycle | `qq.session.create`, `qq.session.init`, `qq.session.start_nt`, `qq.login.connect`, `qq.login.online`, `qq.login.offline` | `NodeIQQNTWrapperSession.create/init/startNT`; `NodeIKernelLoginService.connect/online/offline` | `account_id?`, `platform`, `client_version`, `data_dir`, `login_policy` | `state`, `session_id`, `account_id`, `client_version`, `abi`; login/session state callback | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P0 | Login state | `qq.login.list`, `qq.login.quick`, `qq.login.password`, `qq.login.qr`, `qq.login.poll` | `NodeIKernelLoginService.getLoginList/quickLoginWithUin/passwordLogin/getQRCodePicture/startPolling` | `account_id?`, `uin?`, `secret_ref?`, `qr_code?`, `poll_interval_seconds?` | `login_state`, `account_id`, QQ UIN, user UID, display name, QR/progress result; login callback | Linux x86_64 + exact build/ABI; password is secret-ref only; `NOT_RUN` |
| P0 | Login status observation | `qq.login.self_status` | `NodeIKernelProfileService.getSelfStatus` | `account_id?`, `uin?`, `uid?` | `login_state`, `account_id`, QQ UIN, user UID, display name | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P0 | Self identity | `qq.account.core`, `qq.account.simple` | `NodeIKernelProfileService.getCoreAndBaseInfo/getUserSimpleInfo` | `account_id?`, `uid?`, `uin?` | account ID, QQ UIN, user UID, display name, profile result | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P0 | Receive messages | `qq.message.subscribe` | `NodeIKernelMsgService.addKernelMsgListener` + `NodeIKernelMsgListener.onRecvMsg` | `account_id?`, `events?`, `filter?` | `message.received` callback: account, peer, message ID, sequence, random, timestamp, ordered elements | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P0 | Send messages | `send_message`, `qq.message.send`, `qq.message.send_completion` (callback-only) | `NodeIKernelMsgService.sendMsg` + `onMsgInfoListUpdate` | canonical conversation/content/reply; `peer`, `elements` | accepted DeliveryResult, native message ID, sequence, random, peer; completion callback | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P0 | Peer resolution | `qq.peer.uid_by_uin`, `qq.peer.uin_by_uid`, `qq.peer.uid`, `qq.peer.uin` | `NodeIKernelProfileService.getUidByUin/getUinByUid`; `NodeIKernelUixConvertService.getUid/getUin` | `account_id?`, `uid?`, `uin?`, `user_uid?`, `user_uin?`, `peer_uid?` | explicit UID/UIN/peer identity result; no lossy ID substitution | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P1 | Message lookup | `qq.message.history_include_self`, `qq.message.history_by_seq`, `qq.message.by_id`, `qq.message.single`, `qq.message.search` | `getMsgsIncludeSelf/getMsgsBySeqAndCount/getMsgsByMsgId/getSingleMsg/queryMsgsWithFilterEx` | `account_id`, `peer`, `message_id?`, `sequence?`, `random?`, `offset?`, `count?`, `filter?`, `query?` | ordered messages with message ID, sender, timestamp, sequence, random, elements | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P1 | Recall and forward | `qq.message.recall`, `qq.message.forward`, `qq.message.forward_comment`, `qq.message.multi_forward` | `recallMsg/forwardMsg/forwardMsgWithComment/multiForwardMsg` | `account_id`, `source`, `destination`, `message_id?`, `message_ids?`, `comment?`, `messages?` | accepted/rejected result, source/destination identity, resulting message ID; completion when supplied | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P1 | Read and emoji likes | `qq.message.read`, `qq.message.read_all`, `qq.message.emoji_likes`, `qq.message.emoji_likes_list` | `setMsgRead/setAllC2CAndGroupMsgRead/setMsgEmojiLikes/getMsgEmojiLikesList` | `account_id`, `peer?`, `message_id?`, `message_ids?`, `like_id?`, `like_type?` | status, read marker, like list/count, message identity | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P1 | Group discovery | `qq.group.list`, `qq.group.detail`, `qq.group.members`, `qq.group.member` | `getGroupList/getGroupDetailInfo/getAllMemberList/getMemberInfo` | `account_id`, `group_id?`, `group_code?`, `member_uid?`, `member_uin?`, `offset?`, `count?` | group code, name, permissions, member UID/UIN, display data | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P1 | Friend discovery | `qq.friend.list`, `qq.friend.cached`, `qq.friend.requests` | `getBuddyListV2/getBuddyListFromCache/getBuddyReq` | `account_id`, `uid?`, `uin?`, `offset?`, `count?` | friend UID/UIN, display data, request identity/state | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P1 | Media download | `qq.media.element`, `qq.media.download`, `qq.media.video_url`, `qq.media.download_complete` (callback-only) | `getRichMediaElement/downloadRichMedia/getVideoPlayUrlV2/onRichMediaDownloadComplete` | `account_id`, `message_id?`, `element_id?`, `media_id?`, `media_type?`, `codec?`, `download?` | element/file ID, media type, progress, bounded `remote_uri` or QQ media ref, local result ref, error; download callback | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P1 | Files | `qq.file.list`, `qq.file.search`, `qq.file.download`, `qq.file.forward`, `qq.file.save` | `getGroupFileList/searchFile/downloadFile/forwardFile/saveAs` | `account_id`, `group_id?`, `folder_id?`, `file_id?`, `file_uuid?`, `file_name?`, `query?`, `source?`, `destination?` | bounded file reference, name/type, progress, local result ref, error; file callback | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P2 | Group administration | `qq.group.modify_name`, `qq.group.modify_remark`, `qq.group.mute_member`, `qq.group.mute`, `qq.group.kick`, `qq.group.quit` | `modifyGroupName/modifyGroupRemark/setMemberShutUp/setGroupShutUp/kickMember/quitGroup` | `account_id`, `group_id`, `member_uid?`, `member_uin?`, `name?`, `remark?`, `duration_seconds?` | accepted/rejected status, target identity, reason; admin callback if native API emits one | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P2 | Group join approval adapter | `qq.group.approve` | `NodeIKernelGroupService.operateSysNotify` | `account_id`, `request_id`, `group_id?`, `user_id?`, `approve`, `comment?`, `vendor_request?` | accepted/rejected request result, request/group/member identity | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P2 | Friend requests and remark | `qq.friend.approve`, `qq.friend.doubt_requests`, `qq.friend.approve_doubt`, `qq.friend.add`, `qq.friend.delete`, `qq.friend.set_remark` | `approvalFriendRequest/getDoubtBuddyReq/approvalDoubtBuddyReq/reqToAddFriends/delBuddy/setBuddyRemark` | `account_id`, `request_id?`, `uid?`, `uin?`, `approve?`, `comment?`, `remark?` | request/friend identity, decision/status, reason; request callback where native API emits one | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P2 | Profile mutation | `qq.profile.modify`, `qq.profile.nickname`, `qq.profile.long_nick`, `qq.profile.birthday`, `qq.profile.gender`, `qq.profile.header` | `modifySelfProfile/setNickName/setLongNick/setBirthday/setGander/setHeader` | `account_id`, `profile?`, `nickname?`, `long_nick?`, `birthday?`, `gender?`, `header?` | field-level accepted/rejected result and account identity | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P2 | Search | `qq.search.stranger`, `qq.search.group`, `qq.search.contact`, `qq.search.message`, `qq.search.file` | `searchStranger/searchGroup/searchContact/searchMsgWithKeywords/searchFileWithKeywords` | `account_id`, `query?`, `keywords?`, `scope?`, `offset?`, `count?`, `filter?` | typed result list preserving UID/UIN/group/file/message identities | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P2 | Online state and likes | `qq.online.status`, `qq.online.devices`, `qq.online.likes`, `qq.online.set_like`, `qq.online.check_like` | `setStatus/getOnLineDev/getLikeList/setLikeStatus/checkLikeStatus` | `account_id`, `status?`, `device_id?`, `like_id?`, `like_type?`, `target_id?` | status/device/like result with account and target identity | Linux x86_64 + exact build/ABI; `NOT_RUN` |

## Cross-cutting contract profiles / 横切契约 profile

Each fixed operation selects one profile through the executable `mapping` value
in `QQOperation`. The profile is the request/result boundary for every operation
in that row; the native Host remains responsible for exact overload validation.

每个固定操作通过可执行 `QQOperation.mapping` 值选择一个 profile。该行就是该组所有操作的
请求/结果边界；准确 overload 校验仍由 native Host 负责。

| Mapping profile | Applies to | Request contract | Result/callback contract |
| --- | --- | --- | --- |
| `session` | session lifecycle | `account_id?`, `platform`, `client_version`, `data_dir`, `login_policy` | state/session/account/version/ABI; lifecycle callback |
| `login` | login state | account/UIN, `secret_ref?`, QR/poll fields | login state, account/UIN/UID, QR/progress; login callback |
| `account` | self identity | account/UIN/UID selector | account/UIN/UID/display name/profile |
| `message` | subscribe, recall, forward, multi-forward | account, peer/source/destination/message identity, ordered elements as applicable | message identity or accepted status; native completion callback as applicable |
| `send_message` | canonical send and callback record `qq.message.send_completion` | canonical conversation/content/reply plus QQ peer/elements | DeliveryResult, message ID/sequence/random/peer, completion callback; callback record is not requestable |
| `peer` | peer resolution | explicit UID/UIN/account selector | explicit UID/UIN/peer result |
| `lookup` | history/by-id/search | account, peer, message/sequence/filter/page fields | ordered typed messages with identity fields |
| `read` | read state | account, peer/message selectors | accepted status/read marker |
| `emoji` | emoji likes | account, message/like selectors | like list/count/status |
| `group` | group discovery | account, group/member selectors and paging | group/member identity, name, permissions |
| `friend` | friend discovery | account, friend selector and paging | friend/request identity and display data |
| `media` | media download | account, message/element/media selector and media options | bounded media reference, progress, local result ref, error; download callback |
| `file` | file operations | account, group/folder/file selector and source/destination | bounded file reference, metadata, progress, local result ref, error |
| `group_mutation` | group administration and approval | account, group/member/request target, decision or mutation fields | target identity, accepted/rejected status, reason |
| `friend_mutation` | friend request and remark mutation | account, request/friend target, decision/remark | request/friend identity, accepted/rejected status, reason |
| `profile` | profile mutation | account and field-specific profile values | field-level status and account identity |
| `search` | search operations | account, query/keywords/scope/filter/page | typed identity-preserving result list |
| `online` | online state and likes | account, status/device/like target | status/device/like result |

For canonical `message.connector.v1` sends, the optional `vendor_extension`
with `vendor=qq` may carry these identity facts: `qq_peer_uid`, `qq_peer_uin`,
`qq_group_code`, `qq_user_uid`, and `qq_user_uin`. The direct adapter copies
only these recognized facts into the native `peer` object and rejects duplicate
values; `conversation_id` remains the canonical connector identifier. Inbound
messages and accepted send results expose the same values as separate QQ facts
when the native Host supplies them.

对于 canonical `message.connector.v1` 发送请求，`vendor_extension` 可在
`vendor=qq` 时携带 `qq_peer_uid`、`qq_peer_uin`、`qq_group_code`、`qq_user_uid`、
`qq_user_uin` 这些身份事实。直连适配器只把已登记的事实复制到原生 `peer` 对象，重复值会被
拒绝；`conversation_id` 仍是 canonical connector 标识，不会被猜测成某一种 QQ ID。原生
Host 提供这些值时，入站消息和发送结果也会以独立 QQ fact 暴露。

## Shared safety rules / 共享安全规则

| Area / 领域 | Rule / 规则 | Evidence state / 证据状态 |
| --- | --- | --- |
| Correlation | `binding_id + generation + request_id`; UID, UIN, peer UID, group code, message ID, sequence, and random remain separate values. Callback records require a matching originating request, preserve peer identity fields independently, and are consumed once. Unknown event names are dropped. | Fake Host tested; real `NOT_RUN` |
| Timeout/cancel | Bounded deadline; cancellation sends a control frame; removed requests ignore late responses; no implicit side-effect retry. | Fake Host tested; real `NOT_RUN` |
| Account/session | Exact configured platform/build/ABI and optional expected account; missing or mismatched ready account fails closed. | Fake Host tested; real `NOT_RUN` |
| Media/files | Only HTTP(S) URI or binding-private QQ media reference crosses the canonical seam; no Product-local path or unbounded content. | Schema and mapper tested; real `NOT_RUN` |
| Security | `qq.login.password` accepts `secret_ref`, never `password`; diagnostics redact credential-like fields and are bounded. | Static/test boundary; real `NOT_RUN` |
| Process isolation | One binding owns one data directory and process group; shutdown reaps binding-local descendants and does not use TCP/WS/OneBot transport. | Fake Host tested; real `NOT_RUN` |
| Installation selection | Linux x86_64 operator path or one exact installation manifest; zero/multiple candidates and build drift fail closed. | Discovery tests; real `NOT_RUN` |
| Crash supervision | Unexpected exit uses a bounded binding-local restart budget and circuit; failed operations are never implicitly replayed. | Fake Host recovery/circuit tests; real `NOT_RUN` |

## Operation schemas / 操作 Schema

Every one of the 79 operation entries has its own request schema reference in
`plugin.manifest.json` under
`contracts/v1/schema.json#/$defs/<operation>_request`. The executable worker
allow-list and these schema property sets are checked for parity. The fields
are the conservative public envelope from `QQ_API_PLAN.md`; the authorized
Host remains responsible for exact target-client overload validation and native
message shapes.

Before IPC, the Python worker also validates primitive types, finite numeric
values, collection depth/size, and reserved fields. This keeps malformed JSON
and oversized nested values out of the native Host even when a caller bypasses
the repository's schema-validation tooling.

`plugin.manifest.json` 中 79 个 operation 都有独立的 request Schema 引用，格式为
`contracts/v1/schema.json#/$defs/<operation>_request`。可执行 Worker allow-list 与这些
Schema 字段会做 parity 校验。字段只取 `QQ_API_PLAN.md` 定义的保守公共 envelope；准确的
目标客户端 overload 校验与原生消息形状仍由授权 Host 负责。

Each operation also has an independent output schema reference in the manifest:
`contracts/v1/schema.json#/$defs/<operation>_response`. The response envelope fixes
the operation name, priority, and exact service/method mapping; its result selects
the operation's mapping profile. Profile result objects remain extensible because
the authorized Host owns exact overload-specific fields, while the Python worker
still rejects malformed, oversized, or credential-bearing values before IPC.

`plugin.manifest.json` 中每个 operation 也有独立的 output Schema 引用：
`contracts/v1/schema.json#/$defs/<operation>_response`。响应 envelope 固定 operation 名称、
优先级与精确 service/method 映射，result 再选择该 operation 的 mapping profile。由于准确的
overload 字段由授权 Host 负责，profile result 对象保留扩展性；Python Worker 仍会在 IPC 前
拒绝格式错误、超限或含凭据的数据。

在 IPC 之前，Python Worker 还会校验基础类型、有限数值、集合深度/大小与保留字段；即使调用方
绕过仓库的 Schema 校验工具，格式错误或过大的嵌套值也不会进入 native Host。

The additional getters in `QQ_SIDE_INTERFACES.md` (collection, album,
robot, ticket, setting, mini-app, third-party signature, and similar services)
are not claimed by this matrix. They require a separate fixed operation, schema,
exact-version mapping, authorization record, and test before being added.

`QQ_SIDE_INTERFACES.md` 中 collection、album、robot、ticket、setting、mini-app、
third-party signature 等额外 getter 不在本矩阵声明范围内。只有新增固定操作、Schema、精确版本
映射、授权记录和测试后，才可加入。
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# QQNT Direct API 映射矩阵

本矩阵是 src/qq_connector/qqnt_direct_operations.py 中可执行固定操作表的人工可读投影。capability 来源见 QQ_API_PLAN.md。计划中命名的每项操作都列于下表；额外的 qq.friend.set_remark 是明确登记的 adapter 扩展，不会用来隐藏任意原生调用。

## 目标与证据策略

首个受支持的真实目标平台是 Linux x86_64。部署必须提供书面授权记录、准确的 QQ 客户端 build 字符串和准确的 Host ABI。配置将 build 字符串记录为 required_client_version，将 ABI 记录为 required_host_abi；Host hello 则提供 client_version 和 abi。由于本仓库没有获授权的 QQ build 或原生 Host，所有真实 API 行当前都是 NOT_RUN。Fake Host 测试只能证明 worker 边界、映射机制和失败处理。

下表的请求/结果列描述规范化的 qq.client.v1 envelope，并非臆造的原生重载签名。某一行要改为 PASS，验收记录必须针对配置的 build 写明准确的原生签名、输入标识符类型、直接返回值、Listener 回调、超时/取消结果和观测到的错误；不得记录凭据、ticket、session 文件或私聊正文。

规范 message.connector.v1 mapper 当前只有四种内容：text、mention、image 和 file；reply 使用单独的 reply reference。原生 audio/video 元素不会被强制转换为其他类型。在规范 connector contract 定义对应内容类型之前，这些元素只通过固定 QQ media/file 操作边界提供。

## 覆盖矩阵

| 优先级 | QQ API 行 | 直连操作 | 原生 Service 与方法 | 请求字段 | 结果或回调 | 目标与证据 |
| --- | --- | --- | --- | --- | --- | --- |
| P0 | Session 生命周期 | qq.session.create、qq.session.init、qq.session.start_nt、qq.login.connect、qq.login.online、qq.login.offline | NodeIQQNTWrapperSession.create/init/startNT；NodeIKernelLoginService.connect/online/offline | account_id?、platform、client_version、data_dir、login_policy | state、session_id、account_id、client_version、abi；login/session 状态回调 | Linux x86_64 + 准确 build/ABI；NOT_RUN |
| P0 | 登录状态 | qq.login.list、qq.login.quick、qq.login.password、qq.login.qr、qq.login.poll | NodeIKernelLoginService.getLoginList/quickLoginWithUin/passwordLogin/getQRCodePicture/startPolling | account_id?、uin?、secret_ref?、qr_code?、poll_interval_seconds? | login_state、account_id、QQ UIN、用户 UID、显示名称、QR/进度结果；login 回调 | Linux x86_64 + 准确 build/ABI；密码只能通过 secret_ref；NOT_RUN |
| P0 | 登录状态观测 | qq.login.self_status | NodeIKernelProfileService.getSelfStatus | account_id?、uin?、uid? | login_state、account_id、QQ UIN、用户 UID、显示名称 | Linux x86_64 + 准确 build/ABI；NOT_RUN |
| P0 | 当前账号身份 | qq.account.core、qq.account.simple | NodeIKernelProfileService.getCoreAndBaseInfo/getUserSimpleInfo | account_id?、uid?、uin? | account ID、QQ UIN、用户 UID、显示名称、profile 结果 | Linux x86_64 + 准确 build/ABI；NOT_RUN |
| P0 | 接收消息 | qq.message.subscribe | NodeIKernelMsgService.addKernelMsgListener + NodeIKernelMsgListener.onRecvMsg | account_id?、events?、filter? | message.received 回调：账号、会话、message ID、sequence、random、时间戳、有序元素 | Linux x86_64 + 准确 build/ABI；NOT_RUN |
| P0 | 发送消息 | send_message、qq.message.send、qq.message.send_completion（仅回调） | NodeIKernelMsgService.sendMsg + onMsgInfoListUpdate | 规范 conversation/content/reply；peer、elements | 已接受的 DeliveryResult、原生 message ID、sequence、random、peer；完成回调 | Linux x86_64 + 准确 build/ABI；NOT_RUN |
| P0 | 会话对象解析 | qq.peer.uid_by_uin、qq.peer.uin_by_uid、qq.peer.uid、qq.peer.uin | NodeIKernelProfileService.getUidByUin/getUinByUid；NodeIKernelUixConvertService.getUid/getUin | account_id?、uid?、uin?、user_uid?、user_uin?、peer_uid? | 明确的 UID/UIN/peer identity 结果；不得有损替换 ID | Linux x86_64 + 准确 build/ABI；NOT_RUN |
| P1 | 消息查询 | qq.message.history_include_self、qq.message.history_by_seq、qq.message.by_id、qq.message.single、qq.message.search | getMsgsIncludeSelf/getMsgsBySeqAndCount/getMsgsByMsgId/getSingleMsg/queryMsgsWithFilterEx | account_id、peer、message_id?、sequence?、random?、offset?、count?、filter?、query? | 按顺序返回消息，并包含 message ID、sender、timestamp、sequence、random 和 elements | Linux x86_64 + 准确 build/ABI；NOT_RUN |
| P1 | 撤回与转发 | qq.message.recall、qq.message.forward、qq.message.forward_comment、qq.message.multi_forward | recallMsg/forwardMsg/forwardMsgWithComment/multiForwardMsg | account_id、source、destination、message_id?、message_ids?、comment?、messages? | accepted/rejected 结果、源/目标身份、生成的 message ID；存在时返回完成结果 | Linux x86_64 + 准确 build/ABI；NOT_RUN |
| P1 | 已读与表情点赞 | qq.message.read、qq.message.read_all、qq.message.emoji_likes、qq.message.emoji_likes_list | setMsgRead/setAllC2CAndGroupMsgRead/setMsgEmojiLikes/getMsgEmojiLikesList | account_id、peer?、message_id?、message_ids?、like_id?、like_type? | 状态、已读标记、点赞列表/数量和消息身份 | Linux x86_64 + 准确 build/ABI；NOT_RUN |
| P1 | 群发现 | qq.group.list、qq.group.detail、qq.group.members、qq.group.member | getGroupList/getGroupDetailInfo/getAllMemberList/getMemberInfo | account_id、group_id?、group_code?、member_uid?、member_uin?、offset?、count? | 群号、名称、权限、成员 UID/UIN 和显示数据 | Linux x86_64 + 准确 build/ABI；NOT_RUN |
| P1 | 好友发现 | qq.friend.list、qq.friend.cached、qq.friend.requests | getBuddyListV2/getBuddyListFromCache/getBuddyReq | account_id、uid?、uin?、offset?、count? | 好友 UID/UIN、显示数据、请求身份/状态 | Linux x86_64 + 准确 build/ABI；NOT_RUN |
| P1 | 媒体下载 | qq.media.element、qq.media.download、qq.media.video_url、qq.media.download_complete（仅回调） | getRichMediaElement/downloadRichMedia/getVideoPlayUrlV2/onRichMediaDownloadComplete | account_id、message_id?、element_id?、media_id?、media_type?、codec?、download? | element/file ID、媒体类型、进度、有界 remote_uri 或 QQ media ref、本地结果 ref、错误；下载回调 | Linux x86_64 + 准确 build/ABI；NOT_RUN |
| P1 | 文件 | qq.file.list、qq.file.search、qq.file.download、qq.file.forward、qq.file.save | getGroupFileList/searchFile/downloadFile/forwardFile/saveAs | account_id、group_id?、folder_id?、file_id?、file_uuid?、file_name?、query?、source?、destination? | 有界 file reference、名称/类型、进度、本地结果 ref、错误；文件回调 | Linux x86_64 + 准确 build/ABI；NOT_RUN |
| P2 | 群管理 | qq.group.modify_name、qq.group.modify_remark、qq.group.mute_member、qq.group.mute、qq.group.kick、qq.group.quit | modifyGroupName/modifyGroupRemark/setMemberShutUp/setGroupShutUp/kickMember/quitGroup | account_id、group_id、member_uid?、member_uin?、name?、remark?、duration_seconds? | accepted/rejected 状态、目标身份、原因；原生 API 发出时记录管理回调 | Linux x86_64 + 准确 build/ABI；NOT_RUN |
| P2 | 群申请审批 adapter | qq.group.approve | NodeIKernelGroupService.operateSysNotify | account_id、request_id、group_id?、user_id?、approve、comment?、vendor_request? | 已接受/拒绝的请求结果、请求/群/成员身份 | Linux x86_64 + 准确 build/ABI；NOT_RUN |
| P2 | 好友申请与备注 | qq.friend.approve、qq.friend.doubt_requests、qq.friend.approve_doubt、qq.friend.add、qq.friend.delete、qq.friend.set_remark | approvalFriendRequest/getDoubtBuddyReq/approvalDoubtBuddyReq/reqToAddFriends/delBuddy/setBuddyRemark | account_id、request_id?、uid?、uin?、approve?、comment?、remark? | 请求/好友身份、决定/状态、原因；原生 API 发出时记录请求回调 | Linux x86_64 + 准确 build/ABI；NOT_RUN |
| P2 | 资料修改 | qq.profile.modify、qq.profile.nickname、qq.profile.long_nick、qq.profile.birthday、qq.profile.gender、qq.profile.header | modifySelfProfile/setNickName/setLongNick/setBirthday/setGander/setHeader | account_id、profile?、nickname?、long_nick?、birthday?、gender?、header? | 字段级 accepted/rejected 结果和账号身份 | Linux x86_64 + 准确 build/ABI；NOT_RUN |
| P2 | 搜索 | qq.search.stranger、qq.search.group、qq.search.contact、qq.search.message、qq.search.file | searchStranger/searchGroup/searchContact/searchMsgWithKeywords/searchFileWithKeywords | account_id、query?、keywords?、scope?、offset?、count?、filter? | 保留 UID/UIN/group/file/message identity 的类型化结果列表 | Linux x86_64 + 准确 build/ABI；NOT_RUN |
| P2 | 在线状态与点赞 | qq.online.status、qq.online.devices、qq.online.likes、qq.online.set_like、qq.online.check_like | setStatus/getOnLineDev/getLikeList/setLikeStatus/checkLikeStatus | account_id、status?、device_id?、like_id?、like_type?、target_id? | 包含账号和目标身份的状态/设备/点赞结果 | Linux x86_64 + 准确 build/ABI；NOT_RUN |

## 横切契约 profile

每个固定操作通过 QQOperation 中可执行的 mapping 值选择一个 profile。此 profile 定义该行每个操作的 request/result 边界；native Host 仍负责准确的 overload 校验。

| Mapping profile | 适用范围 | Request 契约 | Result/callback 契约 |
| --- | --- | --- | --- |
| session | Session 生命周期 | account_id?、platform、client_version、data_dir、login_policy | state/session/account/version/ABI；生命周期回调 |
| login | 登录状态 | account/UIN、secret_ref?、QR/poll 字段 | login state、account/UIN/UID、QR/进度；login 回调 |
| account | 当前账号身份 | account/UIN/UID selector | account/UIN/UID/display name/profile |
| message | 订阅、撤回、转发、多消息转发 | account、peer/source/destination/message identity；适用时包含有序元素 | message identity 或 accepted status；适用时包含 native 完成回调 |
| send_message | 规范发送和回调记录 qq.message.send_completion | 规范 conversation/content/reply 加 QQ peer/elements | DeliveryResult、message ID/sequence/random/peer 和完成回调；回调记录不能作为 request 调用 |
| peer | 会话对象解析 | 明确的 UID/UIN/account selector | 明确的 UID/UIN/peer 结果 |
| lookup | 历史消息、按 ID 查询和搜索 | account、peer、message/sequence/filter/page 字段 | 包含 identity 字段的有序类型化消息 |
| read | 已读状态 | account、peer/message selector | accepted status/read marker |
| emoji | 表情点赞 | account、message/like selector | 点赞列表/数量/状态 |
| group | 群发现 | account、group/member selector 和分页参数 | 群/成员身份、名称、权限 |
| friend | 好友发现 | account、好友 selector 和分页参数 | 好友/请求身份和显示数据 |
| media | 媒体下载 | account、message/element/media selector 和媒体选项 | 有界媒体引用、进度、本地结果 ref、错误；下载回调 |
| file | 文件操作 | account、group/folder/file selector 和 source/destination | 有界文件引用、metadata、进度、本地结果 ref、错误 |
| group_mutation | 群管理和审批 | account、群/成员/请求目标、决定或修改字段 | 目标身份、accepted/rejected 状态、原因 |
| friend_mutation | 好友申请和备注修改 | account、请求/好友目标、决定/备注 | 请求/好友身份、accepted/rejected 状态、原因 |
| profile | 资料修改 | account 和字段专用 profile 值 | 字段级状态和账号身份 |
| search | 搜索操作 | account、query/keywords/scope/filter/page | 保留类型化 identity 的结果列表 |
| online | 在线状态和点赞 | account、status/device/like target | 状态/设备/点赞结果 |

canonical message.connector.v1 发送可选携带 vendor=qq 的 vendor_extension，其中可包含 qq_peer_uid、qq_peer_uin、qq_group_code、qq_user_uid 和 qq_user_uin 等身份事实。直连 adapter 只将这些已识别事实复制到原生 peer 对象，并拒绝重复值；conversation_id 仍是规范 connector 标识。原生 Host 提供这些值时，入站消息和已接受的发送结果也会将其作为单独的 QQ fact 暴露。

## 共享安全规则

| 领域 | 规则 | 证据状态 |
| --- | --- | --- |
| 关联 | 使用 binding_id + generation + request_id；UID、UIN、peer UID、群号、message ID、sequence 和 random 都是不同值。callback record 必须匹配发起它的 request，分别保留 peer identity 字段，且只消费一次。未知 event name 会被丢弃。 | Fake Host 已测试；真实接口 NOT_RUN |
| 超时/取消 | deadline 有界；取消会发送 control frame；被移除的 request 会忽略迟到响应；不会隐式重试有副作用的调用。 | Fake Host 已测试；真实接口 NOT_RUN |
| 账号/session | 必须精确匹配已配置的 platform/build/ABI 和可选的预期账号；ready account 缺失或不匹配时失败关闭。 | Fake Host 已测试；真实接口 NOT_RUN |
| 媒体/文件 | 只有 HTTP(S) URI 或 binding 私有 QQ media reference 能跨越规范边界；不能传递 Product 本地路径或无界内容。 | Schema 和 mapper 已测试；真实接口 NOT_RUN |
| 安全 | qq.login.password 接受 secret_ref，绝不接受 password；诊断会脱敏类似凭据的字段，并限制长度。 | 静态/测试边界；真实接口 NOT_RUN |
| 进程隔离 | 每个 binding 独占一个 data directory 和 process group；关闭时回收该 binding 的后代进程；不使用 TCP/WS/OneBot transport。 | Fake Host 已测试；真实接口 NOT_RUN |
| 安装选择 | 通过 Linux x86_64 operator 路径或唯一且精确的 installation manifest 选择；候选为零/多个或 build 漂移时失败关闭。 | Discovery 测试已覆盖；真实接口 NOT_RUN |
| 崩溃监管 | 意外退出时使用有界的 binding 本地重启预算和 circuit；失败操作绝不隐式 replay。 | Fake Host 恢复/circuit 测试已覆盖；真实接口 NOT_RUN |

## 操作 Schema

plugin.manifest.json 为 79 个操作分别引用自己的 request schema，形式为 contracts/v1/schema.json#/$defs/<operation>_request。可执行 worker allow-list 与这些 schema property set 会校验是否一致。字段采用 QQ_API_PLAN.md 所列的保守公共 envelope；授权 Host 仍负责对精确目标客户端 overload 和原生消息形状进行验证。

在 IPC 前，Python worker 还会校验基础类型、有限数值、集合深度/大小和保留字段。因此，即使调用方绕过仓库的 schema-validation tooling，格式错误的 JSON 和过大的嵌套值也不会到达 native Host。

manifest 还为每项操作单独引用 output schema：contracts/v1/schema.json#/$defs/<operation>_response。响应 envelope 固定操作名称、优先级和精确 service/method 映射；result 按操作选择 mapping profile。profile result object 保持可扩展，因为授权 Host 负责精确的 overload 字段；同时 Python worker 仍会在 IPC 前拒绝格式错误、超限或含凭据的值。

QQ_SIDE_INTERFACES.md 中额外列出的 getter（collection、album、robot、ticket、setting、mini-app、third-party signature 等 Service）不在本矩阵声明范围内。只有为其增加单独的固定操作、schema、精确版本映射、授权记录和测试后，才会纳入本矩阵。
