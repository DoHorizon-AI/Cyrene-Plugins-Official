#!/usr/bin/env python3
"""Compare the shared graph SDK with a separately generated protobuf view."""

from __future__ import annotations

import sys
from pathlib import Path

if len(sys.argv) != 2:
    raise SystemExit("usage: run_tck.py <generated-python-root>")

repo = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
sys.path.insert(0, str(repo / "sdk/python/cyrene_knowledge_graph_contracts/src"))

from cyrene.knowledge.graph.v1 import knowledge_graph_pb2 as generated_pb2
from cyrene_knowledge_graph_contracts import (
    encode_request,
    knowledge_graph_pb2,
)

node = knowledge_graph_pb2.GraphNode(
    node_id="node-1",
    tenant_id="tenant-a",
    namespace="conversation",
    kind="person",
    label="Morgan",
    metadata={"source_kind": "user-message"},
    source_refs=["message-8"],
)
request = knowledge_graph_pb2.UpsertNodesRequest(
    tenant_id="tenant-a", namespace="conversation", nodes=[node]
)
payload = encode_request("upsert_nodes", request)
independent = generated_pb2.UpsertNodesRequest.FromString(payload)
assert independent == request
assert independent.nodes[0].source_refs == ["message-8"]
assert generated_pb2.DESCRIPTOR.name == knowledge_graph_pb2.DESCRIPTOR.name

print("knowledge.graph.v1 Python SDK/generated binding TCK: PASS")
