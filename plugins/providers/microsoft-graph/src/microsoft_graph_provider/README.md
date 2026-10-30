# Microsoft Graph Provider Source

This directory contains the stateless provider implementation. The plugin
validates Exchange identity context, reads the actor-owned Account Center
profile, redeems a one-use lease, and verifies the signed-in Graph user before
calling a fixed v1.0 route. The v1 Planner/SharePoint scope remains exact; v2
adds actor/workspace-bound To Do, calendar, and SharePoint library metadata.

| File | Responsibility |
| --- | --- |
| `settings.py` | Closed host activation settings and private secret resolution. |
| `account_center.py` | Profile lookup and one-use delegated credential redemption. |
| `trust.py` | Exchange context verification and Account Center signing. |
| `microsoft_graph_api.py` | Fixed Graph v1.0 transport, pagination, projections, and write reconciliation. |
| `plugin.py` | Tool catalog, argument checks, scope checks, and invocation routing. |
| `../contracts/office-v2-target-scope.schema.json` | Exact v2 actor-owned resource scope and privacy flags. |
| `../contracts/office-v2-operations.json` | Canonical operation IDs, write classification, result projections, and paging semantics. |

Execution order: `settings.py` → `trust.py` → `account_center.py` → `microsoft_graph_api.py` → `plugin.py`.

Planner plan discovery, To Do list discovery, and calendar list discovery return metadata for the signed-in actor only. Empty selected-ID lists authorize only those discovery calls. Detail and write calls require a selected ID. Calendar event create/update/delete never accept attendees; update/delete reject existing events that have attendees. SharePoint drive items are read through the exact selected site and drive path and return metadata/links only.

计划发现、To Do 列表发现和日历列表发现仅返回当前签名用户可见的元数据。所选 ID 为空时，只能调用对应的发现工具；详情和写入必须先选择精确资源 ID。日历事件创建、更新和删除均不接收参会者；更新或删除前会拒绝已有参会者的事件。SharePoint 文档库项目通过已选站点与文档库固定路径读取，只返回元数据和原链接。
