"""Check shared and existing vendored tool bindings in both import orders."""

import os
import subprocess
import sys
from pathlib import Path


def test_shared_and_vendored_bindings_share_one_descriptor() -> None:
    repository_root = Path(__file__).resolve().parents[4]
    sdk_source = repository_root / "sdk/python/cyrene_tool_provider_contracts/src"
    script = """
import importlib.util
import sys
from pathlib import Path

root = Path.cwd()
paths = list(root.glob('plugins/**/tool_provider_pb2.py'))
assert paths, 'expected one or more existing tool.provider.v1 bindings'
if sys.argv[1] == 'shared-first':
    from cyrene_tool_provider_contracts import tool_provider_pb2
modules = []
for index, path in enumerate(paths):
    name = f'cyrene_legacy_tool_provider_{index}'
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    modules.append(module)
if sys.argv[1] == 'legacy-first':
    from cyrene_tool_provider_contracts import tool_provider_pb2
for module in modules:
    legacy_request = module.CallToolRequest(
        binding_id='binding-1',
        provider_tool_id='search',
        arguments_json='{"query":"cross-binding"}',
    )
    shared_request = tool_provider_pb2.CallToolRequest.FromString(
        legacy_request.SerializeToString(deterministic=True)
    )
    assert shared_request == legacy_request
    assert module.CallToolRequest.FromString(
        shared_request.SerializeToString(deterministic=True)
    ) == legacy_request
assert tool_provider_pb2.ToolProviderError is not None
"""
    environment = os.environ.copy()
    previous_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(sdk_source), previous_pythonpath) if part
    )
    for order in ("shared-first", "legacy-first"):
        subprocess.run(
            [sys.executable, "-c", script, order],
            cwd=repository_root,
            env=environment,
            check=True,
        )
