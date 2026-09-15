# QQNT Direct API Matrix / QQNT Direct API 映射矩阵

This matrix is the human-readable projection of
`qqnt_direct_operations.py`. `NOT_RUN` is intentional: the repository has no
authorized exact QQ Linux x64 build or native Host in CI. Fake Host coverage is
listed separately and does not promote an API row.

本矩阵是 `qqnt_direct_operations.py` 的人工可读投影。`NOT_RUN` 是有意保留的：当前仓库
没有获授权的精确 Linux x64 QQ build 和原生 Host，fake Host 测试不会把真实 API 行提升为
已验证。

| Priority | QQ API row / QQ API 行 | Direct operation(s) / 直连操作 | Native service and method / 原生 Service 与方法 | Canonical mapping / 规范映射 | Status |
| --- | --- | --- | --- | --- | --- |
| P0 | Session lifecycle | `qq.session.create`, `qq.session.init`, `qq.session.start_nt`, `qq.login.connect`, `qq.login.online`, `qq.login.offline` | `NodeIQQNTWrapperSession.create/init/startNT`; `NodeIKernelLoginService.connect/online/offline` | lifecycle state; no OneBot hop | `NOT_RUN` |
| P0 | Login state | `qq.login.list`, `qq.login.quick`, `qq.login.password`, `qq.login.qr`, `qq.login.poll`, `qq.login.self_status` | `NodeIKernelLoginService.getLoginList/quickLoginWithUin/passwordLogin/getQRCodePicture/startPolling/getSelfStatus` | `qq.client.v1` response; password accepts secret ref only | `NOT_RUN` |
| P0 | Self identity | `qq.account.core`, `qq.account.simple` | `NodeIKernelProfileService.getCoreAndBaseInfo/getUserSimpleInfo` | typed account identity | `NOT_RUN` |
| P0 | Receive messages | `qq.message.subscribe` | `NodeIKernelMsgService.addKernelMsgListener` + `NodeIKernelMsgListener.onRecvMsg` | `message.received` -> `inbound_message` | `NOT_RUN` |
| P0 | Send messages | `send_message`, `qq.message.send`, `qq.message.send_completion` | `NodeIKernelMsgService.sendMsg` + `onMsgInfoListUpdate` | private/group `send_message` -> accepted DeliveryResult | `NOT_RUN` |
| P0 | Peer resolution | `qq.peer.uid_by_uin`, `qq.peer.uin_by_uid`, `qq.peer.uid`, `qq.peer.uin` | Profile conversion and `NodeIKernelUixConvertService.getUid/getUin` | explicit UID/UIN/peer fields | `NOT_RUN` |
| P1 | Message lookup | `qq.message.history_include_self`, `qq.message.history_by_seq`, `qq.message.by_id`, `qq.message.single`, `qq.message.search` | `getMsgsIncludeSelf/getMsgsBySeqAndCount/getMsgsByMsgId/getSingleMsg/queryMsgsWithFilterEx` | `qq.client.v1` typed result | `NOT_RUN` |
| P1 | Recall and forward | `qq.message.recall`, `qq.message.forward`, `qq.message.forward_comment`, `qq.message.multi_forward` | `recallMsg/forwardMsg/forwardMsgWithComment/multiForwardMsg` | explicit message/source/destination identities | `NOT_RUN` |
| P1 | Read and emoji likes | `qq.message.read`, `qq.message.read_all`, `qq.message.emoji_likes`, `qq.message.emoji_likes_list` | `setMsgRead/setAllC2CAndGroupMsgRead/setMsgEmojiLikes/getMsgEmojiLikesList` | `qq.client.v1` typed result | `NOT_RUN` |
| P1 | Group discovery | `qq.group.list`, `qq.group.detail`, `qq.group.members`, `qq.group.member` | `getGroupList/getGroupDetailInfo/getAllMemberList/getMemberInfo` | explicit group/member identities | `NOT_RUN` |
| P1 | Friend discovery | `qq.friend.list`, `qq.friend.cached`, `qq.friend.requests` | `getBuddyListV2/getBuddyListFromCache/getBuddyReq` | explicit account/friend/request identities | `NOT_RUN` |
| P1 | Media download | `qq.media.element`, `qq.media.download`, `qq.media.video_url`, `qq.media.download_complete` | `getRichMediaElement/downloadRichMedia/getVideoPlayUrlV2/onRichMediaDownloadComplete` | bounded vendor media reference | `NOT_RUN` |
| P1 | Files | `qq.file.list`, `qq.file.search`, `qq.file.download`, `qq.file.forward`, `qq.file.save` | `getGroupFileList/searchFile/downloadFile/forwardFile/saveAs` | bounded file reference and result | `NOT_RUN` |
| P2 | Group administration | `qq.group.modify_name`, `qq.group.modify_remark`, `qq.group.mute_member`, `qq.group.mute`, `qq.group.kick`, `qq.group.quit` | `modifyGroupName/modifyGroupRemark/setMemberShutUp/setGroupShutUp/kickMember/quitGroup` | explicit group/member target | `NOT_RUN` |
| P2 | Group join approval adapter | `qq.group.approve` | `NodeIKernelGroupService.operateSysNotify` | `respond_request(group_invite)` -> group notification operation | `NOT_RUN` |
| P2 | Friend requests | `qq.friend.approve`, `qq.friend.doubt_requests`, `qq.friend.approve_doubt`, `qq.friend.add`, `qq.friend.delete` | `approvalFriendRequest/getDoubtBuddyReq/approvalDoubtBuddyReq/reqToAddFriends/delBuddy` | request identity and decision | `NOT_RUN` |
| P2 | Profile mutation | `qq.profile.modify`, `qq.profile.nickname`, `qq.profile.long_nick`, `qq.profile.birthday`, `qq.profile.gender`, `qq.profile.header` | `modifySelfProfile/setNickName/setLongNick/setBirthday/setGander/setHeader` | explicit field mutation result | `NOT_RUN` |
| P2 | Search | `qq.search.stranger`, `qq.search.group`, `qq.search.contact`, `qq.search.message`, `qq.search.file` | `searchStranger/searchGroup/searchContact/searchMsgWithKeywords/searchFileWithKeywords` | `qq.client.v1` typed result | `NOT_RUN` |
| P2 | Online state and likes | `qq.online.status`, `qq.online.devices`, `qq.online.likes`, `qq.online.set_like`, `qq.online.check_like` | `setStatus/getOnLineDev/getLikeList/setLikeStatus/checkLikeStatus` | explicit account/device/like result | `NOT_RUN` |

## Cross-cutting behavior / 横切行为

| Area / 领域 | Request contract / 请求契约 | Result and callback / 结果与回调 | Evidence state / 证据状态 |
| --- | --- | --- | --- |
| Correlation | `binding_id + generation + request_id`; UID, UIN, peer UID, group code, message ID, sequence, and random are separate values | direct response or fixed callback event; duplicate late responses ignored | fake Host tested; real `NOT_RUN` |
| Timeout/cancel | bounded operation deadline; cancellation sends a control frame | `TIMEOUT` or `CANCELLED`, no implicit side-effect retry | fake Host tested; real `NOT_RUN` |
| Account/session | exact configured client version, platform, and optional expected account | hello compatibility report; account mismatch fails closed | fake Host tested; real `NOT_RUN` |
| Media/files | HTTP(S) URI or binding-private QQ media reference only; no Product-local path | bounded result/reference, no private content in diagnostics | schema and mapper tested; real `NOT_RUN` |
| Security | `qq.login.password` accepts `secret_ref`, never `password` | no credentials, tickets, or session files in package metadata/logs | static/test boundary; real `NOT_RUN` |

The additional getters in `QQ_SIDE_INTERFACES.md` (collection, album, robot,
ticket, setting, mini-app, third-party signature, and similar services) are not
claimed by this matrix. They are future scope until a separate fixed operation,
schema, exact-version mapping, authorization record, and test exists.

`QQ_SIDE_INTERFACES.md` 中的 collection、album、robot、ticket、setting、mini-app、
third-party signature 等额外 getter 不在本矩阵声明范围内。只有新增固定操作、Schema、
精确版本映射、授权记录和测试后，才可进入后续范围。
