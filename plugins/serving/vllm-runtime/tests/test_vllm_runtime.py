"""Serving runtime contract tests for Reactor serving bindings."""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
from cy_artifacts import ArtifactKind, LocalArtifactProvider
from vllm_runtime import RuntimeHandler, ServingRuntime, ServingRuntimeError

TOKEN = "serving-runtime-token-0123456789abcdef"


class FakeVllmHandler(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible peer that stands in for a vLLM process."""

    def do_GET(self) -> None:
        if self.path == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": "reactor", "object": "model"}]})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/v1/chat/completions":
            self._json(200, {"choices": [{"message": {"role": "assistant", "content": "served"}}]})
            return
        self._json(404, {"error": "not found"})

    def _json(self, status: int, body: dict) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _fake_vllm_script(tmp_path: Path) -> Path:
    """Write a launcher that starts the fake peer on the requested port."""

    script = tmp_path / "fake_vllm.py"
    script.write_text(
        "import json, sys\n"
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        "args = sys.argv[1:]\n"
        "port = int(args[args.index('--port') + 1])\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def _json(self, status, body):\n"
        "        encoded = json.dumps(body).encode()\n"
        "        self.send_response(status)\n"
        "        self.send_header('Content-Type', 'application/json')\n"
        "        self.send_header('Content-Length', str(len(encoded)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(encoded)\n"
        "    def do_GET(self):\n"
        "        if self.path == '/v1/models':\n"
        "            self._json(200, {'object': 'list', 'data': []})\n"
        "        else:\n"
        "            self._json(404, {'error': 'not found'})\n"
        "    def do_POST(self):\n"
        "        self._json(200, {'choices': [{'message': {'role': 'assistant', 'content': 'served'}}]})\n"
        "    def log_message(self, *args):\n"
        "        return\n"
        "ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()\n",
        encoding="utf-8",
    )
    return script


def _artifact(tmp_path: Path) -> tuple[Path, dict]:
    provider = LocalArtifactProvider(tmp_path / "artifacts")
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text('{"model_type":"llama"}', encoding="utf-8")
    reference = provider.publish_portable_directory(model, kind=ArtifactKind("model"))
    return tmp_path / "artifacts", reference.to_dict()


def _runtime(tmp_path: Path, command: list[str], port: int) -> ServingRuntime:
    return ServingRuntime(
        home=tmp_path / "runtime",
        artifact_root=tmp_path / "artifacts",
        command=command,
        control_url=f"http://127.0.0.1:{port}",
        ready_timeout=30.0,
        poll_interval=0.2,
    )


def _serve(runtime: ServingRuntime) -> ThreadingHTTPServer:
    RuntimeHandler.runtime = runtime
    RuntimeHandler.token = TOKEN
    server = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _request(
    server: ThreadingHTTPServer,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    token: str = TOKEN,
) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}{path}",
        data=data,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_execution_lifecycle_serves_and_releases(tmp_path: Path) -> None:
    _, artifact = _artifact(tmp_path)
    script = _fake_vllm_script(tmp_path)
    runtime = _runtime(tmp_path, ["python3", str(script)], 0)
    server = _serve(runtime)
    deployment_id = uuid4()
    runtime.control_url = f"http://127.0.0.1:{server.server_port}"
    try:
        status, started = _request(
            server, "POST", f"/executions/{deployment_id}", {"modelArtifact": artifact}
        )
        assert status == 200, started
        assert started["ready"] is True
        assert started["servedModel"] == f"reactor-{deployment_id}"
        assert started["endpointUrl"].endswith(f"/serving/{deployment_id}/v1")

        status, inspected = _request(server, "GET", f"/executions/{deployment_id}")
        assert status == 200
        assert inspected["modelArtifact"] == artifact
        assert inspected["ready"] is True

        status, completion = _request(
            server,
            "POST",
            f"/serving/{deployment_id}/v1/chat/completions",
            {"model": f"reactor-{deployment_id}", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert status == 200, completion
        assert completion["choices"][0]["message"]["content"] == "served"

        status, stopped = _request(server, "POST", f"/executions/{deployment_id}/stop")
        assert status == 200
        assert stopped["released"] is True
        assert stopped["ready"] is False
        assert runtime.inspect(deployment_id)["ready"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_execution_requires_the_binding_credential(tmp_path: Path) -> None:
    _, artifact = _artifact(tmp_path)
    runtime = _runtime(tmp_path, ["python3", str(_fake_vllm_script(tmp_path))], 0)
    server = _serve(runtime)
    try:
        status, body = _request(
            server, "POST", f"/executions/{uuid4()}", {"modelArtifact": artifact}, token="wrong"
        )
        assert status == 403
        assert body["code"] == "SERVING_BINDING_PERMISSION_DENIED"
        assert _request(server, "GET", "/healthz", token="wrong")[0] == 200
    finally:
        server.shutdown()
        server.server_close()


def test_unknown_execution_fails_closed(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, ["python3", str(_fake_vllm_script(tmp_path))], 0)
    server = _serve(runtime)
    try:
        status, body = _request(server, "GET", f"/executions/{uuid4()}")
        assert status == 404
        assert body["code"] == "SERVING_EXECUTION_NOT_FOUND"
    finally:
        server.shutdown()
        server.server_close()


def _model_directory(tmp_path: Path) -> Path:
    """Create the minimum servable text-model directory for import tests."""

    model = tmp_path / "candidate"
    model.mkdir()
    (model / "config.json").write_text('{"model_type":"qwen2"}', encoding="utf-8")
    (model / "tokenizer.json").write_text('{"version":"1.0"}', encoding="utf-8")
    (model / "tokenizer_config.json").write_text(
        '{"chat_template":"{{ messages }}"}', encoding="utf-8"
    )
    (model / "model.safetensors").write_bytes(b"\x00" * 32)
    (model / "LICENSE").write_text("Apache-2.0\n", encoding="utf-8")
    return model


def test_model_import_validates_and_publishes_a_local_directory(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, ["python3", "unused"], 0)
    server = _serve(runtime)
    try:
        model = _model_directory(tmp_path)
        payload = {
            "name": "candidate",
            "source": {"kind": "LOCAL_PATH", "path": str(model)},
        }
        status, imported = _request(server, "POST", "/imports", payload)
        assert status == 200, imported
        assert imported["validation"]["weights"] is True
        assert imported["validation"]["config"] is True
        assert imported["validation"]["tokenizer"] is True
        assert imported["validation"]["chatTemplate"] is True
        assert imported["validation"]["trustRemoteCode"] is False
        assert imported["validation"]["license"] == "Apache-2.0"
        assert imported["validation"]["digest"].startswith("sha256:")
        assert imported["modelArtifact"]["kind"] == "model"
        assert imported["modelArtifact"]["uri"].startswith("artifact://sha256/")

        status, replay = _request(server, "POST", "/imports", payload)
        assert status == 200
        assert replay["modelArtifact"] == imported["modelArtifact"]
        assert replay["validation"]["digest"] == imported["validation"]["digest"]
    finally:
        server.shutdown()
        server.server_close()


def test_model_import_fails_closed_without_a_complete_model(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, ["python3", "unused"], 0)
    server = _serve(runtime)
    try:
        empty = tmp_path / "empty"
        empty.mkdir()
        status, body = _request(
            server,
            "POST",
            "/imports",
            {"name": "empty", "source": {"kind": "LOCAL_PATH", "path": str(empty)}},
        )
        assert status == 422
        assert body["code"] == "SERVING_MODEL_VALIDATION_FAILED"

        status, body = _request(
            server,
            "POST",
            "/imports",
            {
                "name": "remote-code",
                "source": {"kind": "LOCAL_PATH", "path": str(empty)},
                "trustRemoteCode": True,
            },
        )
        assert status == 422
        assert body["code"] == "SERVING_TRUST_REMOTE_CODE_FORBIDDEN"

        status, body = _request(
            server,
            "POST",
            "/imports",
            {"name": "relative", "source": {"kind": "LOCAL_PATH", "path": "models/candidate"}},
        )
        assert status == 422
        assert body["code"] == "SERVING_MODEL_SOURCE_INVALID"
    finally:
        server.shutdown()
        server.server_close()


def test_model_import_resolves_private_credentials_for_hugging_face(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path, ["python3", "unused"], 0)
    marker = runtime.credentials / hashlib.sha256(b"org/private").hexdigest()
    marker.write_text("hf-token-value", encoding="utf-8")
    marker.chmod(0o600)
    seen: dict[str, object] = {}

    def fake_download(source: dict, token: str | None) -> Path:
        seen["source"] = source
        seen["token"] = token
        return _model_directory(tmp_path)

    monkeypatch.setattr(runtime, "_download_hugging_face", fake_download)
    revision = "a" * 40
    result = runtime.import_model(
        {
            "name": "private",
            "source": {
                "kind": "HUGGING_FACE",
                "repository": "org/private",
                "revision": revision,
            },
            "credentialRef": "org/private",
        }
    )
    assert seen["token"] == "hf-token-value"
    assert result["validation"]["provenance"] == f"huggingface:org/private@{revision}"

    with pytest.raises(ServingRuntimeError) as failure:
        runtime.import_model(
            {
                "name": "unresolved",
                "source": {
                    "kind": "HUGGING_FACE",
                    "repository": "org/private",
                    "revision": revision,
                },
                "credentialRef": "org/missing",
            }
        )
    assert failure.value.code == "SERVING_CREDENTIAL_REF_UNRESOLVED"
