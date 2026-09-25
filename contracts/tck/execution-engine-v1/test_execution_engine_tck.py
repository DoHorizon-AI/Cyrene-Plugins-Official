"""execution.engine.v1 conformance tests. | 服务引擎契约测试。

Validates binding payloads against the owner-scoped schema independent of any
Product state, using a stand-in accelerator process for the lifecycle.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from vllm_runtime import RuntimeHandler, ServingRuntime

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCHEMA = json.loads(
    (
        REPOSITORY_ROOT
        / "plugins/serving/vllm-runtime/contracts/v1/schema.json"
    ).read_text(encoding="utf-8")
)
TOKEN = "execution-engine-tck-token-0123456789abcdef"


def _validator(definition: str) -> Draft202012Validator:
    resolver = Draft202012Validator(
        {"$ref": f"#/$defs/{definition}", "$defs": SCHEMA["$defs"]}
    )
    return resolver


def _standin_script(tmp_path: Path) -> Path:
    script = tmp_path / "standin_engine.py"
    script.write_text(
        "import json, sys\n"
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        "args = sys.argv[1:]\n"
        "port = int(args[args.index('--port') + 1])\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        body = {'object': 'list', 'data': []}\n"
        "        encoded = json.dumps(body).encode()\n"
        "        self.send_response(200 if self.path == '/v1/models' else 404)\n"
        "        self.send_header('Content-Type', 'application/json')\n"
        "        self.send_header('Content-Length', str(len(encoded)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(encoded)\n"
        "    def log_message(self, *args):\n"
        "        return\n"
        "ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()\n",
        encoding="utf-8",
    )
    return script


def _model_directory(tmp_path: Path) -> Path:
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text('{"model_type":"qwen2"}', encoding="utf-8")
    (model / "tokenizer.json").write_text('{"version":"1.0"}', encoding="utf-8")
    (model / "tokenizer_config.json").write_text(
        '{"chat_template":"{{ messages }}"}', encoding="utf-8"
    )
    (model / "model.safetensors").write_bytes(b"\x00" * 16)
    (model / "LICENSE").write_text("Apache-2.0\n", encoding="utf-8")
    return model


def _serve(tmp_path: Path) -> ThreadingHTTPServer:
    runtime = ServingRuntime(
        home=tmp_path / "runtime",
        artifact_root=tmp_path / "artifacts",
        command=["python3", str(_standin_script(tmp_path))],
        control_url="http://127.0.0.1:0",
        ready_timeout=30.0,
        poll_interval=0.2,
    )
    RuntimeHandler.runtime = runtime
    RuntimeHandler.token = TOKEN
    server = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeHandler)
    runtime.control_url = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _call(
    server: ThreadingHTTPServer | None,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    base_url: str | None = None,
    token: str = TOKEN,
) -> tuple[int, dict]:
    origin = base_url or f"http://127.0.0.1:{server.server_port}"  # type: ignore[union-attr]
    request = urllib.request.Request(
        origin + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_import_and_execution_payloads_match_the_contract(tmp_path: Path) -> None:
    server = _serve(tmp_path)
    try:
        model = _model_directory(tmp_path)
        status, imported = _call(
            server,
            "POST",
            "/imports",
            {"name": "tck", "source": {"kind": "LOCAL_PATH", "path": str(model)}},
        )
        assert status == 200
        _validator("ImportResponse").validate(imported)
        assert imported["modelArtifact"]["uri"].startswith("artifact://sha256/")

        status, replay = _call(
            server,
            "POST",
            "/imports",
            {"name": "tck", "source": {"kind": "LOCAL_PATH", "path": str(model)}},
        )
        assert status == 200
        assert replay["validation"]["digest"] == imported["validation"]["digest"]

        deployment_id = uuid4()
        status, execution = _call(
            server,
            "POST",
            f"/executions/{deployment_id}",
            {"modelArtifact": imported["modelArtifact"]},
        )
        assert status == 200, execution
        _validator("ExecutionResponse").validate(execution)
        assert execution["ready"] is True

        status, stopped = _call(server, "POST", f"/executions/{deployment_id}/stop")
        assert status == 200
        _validator("ExecutionResponse").validate(stopped)
        assert stopped["released"] is True
        assert stopped["ready"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_import_refuses_remote_code_and_incomplete_models(tmp_path: Path) -> None:
    server = _serve(tmp_path)
    try:
        model = _model_directory(tmp_path)
        status, refused = _call(
            server,
            "POST",
            "/imports",
            {
                "name": "tck",
                "source": {"kind": "LOCAL_PATH", "path": str(model)},
                "trustRemoteCode": True,
            },
        )
        assert status == 422
        assert refused["code"] == "SERVING_TRUST_REMOTE_CODE_FORBIDDEN"

        incomplete = tmp_path / "incomplete"
        incomplete.mkdir()
        status, rejected = _call(
            server,
            "POST",
            "/imports",
            {"name": "tck", "source": {"kind": "LOCAL_PATH", "path": str(incomplete)}},
        )
        assert status == 422
        assert rejected["code"] == "SERVING_MODEL_VALIDATION_FAILED"
    finally:
        server.shutdown()
        server.server_close()


def test_operator_binding_fails_closed_with_typed_errors(tmp_path: Path) -> None:
    """Probe an operator binding when explicitly configured; never silently skip.

        中文:仅在显式配置时探测操作者绑定;绝不静默跳过。"""

    base_url = os.environ.get("CYRENE_ENGINE_TCK_BASE_URL", "").rstrip("/")
    token_file = os.environ.get("CYRENE_ENGINE_TCK_TOKEN", "")
    if not base_url and not token_file:
        return
    if not base_url or not token_file:
        pytest.fail("set both CYRENE_ENGINE_TCK_BASE_URL and CYRENE_ENGINE_TCK_TOKEN")
    del tmp_path
    token = Path(token_file).read_text(encoding="utf-8").strip()
    status, body = _call(
        None,
        "GET",
        f"/executions/{uuid4()}",
        base_url=base_url,
        token=token,
    )
    assert status in {403, 404}, body
    _validator("EngineError").validate(body)