"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 vllm_runtime.py                                                 │
│  Module: vllm_runtime                                               │
│  Role: Plugins-owned serving execution runtime for Reactor.         │
│                                                                     │
│  模块职责：Reactor serving 绑定的执行宿主：拉起 vLLM、代理推理、确认回收。  │
└─────────────────────────────────────────────────────────────────────┘

Reactor owns deployment intent; this runtime owns only the accelerator
process. It accepts an execution, materializes the Product artifact through the
Platform Artifact SDK, launches vLLM on a private port, publishes the
OpenAI-compatible endpoint the Product must reach, and confirms release on
stop. It never decides composition or lineage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID

from cy_artifacts import ArtifactKind, LocalArtifactProvider
from cy_artifacts import ArtifactRef as PlatformArtifactRef

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TERMINAL_RELEASED = "RELEASED"
DEFAULT_READY_TIMEOUT = 900.0
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".pth", ".gguf")
TOKENIZER_FILES = (
    "tokenizer.json",
    "tokenizer.model",
    "spiece.model",
    "tokenizer_config.json",
)


class ServingRuntimeError(RuntimeError):
    """Fail-closed runtime error carrying a stable code for the Product."""

    def __init__(self, code: str, detail: str, *, status: int = 409, retryable: bool = False) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status
        self.retryable = retryable


def _model_version_artifact(document: dict[str, Any]) -> dict[str, Any]:
    """Return the serving artifact projection of a canonical ModelVersion."""

    composition = document.get("composition")
    if composition == "FULL_MODEL":
        value = document.get("fullModelArtifact")
    elif composition == "BASE_PLUS_LORA":
        base = document.get("baseModel")
        value = base.get("artifact") if isinstance(base, dict) else None
    else:
        value = None
    if not isinstance(value, dict):
        raise ServingRuntimeError(
            "SERVING_MODEL_VERSION_INVALID",
            "The composed ModelVersion has no serving artifact projection.",
            status=422,
        )
    return value


def _manifest_digest(root: Path, files: list[Path]) -> str:
    """Digest the sorted relative path and size manifest of an import."""

    lines = []
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        lines.append(f"{path.relative_to(root).as_posix()}\0{path.stat().st_size}")
    return "sha256:" + hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _chat_template_present(root: Path) -> bool:
    if (root / "chat_template.jinja").is_file() or (root / "chat_template.json").is_file():
        return True
    tokenizer_config = root / "tokenizer_config.json"
    if not tokenizer_config.is_file():
        return False
    try:
        document = json.loads(tokenizer_config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(document, dict) and bool(document.get("chat_template"))


def _license_name(root: Path) -> str | None:
    for candidate in sorted(root.iterdir()):
        if not candidate.is_file():
            continue
        upper = candidate.name.upper()
        if upper.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")):
            try:
                first_line = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return None
            return first_line[0][:200] if first_line else candidate.name
    return None


def _validate_model_directory(root: Path) -> tuple[dict[str, bool], list[str]]:
    """Check the minimum portable text-model file set. | 校验最小可移植文本模型文件集。"""

    files = [path for path in root.rglob("*") if path.is_file() and not path.is_symlink()]
    issues: list[str] = []
    weights = any(path.name.endswith(WEIGHT_SUFFIXES) for path in files)
    config = (root / "config.json").is_file()
    tokenizer = (root / "tokenizer.json").is_file() or (root / "tokenizer.model").is_file() or (
        (root / "vocab.json").is_file() and (root / "merges.txt").is_file()
    )
    chat_template = _chat_template_present(root)
    if not weights:
        issues.append("no supported weight file was found")
    if not config:
        issues.append("config.json is missing")
    if not tokenizer:
        issues.append("no supported tokenizer file was found")
    if not chat_template:
        issues.append("no chat template was found; the model may not accept conversations")
    return (
        {
            "weights": weights,
            "config": config,
            "tokenizer": tokenizer,
            "chat_template": chat_template,
        },
        issues,
    )


class ServingRuntime:
    """Owns one accelerator process per deployment."""

    def __init__(
        self,
        *,
        home: Path,
        artifact_root: Path,
        command: list[str],
        control_url: str,
        ready_timeout: float = DEFAULT_READY_TIMEOUT,
        poll_interval: float = 1.0,
    ) -> None:
        self.home = home
        self.artifact_root = artifact_root
        self.command = command
        self.control_url = control_url.rstrip("/")
        self.ready_timeout = ready_timeout
        self.poll_interval = poll_interval
        self.executions = home / "executions"
        self.models = home / "models"
        self.credentials = home / "credentials"
        self.executions.mkdir(parents=True, exist_ok=True)
        self.models.mkdir(parents=True, exist_ok=True)
        self.credentials.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._processes: dict[str, subprocess.Popen] = {}
        self._lock = threading.RLock()

    # ── persistence ─────────────────────────────────────────────────────

    def _record_path(self, deployment_id: UUID) -> Path:
        return self.executions / f"{deployment_id}.json"

    def _load(self, deployment_id: UUID) -> dict[str, Any]:
        path = self._record_path(deployment_id)
        if not path.is_file():
            raise ServingRuntimeError(
                "SERVING_EXECUTION_NOT_FOUND",
                "No execution exists with the requested identity.",
                status=404,
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, deployment_id: UUID, document: dict[str, Any]) -> None:
        path = self._record_path(deployment_id)
        pending = path.with_suffix(".pending")
        pending.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        pending.replace(path)

    # ── model import ────────────────────────────────────────────────────

    def _credential(self, credential_ref: str) -> str:
        """Resolve one CredentialRef from the runtime's private store.

        The ref itself is never a secret: only its digest names the mode-0600
        file that holds the token, and the token never appears in a response.
        """

        marker = self.credentials / hashlib.sha256(credential_ref.encode("utf-8")).hexdigest()
        if not marker.is_file() or marker.stat().st_mode & 0o077:
            raise ServingRuntimeError(
                "SERVING_CREDENTIAL_REF_UNRESOLVED",
                "The referenced credential is not installed for this serving binding.",
                status=403,
            )
        token = marker.read_text(encoding="utf-8").strip()
        if len(token) < 8:
            raise ServingRuntimeError(
                "SERVING_CREDENTIAL_REF_UNRESOLVED",
                "The referenced credential is empty.",
                status=403,
            )
        return token

    def _download_hugging_face(self, source: dict[str, Any], token: str | None) -> Path:
        """Materialize a pinned Hugging Face revision through huggingface_hub."""

        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise ServingRuntimeError(
                "SERVING_MODEL_SOURCE_UNAVAILABLE",
                "huggingface_hub is required to import a Hugging Face repository.",
                status=503,
            ) from exc
        repository = str(source.get("repository") or "")
        revision = str(source.get("revision") or "")
        destination = self.models / "imports" / f"{repository.replace('/', '__')}-{revision}"
        if not (destination / "config.json").is_file():
            destination.mkdir(parents=True, exist_ok=True)
            try:
                snapshot_download(
                    repo_id=repository,
                    revision=revision,
                    local_dir=destination,
                    token=token,
                )
            except Exception as exc:
                raise ServingRuntimeError(
                    "SERVING_MODEL_SOURCE_UNAVAILABLE",
                    "The pinned Hugging Face revision could not be downloaded.",
                    status=503,
                    retryable=True,
                ) from exc
        return destination

    def import_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate and publish an external model source. | 校验并发布外部模型来源。"""

        if payload.get("trustRemoteCode"):
            raise ServingRuntimeError(
                "SERVING_TRUST_REMOTE_CODE_FORBIDDEN",
                "trust_remote_code is disabled for the released import path.",
                status=422,
            )
        source = payload.get("source")
        if not isinstance(source, dict):
            raise ServingRuntimeError(
                "SERVING_REQUEST_INVALID", "source is required.", status=422
            )
        credential_ref = payload.get("credentialRef")
        if credential_ref is not None and not isinstance(credential_ref, str):
            raise ServingRuntimeError(
                "SERVING_REQUEST_INVALID", "credentialRef must be a string.", status=422
            )
        token = self._credential(credential_ref) if credential_ref else None
        kind = source.get("kind")
        if kind == "LOCAL_PATH":
            candidate = Path(str(source.get("path") or ""))
            if not candidate.is_absolute() or ".." in candidate.parts or candidate.is_symlink():
                raise ServingRuntimeError(
                    "SERVING_MODEL_SOURCE_INVALID",
                    "a local import requires an absolute, traversal-free directory",
                    status=422,
                )
            if not candidate.is_dir():
                raise ServingRuntimeError(
                    "SERVING_MODEL_SOURCE_UNAVAILABLE",
                    "the local model directory does not exist on the execution host",
                    status=422,
                )
            directory = candidate
            provenance = f"local:{candidate}"
        elif kind == "HUGGING_FACE":
            repository = str(source.get("repository") or "")
            revision = str(source.get("revision") or "")
            if "/" not in repository or len(revision) != 40:
                raise ServingRuntimeError(
                    "SERVING_MODEL_SOURCE_INVALID",
                    "a Hugging Face import requires a repository and a pinned 40-hex revision",
                    status=422,
                )
            directory = self._download_hugging_face(source, token)
            provenance = f"huggingface:{repository}@{revision}"
        else:
            raise ServingRuntimeError(
                "SERVING_MODEL_SOURCE_INVALID", "unsupported source kind", status=422
            )

        checks, issues = _validate_model_directory(directory)
        if not (checks["weights"] and checks["config"] and checks["tokenizer"]):
            raise ServingRuntimeError(
                "SERVING_MODEL_VALIDATION_FAILED",
                "The model directory is not servable: " + "; ".join(issues),
                status=422,
            )
        files = [
            path
            for path in directory.rglob("*")
            if path.is_file() and not path.is_symlink()
        ]
        artifact = LocalArtifactProvider(self.artifact_root).publish_portable_directory(
            directory, kind=ArtifactKind("model")
        )
        return {
            "modelArtifact": artifact.to_dict(),
            "validation": {
                "weights": checks["weights"],
                "config": checks["config"],
                "tokenizer": checks["tokenizer"],
                "chatTemplate": checks["chat_template"],
                "license": _license_name(directory),
                "provenance": provenance,
                "digest": _manifest_digest(directory, files),
                "trustRemoteCode": False,
                "issues": issues,
            },
        }

    # ── operations ──────────────────────────────────────────────────────

    def start(self, deployment_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        """Materialize the artifact, launch vLLM, and confirm readiness."""

        with self._lock:
            if self._record_path(deployment_id).is_file():
                return self._public(deployment_id, self._load(deployment_id))
            model_artifact = payload.get("modelArtifact")
            if not isinstance(model_artifact, dict):
                raise ServingRuntimeError(
                    "SERVING_REQUEST_INVALID", "modelArtifact is required.", status=422
                )
            model_version = payload.get("modelVersion")
            if model_version is not None and not isinstance(model_version, dict):
                raise ServingRuntimeError(
                    "SERVING_REQUEST_INVALID", "modelVersion must be an object.", status=422
                )
            served_model = f"reactor-{deployment_id}"
            document: dict[str, Any] = {
                "deploymentId": str(deployment_id),
                "modelArtifact": model_artifact,
                "servedModel": served_model,
                "endpointUrl": f"{self.control_url}/serving/{deployment_id}/v1",
                "state": "STARTING",
            }
            if model_version is not None:
                document["modelVersion"] = model_version
            self._save(deployment_id, document)
            try:
                argv, port = self._prepare_process(deployment_id, payload, served_model)
            except ServingRuntimeError as exc:
                document.update(state="FAILED", detail=exc.detail, code=exc.code)
                self._save(deployment_id, document)
                raise
            log = (self.home / "logs").joinpath(f"{deployment_id}.log")
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("ab") as stream:
                process = subprocess.Popen(
                    argv,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            document.update(pid=process.pid, port=port, state="STARTING")
            self._processes[str(deployment_id)] = process
            self._save(deployment_id, document)
            if not self._await_ready(process, port):
                self._terminate(str(deployment_id), process.pid)
                document.update(state="FAILED", detail="vLLM did not become ready", code="MODEL_NOT_READY")
                self._save(deployment_id, document)
                raise ServingRuntimeError(
                    "MODEL_NOT_READY",
                    "vLLM did not become ready before the deadline.",
                    status=503,
                    retryable=True,
                )
            document["state"] = "READY"
            self._save(deployment_id, document)
            return self._public(deployment_id, document)

    def inspect(self, deployment_id: UUID) -> dict[str, Any]:
        """Report the recorded execution and its live process state."""

        with self._lock:
            document = self._load(deployment_id)
            return self._public(deployment_id, document)

    def stop(self, deployment_id: UUID) -> dict[str, Any]:
        """Terminate the accelerator process and confirm the release."""

        with self._lock:
            document = self._load(deployment_id)
            if document.get("state") == TERMINAL_RELEASED:
                return self._public(deployment_id, document)
            pid = document.get("pid")
            if isinstance(pid, int):
                self._terminate(str(deployment_id), pid)
            document.update(state=TERMINAL_RELEASED, ready=False, released=True)
            document.pop("pid", None)
            self._save(deployment_id, document)
            return self._public(deployment_id, document)

    # ── internals ───────────────────────────────────────────────────────

    def _prepare_process(
        self, deployment_id: UUID, payload: dict[str, Any], served_model: str
    ) -> tuple[list[str], int]:
        model_artifact = PlatformArtifactRef.from_dict(payload["modelArtifact"])
        model_version = payload.get("modelVersion")
        adapter: Path | None = None
        if isinstance(model_version, dict) and model_version.get("composition") == "BASE_PLUS_LORA":
            base_artifact = PlatformArtifactRef.from_dict(_model_version_artifact(model_version))
            adapter = self._materialize(deployment_id, "adapter", model_artifact)
            model_artifact = base_artifact
        model_dir = self._materialize(deployment_id, "model", model_artifact)
        port = self._free_port()
        argv = [
            *self.command,
            str(model_dir),
            "--served-model-name",
            served_model,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
        if adapter is not None:
            argv.extend(["--enable-lora", "--lora-modules", f"{served_model}={adapter}"])
        return argv, port

    def _materialize(self, deployment_id: UUID, name: str, reference: PlatformArtifactRef) -> Path:
        destination = self.models / str(deployment_id) / name
        if destination.is_dir():
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        provider = LocalArtifactProvider(self.artifact_root)
        try:
            provider.stage(reference, destination)
        except Exception as exc:
            raise ServingRuntimeError(
                "SERVING_ARTIFACT_UNAVAILABLE",
                f"The deployment artifact cannot be materialized: {reference.uri}",
                status=422,
            ) from exc
        return destination

    def _free_port(self) -> int:
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    def _await_ready(self, process: subprocess.Popen, port: int) -> bool:
        deadline = time.time() + self.ready_timeout
        url = f"http://127.0.0.1:{port}/v1/models"
        while time.time() < deadline:
            if process.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(url, timeout=2.0):
                    return True
            except (urllib.error.URLError, OSError):
                time.sleep(self.poll_interval)
        return False

    def _terminate(self, deployment_id: str, pid: int) -> None:
        """Stop one execution and confirm the process really exited.

        A process this runtime started is reaped with ``wait`` so a zombie can
        never be mistaken for a live accelerator; a process recovered from an
        earlier run is probed by identity.
        """

        process = self._processes.get(deployment_id)
        for sig, wait in ((signal.SIGTERM, 20.0), (signal.SIGKILL, 5.0)):
            try:
                os.killpg(os.getpgid(pid), sig)
            except (ProcessLookupError, PermissionError):
                self._processes.pop(deployment_id, None)
                return
            if process is not None:
                try:
                    process.wait(timeout=wait)
                except subprocess.TimeoutExpired:
                    continue
                self._processes.pop(deployment_id, None)
                return
            deadline = time.time() + wait
            while time.time() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return
                time.sleep(0.2)

    def _public(self, deployment_id: UUID, document: dict[str, Any]) -> dict[str, Any]:
        pid = document.get("pid")
        alive = False
        if isinstance(pid, int):
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
        response = {
            "ready": document.get("state") == "READY" and alive,
            "executionRef": str(deployment_id),
            "endpointUrl": document["endpointUrl"],
            "servedModel": document["servedModel"],
            "modelArtifact": document["modelArtifact"],
        }
        if "modelVersion" in document:
            response["modelVersion"] = document["modelVersion"]
        if document.get("released"):
            response["released"] = True
        if document.get("detail"):
            response["detail"] = document["detail"]
        return response


class RuntimeHandler(BaseHTTPRequestHandler):
    """Bearer-protected HTTP surface consumed by the Reactor Product."""

    runtime: ServingRuntime
    token: str
    upstream_timeout = 600.0

    protocol_version = "HTTP/1.1"

    def _authorized(self) -> bool:
        return secrets.compare_digest(
            self.headers.get("Authorization", ""), "Bearer " + self.token
        )

    def _send(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _error(self, exc: ServingRuntimeError) -> None:
        self._send(exc.status, {"code": exc.code, "detail": exc.detail, "retryable": exc.retryable})

    def _refuse(self, status: int, code: str, detail: str) -> None:
        self._send(status, {"code": code, "detail": detail, "retryable": False})

    def _read_payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > 2 * 1024 * 1024:
            raise ServingRuntimeError("SERVING_REQUEST_INVALID", "invalid request body", status=422)
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ServingRuntimeError("SERVING_REQUEST_INVALID", "request must be an object", status=422)
        return payload

    def do_GET(self) -> None:
        if self.path in {"/healthz", "/readyz"}:
            self._send(200, {"status": "ok"})
            return
        if not self._authorized():
            self._refuse(
                403,
                "SERVING_BINDING_PERMISSION_DENIED",
                "The binding credential is missing or invalid.",
            )
            return
        parts = self.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "executions":
            try:
                self._send(200, self.runtime.inspect(UUID(parts[1])))
            except (ValueError, ServingRuntimeError) as exc:
                self._error(exc if isinstance(exc, ServingRuntimeError) else ServingRuntimeError("SERVING_EXECUTION_NOT_FOUND", str(exc), status=404))
            return
        self._refuse(404, "SERVING_ROUTE_NOT_FOUND", "no route matches the request")

    def do_POST(self) -> None:
        if not self._authorized():
            self._refuse(
                403,
                "SERVING_BINDING_PERMISSION_DENIED",
                "The binding credential is missing or invalid.",
            )
            return
        parts = self.path.strip("/").split("/")
        try:
            if len(parts) == 1 and parts[0] == "imports":
                self._send(200, self.runtime.import_model(self._read_payload()))
                return
            if len(parts) == 2 and parts[0] == "executions":
                self._send(200, self.runtime.start(UUID(parts[1]), self._read_payload()))
                return
            if len(parts) == 3 and parts[0] == "executions" and parts[2] == "stop":
                self._send(200, self.runtime.stop(UUID(parts[1])))
                return
            if len(parts) >= 4 and parts[0] == "serving" and parts[2] == "v1":
                self._proxy(UUID(parts[1]), "/" + "/".join(parts[2:]))
                return
        except (ValueError, ServingRuntimeError) as exc:
            self._error(exc if isinstance(exc, ServingRuntimeError) else ServingRuntimeError("SERVING_REQUEST_INVALID", str(exc), status=422))
            return
        self._refuse(404, "SERVING_ROUTE_NOT_FOUND", "no route matches the request")

    def _proxy(self, deployment_id: UUID, path: str) -> None:
        document = self.runtime.inspect(deployment_id)
        if not document.get("ready"):
            raise ServingRuntimeError("MODEL_NOT_READY", "the execution is not serving", status=503, retryable=True)
        port = self.runtime._load(deployment_id).get("port")
        if not isinstance(port, int):
            raise ServingRuntimeError("SERVING_RESPONSE_INCOMPATIBLE", "execution has no serving port")
        payload = self._read_payload()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.upstream_timeout) as response:
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                self.send_header("Connection", "close")
                self.end_headers()
                shutil.copyfileobj(response, self.wfile)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (urllib.error.URLError, OSError) as exc:
            raise ServingRuntimeError(
                "SERVING_UPSTREAM_UNAVAILABLE",
                f"the serving process is unreachable: {exc}",
                status=503,
                retryable=True,
            ) from exc

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _bounded_home(value: Path) -> Path:
    home = value.expanduser().resolve()
    if not value.is_absolute() or home == Path(home.anchor):
        raise ValueError("SERVING_RUNTIME_HOME_REQUIRED")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    return home


def _token(credential_file: Path) -> str:
    if not credential_file.is_file() or credential_file.stat().st_mode & 0o077:
        raise ValueError("SERVING_CREDENTIAL_INVALID: expected a mode-0600 credential file")
    token = credential_file.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError("SERVING_CREDENTIAL_INVALID: credential is too short")
    return token


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Cyrene vLLM serving execution runtime")
    value.add_argument("command", choices=("serve", "status"), nargs="?", default="serve")
    value.add_argument("--runtime-home", type=Path, required=True)
    value.add_argument("--artifact-root", type=Path, required=True)
    value.add_argument("--credential-file", type=Path, required=True)
    value.add_argument("--control-url", required=True)
    value.add_argument("--host", default="127.0.0.1")
    value.add_argument("--port", type=int, default=19400)
    value.add_argument(
        "--vllm-command",
        default="vllm serve",
        help="Operator-installed vLLM launcher, for example 'vllm serve'",
    )
    value.add_argument("--ready-timeout", type=float, default=DEFAULT_READY_TIMEOUT)
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        home = _bounded_home(args.runtime_home)
        token = _token(args.credential_file)
        if args.host not in LOOPBACK_HOSTS:
            raise ValueError("SERVING_TLS_REQUIRED: bind loopback or terminate TLS in front")
        if args.command == "status":
            print(json.dumps({"status": "READY", "runtimeHome": str(home)}, sort_keys=True))
            return 0
        RuntimeHandler.runtime = ServingRuntime(
            home=home,
            artifact_root=args.artifact_root.resolve(),
            command=args.vllm_command.split(),
            control_url=args.control_url,
            ready_timeout=args.ready_timeout,
        )
        RuntimeHandler.token = token
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "FAILED", "code": str(exc).split(":", 1)[0]}, sort_keys=True))
        return 1
    server = ThreadingHTTPServer((args.host, args.port), RuntimeHandler)
    print(json.dumps({"status": "READY", "port": args.port}, sort_keys=True), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())