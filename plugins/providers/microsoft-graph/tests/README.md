# Microsoft Graph Provider Tests

The tests use synthetic user IDs, profile scopes, cursor tokens, and fake HTTPS
responses. They never read developer credentials or call Microsoft Graph.

Tests focus on the provider boundary: same-token `/me` identity validation,
fixed endpoint construction, bounded pages, profile-bound cursors, explicit
authorization errors, ETag conflicts, and safe handling of ambiguous creates.

测试使用合成用户 ID、账号范围、分页令牌和假 HTTPS 响应，不读取开发者凭据，也不调用
Microsoft Graph。测试聚焦供应商边界、同令牌 `/me` 身份校验、固定路由、有界分页、租户与
用户绑定的游标、明确授权错误、ETag 冲突和不确定创建的安全处理。
