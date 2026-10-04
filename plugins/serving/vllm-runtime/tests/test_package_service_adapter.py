"""Loopback tests for the HTTP package lifecycle adapter.

中文：验证 HTTP 生命周期适配器的 readiness、metadata、认证和本地模型导入。
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import package_service_adapter as adapter

TOKEN = "adapter-test-serving-token-0123456789"
PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _model_directory(root: Path) -> Path:
    model = root / "candidate-model"
    model.mkdir()
    (model / "config.json").write_text('{"model_type":"qwen2"}', encoding="utf-8")
    (model / "tokenizer.json").write_text('{"version":"1.0"}', encoding="utf-8")
    (model / "tokenizer_config.json").write_text(
        '{"chat_template":"{{ messages }}"}', encoding="utf-8"
    )
    (model / "model.safetensors").write_bytes(b"\x00" * 32)
    (model / "LICENSE").write_text("Apache-2.0\n", encoding="utf-8")
    return model


def _request(
    connection_ref: str,
    method: str,
    path: str,
    *,
    body: dict[str, object] | None = None,
    token: str | None = TOKEN,
) -> tuple[int, dict[str, object]]:
    host, port = adapter.decode_connection_ref(connection_ref)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"http://{host}:{port}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _environment(tmp_path: Path) -> dict[str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    runtime_home = tmp_path / "owner-runtime-home"
    artifact_root = tmp_path / "owner-artifact-root"
    artifact_root.mkdir()
    credential_file = tmp_path / "serving.token"
    credential_file.write_text(TOKEN + "\n", encoding="utf-8")
    credential_file.chmod(0o600)
    return {
        "CYRENE_VLLM_RUNTIME_HOME": str(runtime_home),
        "CYRENE_ARTIFACT_ROOT": str(artifact_root),
        "CYRENE_SERVING_CREDENTIAL_FILE": str(credential_file),
        "CYRENE_SERVING_CONTROL_URL": "http://127.0.0.1:19400",
    }


class PackageServiceAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cyrene-vllm-adapter-test-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_adapter_starts_http_service_and_imports_a_local_model(self) -> None:
        server, worker, ready = adapter.start_service(
            [
                "--capability",
                adapter.CAPABILITY_ID,
                "--interface-version",
                adapter.INTERFACE_VERSION,
                "--listen",
                "127.0.0.1:0",
            ],
            environ=_environment(self.root),
            manifest_path=PACKAGE_ROOT / "plugin.manifest.json",
        )
        try:
            self.assertEqual(ready["event"], "direct_plugin_ready")
            self.assertEqual(ready["capability"], "execution.engine.v1")
            self.assertEqual(ready["interfaceVersion"], "1")
            host, port = adapter.decode_connection_ref(ready["connection_ref"])
            self.assertEqual((host, port), server.server_address[:2])

            status, health = _request(ready["connection_ref"], "GET", "/readyz", token=None)
            self.assertEqual(status, 200)
            self.assertEqual(health, {"status": "ok"})

            status, denied = _request(ready["connection_ref"], "GET", "/metadata", token=None)
            self.assertEqual(status, 403)
            self.assertEqual(denied["code"], "SERVING_BINDING_PERMISSION_DENIED")

            status, metadata = _request(ready["connection_ref"], "GET", "/metadata")
            self.assertEqual(status, 200)
            self.assertEqual(
                metadata,
                {
                    "package_id": "cyrene.serving.vllm-runtime",
                    "package_version": "0.1.0",
                    "capability": "execution.engine.v1",
                    "interface_version": "1",
                    "transport": "http",
                },
            )

            model = _model_directory(self.root)
            status, imported = _request(
                ready["connection_ref"],
                "POST",
                "/imports",
                body={
                    "name": "candidate-model",
                    "source": {"kind": "LOCAL_PATH", "path": str(model)},
                },
            )
            self.assertEqual(status, 200, imported)
            self.assertIs(imported["validation"]["weights"], True)
            self.assertEqual(imported["validation"]["provenance"], f"local:{model}")
            self.assertEqual(imported["modelArtifact"]["kind"], "model")
            self.assertTrue(imported["modelArtifact"]["uri"].startswith("artifact://sha256/"))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_cli_emits_host_readiness_after_the_http_health_probe(self) -> None:
        environment = _environment(self.root)
        if "PYTHONPATH" in os.environ:
            environment["PYTHONPATH"] = os.environ["PYTHONPATH"]
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(PACKAGE_ROOT / "package_service_adapter.py"),
                "--capability",
                adapter.CAPABILITY_ID,
                "--interface-version",
                adapter.INTERFACE_VERSION,
                "--listen",
                "127.0.0.1:0",
            ],
            cwd=PACKAGE_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertIsNotNone(process.stdout)
            readable, _, _ = select.select([process.stdout], [], [], 5.0)
            if not readable:
                self.fail(
                    f"adapter did not emit readiness; process status={process.poll()}"
                )
            line = process.stdout.readline()
            descriptor = json.loads(line)
            self.assertEqual(
                set(descriptor),
                {"event", "capability", "interfaceVersion", "connection_ref"},
            )
            self.assertEqual(descriptor["event"], "direct_plugin_ready")
            self.assertEqual(descriptor["capability"], adapter.CAPABILITY_ID)
            self.assertEqual(descriptor["interfaceVersion"], adapter.INTERFACE_VERSION)
            status, health = _request(
                descriptor["connection_ref"], "GET", "/readyz", token=None
            )
            self.assertEqual(status, 200)
            self.assertEqual(health, {"status": "ok"})
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def test_connection_ref_decoder_rejects_untyped_or_untrusted_urls(self) -> None:
        invalid = (
            "http://127.0.0.1:1234",
            "cyrene-http-v1://localhost:1234",
            "cyrene-http-v1://127.0.0.1:1234/path",
            "cyrene-http-v1://user@127.0.0.1:1234",
            "cyrene-http-v1://127.0.0.1:1234?token=secret",
            "cyrene-http-v1://192.0.2.1:1234",
            "cyrene-http-v1://127.0.0.1:0",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaisesRegex(
                adapter.AdapterError, "SERVING_CONNECTION_REF_INVALID"
            ):
                adapter.decode_connection_ref(value)

    def test_manifest_must_bind_http_transport_and_package_capability(self) -> None:
        manifest = adapter._read_manifest(PACKAGE_ROOT / "plugin.manifest.json")
        metadata = adapter.package_metadata(manifest)
        self.assertEqual(metadata["transport"], "http")
        changed = json.loads(json.dumps(manifest))
        changed["runtime"]["transport"] = "grpc"
        with self.assertRaisesRegex(
            adapter.AdapterError, "SERVING_PACKAGE_CONTRACT_MISMATCH"
        ):
            adapter.package_metadata(changed)

    def test_owner_configuration_rejects_relative_paths_and_external_plain_http(self) -> None:
        environment = _environment(self.root)
        environment["CYRENE_ARTIFACT_ROOT"] = "relative/artifacts"
        with self.assertRaisesRegex(
            adapter.AdapterError, "SERVING_OWNER_PATHS_MUST_BE_ABSOLUTE"
        ):
            adapter._required_environment(environment)

        environment = _environment(self.root / "second")
        environment["CYRENE_SERVING_CONTROL_URL"] = "http://203.0.113.9:8080"
        with self.assertRaisesRegex(adapter.AdapterError, "SERVING_CONTROL_URL_INVALID"):
            adapter._required_environment(environment)

    def test_activation_rejects_a_credential_file_that_is_not_private_mode_0600(self) -> None:
        environment = _environment(self.root)
        Path(environment["CYRENE_SERVING_CREDENTIAL_FILE"]).chmod(0o640)
        with self.assertRaisesRegex(adapter.AdapterError, "SERVING_CREDENTIAL_INVALID"):
            adapter.start_service(
                [
                    "--capability",
                    adapter.CAPABILITY_ID,
                    "--interface-version",
                    adapter.INTERFACE_VERSION,
                    "--listen",
                    "127.0.0.1:0",
                ],
                environ=environment,
                manifest_path=PACKAGE_ROOT / "plugin.manifest.json",
            )


if __name__ == "__main__":
    unittest.main()
