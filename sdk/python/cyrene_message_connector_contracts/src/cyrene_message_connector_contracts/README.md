# SDK package

`codec.py` validates method-aware protobuf and JSON payloads;
`_generated/message_connector_pb2.py` contains the frozen protocol binding;
`schemas/` carries the two canonical JSON schemas for `inbound_request` and
`respond_request`.

`codec.py` 校验按方法区分的 protobuf 与 JSON payload；
`_generated/message_connector_pb2.py` 保存冻结协议绑定；`schemas/` 收录
`inbound_request` 和 `respond_request` 的两个规范 JSON schema。
