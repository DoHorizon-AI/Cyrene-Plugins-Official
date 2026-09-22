"""Diagnostics capture tests for the vLLM serving runtime."""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from cy_artifacts import ArtifactKind, LocalArtifactProvider
from vllm_runtime import RuntimeHandler, ServingDiagnostics, ServingRuntime

TOKEN = "serving-runtime-token-0123456789abcdef"


def _chatty_script(tmp_path: Path) -> Path:
    """A stand-in vLLM that writes to both streams before serving."""

    script = tmp_path / "chatty_vllm.py"
    script.write_text(
        "import json, sys\n"
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        "args = sys.argv[1:]\n"
        "port = int(args[args.index('--port') + 1])\n"
        "print('INFO: loading weights')\n"
        "print('ERROR: cuda kernel missing', file=sys.stderr)\n"
        "sys.stdout.flush()\n"
        "sys.stderr.flush()\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def _json(self, status, body):\n"
        "        encoded = json.dumps(body).encode()\n"
        "        self.send_response(status)\n"
        "        self.send_header('Content-Type', 'application/json')\n"
        "        self.send_header('Content-Length', str(len(encoded)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(encoded)\n"
        "    def do_GET(self):\n"
        "        self._json(200, {'object': 'list', 'data': [], 'path': self.path})\n"
        "    def log_message(self, *args):\n"
        "        return\n"
        "ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()\n",
        encoding="utf-8",
    )
    return script


def _artifact(tmp_path: Path) -> dict:
    provider = LocalArtifactProvider(tmp_path / "artifacts")
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text('{"model_type":"llama"}', encoding="utf-8")
    return provider.publish_portable_directory(model, kind=ArtifactKind("model")).to_dict()


def test_sink_tags_streams_and_rotates_within_the_budget(tmp_path: Path) -> None:
    sink = ServingDiagnostics(tmp_path, "dep-1")
    sink.start()
    sink._pending.put(("stdout", "loading weights"))
    sink._pending.put(("stderr", "kernel missing"))
    assert sink.close() is False

    records = [json.loads(line) for line in (tmp_path / "dep-1.ndjson").read_text().splitlines()]
    assert [record["stream"] for record in records] == ["stdout", "stderr"]
    # Sequence is 1-based so a client can page from 0 without missing record one.
    assert [record["sequence"] for record in records] == [1, 2]
    assert records[1]["level"] == "warn"

    page, degraded = sink.page(after_sequence=0)
    assert [item["sequence"] for item in page] == [1, 2]
    assert degraded is False
    after, _ = sink.page(after_sequence=1)
    assert [item["sequence"] for item in after] == [2]

    # Exceeding the budget rotates into the second retained file.
    rotating = ServingDiagnostics(tmp_path, "dep-2", budget_bytes=1)
    rotating.start()
    rotating._pending.put(("stderr", "before rotation"))
    rotating._pending.put(("stderr", "after rotation"))
    assert rotating.close() is False
    rotated = [
        json.loads(line) for line in (tmp_path / "dep-2.ndjson.1").read_text().splitlines()
    ]
    current = [json.loads(line) for line in (tmp_path / "dep-2.ndjson").read_text().splitlines()]
    assert [item["message"] for item in rotated] == ["before rotation"]
    assert [item["message"] for item in current] == ["after rotation"]
    # Sequences stay monotonic across the rotation.
    assert current[0]["sequence"] == rotated[0]["sequence"] + 1


def test_sink_degrades_instead_of_blocking_when_unwritable(tmp_path: Path) -> None:
    (tmp_path / "dep-3.ndjson").mkdir()
    sink = ServingDiagnostics(tmp_path, "dep-3")
    sink.start()
    for index in range(50):
        sink._pending.put(("stdout", f"line {index}"))
    assert sink.close() is True
    assert (tmp_path / "dep-3.degraded").exists()


def test_diagnostics_route_reports_both_streams(tmp_path: Path) -> None:
    runtime = ServingRuntime(
        home=tmp_path / "runtime",
        artifact_root=tmp_path / "artifacts",
        command=["python3", str(_chatty_script(tmp_path))],
        control_url="http://127.0.0.1:0",
        ready_timeout=30.0,
        poll_interval=0.2,
    )
    RuntimeHandler.runtime = runtime
    RuntimeHandler.token = TOKEN
    server = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    runtime.control_url = f"http://127.0.0.1:{server.server_port}"
    deployment_id = uuid4()

    def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        import urllib.error
        import urllib.request

        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}{path}",
            data=data,
            headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    try:
        status, started = call("POST", f"/executions/{deployment_id}", {"modelArtifact": _artifact(tmp_path)})
        assert status == 200, started

        status, page = call("GET", f"/executions/{deployment_id}/diagnostics?afterSequence=0&limit=200")
        assert status == 200
        assert page["resourceId"] == str(deployment_id)
        assert page["terminal"] is False
        assert page["diagnosticsDegraded"] is False
        streams = {item["stream"] for item in page["items"]}
        assert {"stdout", "stderr"} <= streams
        messages = " ".join(item["message"] for item in page["items"])
        assert "cuda kernel missing" in messages

        # Reading `self.path` with a query must not swallow the query for the
        # upstream call, so the proxy keeps forwarding it verbatim.
        status, proxied = call("GET", f"/serving/{deployment_id}/v1/models?detailed=true")
        assert status == 200
        assert proxied["path"] == "/v1/models?detailed=true"

        status, _stopped = call("POST", f"/executions/{deployment_id}/stop")
        assert status == 200
        status, page = call("GET", f"/executions/{deployment_id}/diagnostics")
        assert status == 200
        assert page["terminal"] is True

        status, _unknown = call("GET", f"/executions/{uuid4()}/diagnostics")
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
