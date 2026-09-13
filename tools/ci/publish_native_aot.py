#!/usr/bin/env python3
"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 publish_native_aot.py                                            │
│  Module: tools.ci                                                    │
│  Role: Publish and smoke-test the per-RID .NET Native AOT payload.    │
│                                                                     │
│  模块职责：按 RID 发布并冒烟验证 .NET Native AOT 产物。                  │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SUPPORTED_RIDS = {"linux-x64", "linux-arm64", "win-x64"}
EXPECTED_SYMBOLS = (
    "cyrene_plugin_get_api_v1",
    "cyrene_plugin_create_v1",
    "cyrene_plugin_invoke_v1",
    "cyrene_plugin_invoke_stream_v1",
    "cyrene_plugin_cancel_v1",
    "cyrene_plugin_free_buffer_v1",
    "cyrene_plugin_destroy_v1",
)


@dataclass(frozen=True)
class PublishedBinary:
    """Describe one published binary and its role in the release payload."""

    role: str
    path: Path


def _parse_args() -> argparse.Namespace:
    """Parse the target RID and disposable output directory."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rid", required=True, choices=sorted(SUPPORTED_RIDS))
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def _run(command: list[str], *, cwd: Path = REPOSITORY_ROOT) -> subprocess.CompletedProcess[str]:
    """Run a required command while preserving its output for CI evidence."""

    print("+", " ".join(command))
    return subprocess.run(command, cwd=cwd, check=True, text=True)


def _publish(project: str, rid: str, output_dir: Path) -> None:
    """Publish one project as a self-contained Native AOT artifact."""

    _run(
        [
            "dotnet",
            "publish",
            project,
            "--configuration",
            "Release",
            "--runtime",
            rid,
            "--self-contained",
            "true",
            "-p:PublishAot=true",
            "--output",
            str(output_dir),
        ]
    )


def _find_executable(directory: Path, stem: str) -> Path:
    """Resolve a Native AOT executable on Unix and Windows."""

    candidates = [directory / stem, directory / f"{stem}.exe"]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError(f"Published executable not found: {directory}/{stem}")


def _find_native_library(directory: Path) -> Path:
    """Resolve the NativeLib output without assuming a platform suffix."""

    candidates = sorted(
        path
        for path in directory.rglob("dotnet_cyrene_plugin.*")
        if path.is_file() and path.suffix.lower() in {".so", ".dll", ".dylib"}
    )
    if len(candidates) != 1:
        raise FileNotFoundError(
            f"Expected exactly one dotnet_cyrene_plugin shared library, got {candidates}"
        )
    return candidates[0]


def _run_json_binary(
    binary: PublishedBinary,
    *arguments: str,
    expected_returncodes: tuple[int, ...] = (0,),
) -> dict[str, object]:
    """Run a health command and parse its JSON response."""

    result = subprocess.run(
        [str(binary.path), *arguments],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in expected_returncodes:
        raise RuntimeError(
            f"{binary.role} returned {result.returncode} for {' '.join(arguments)}: "
            f"{result.stdout!r} {result.stderr!r}"
        )
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{binary.role} did not emit JSON for {' '.join(arguments)}: {result.stdout!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise TypeError(f"{binary.role} emitted a non-object JSON response")
    return parsed


def _verify_symbols(library: Path, rid: str) -> list[str]:
    """Verify every C ABI export with the native platform symbol tool."""

    if rid.startswith("linux"):
        tool = shutil.which("nm")
        if tool is None:
            raise RuntimeError("nm is required for Linux C ABI symbol verification")
        command = [tool, "-D", "--defined-only", str(library)]
    else:
        tool = shutil.which("dumpbin")
        if tool is None:
            raise RuntimeError("dumpbin is required for Windows C ABI symbol verification")
        command = [tool, "/exports", str(library)]

    output = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    missing = [symbol for symbol in EXPECTED_SYMBOLS if symbol not in output]
    if missing:
        raise RuntimeError(f"Missing C ABI symbols in {library}: {', '.join(missing)}")
    return list(EXPECTED_SYMBOLS)


def _publish_and_smoke(rid: str, output_dir: Path) -> dict[str, object]:
    """Publish all C# Native AOT components and execute their smoke checks."""

    if output_dir.exists():
        shutil.rmtree(output_dir)
    (output_dir / "bin").mkdir(parents=True)
    (output_dir / "lib").mkdir()
    (output_dir / "providers" / "openai").mkdir(parents=True)
    (output_dir / "providers" / "anthropic").mkdir(parents=True)
    (output_dir / "providers" / "gemini").mkdir(parents=True)

    host_stage = output_dir / "_host"
    native_lib_stage = output_dir / "_native-lib"
    openai_stage = output_dir / "_openai"
    anthropic_stage = output_dir / "_anthropic"
    gemini_stage = output_dir / "_gemini"

    _publish(
        "runtime/dotnet-native-aot/Cyrene.Plugin.Host/Cyrene.Plugin.Host.csproj",
        rid,
        host_stage,
    )
    _publish(
        "runtime/dotnet-native-aot/Cyrene.Plugin.NativeLib/Cyrene.Plugin.NativeLib.csproj",
        rid,
        native_lib_stage,
    )
    _publish(
        "runtime/dotnet-native-aot/Cyrene.Provider.OpenAi/Cyrene.Provider.OpenAi.csproj",
        rid,
        openai_stage,
    )
    _publish(
        "runtime/dotnet-native-aot/Cyrene.Provider.Anthropic/Cyrene.Provider.Anthropic.csproj",
        rid,
        anthropic_stage,
    )
    _publish(
        "runtime/dotnet-native-aot/Cyrene.Provider.Gemini/Cyrene.Provider.Gemini.csproj",
        rid,
        gemini_stage,
    )

    host = _find_executable(host_stage, "cyrene-plugin-host")
    native_library = _find_native_library(native_lib_stage)
    openai = _find_executable(openai_stage, "Cyrene.Provider.OpenAi")
    anthropic = _find_executable(anthropic_stage, "Cyrene.Provider.Anthropic")
    gemini = _find_executable(gemini_stage, "Cyrene.Provider.Gemini")

    staged_host = output_dir / "bin" / host.name
    staged_library = output_dir / "lib" / native_library.name
    staged_openai = output_dir / "providers" / "openai" / openai.name
    staged_anthropic = output_dir / "providers" / "anthropic" / anthropic.name
    staged_gemini = output_dir / "providers" / "gemini" / gemini.name
    for source, target in (
        (host, staged_host),
        (native_library, staged_library),
        (openai, staged_openai),
        (anthropic, staged_anthropic),
        (gemini, staged_gemini),
    ):
        shutil.copy2(source, target)
        target.chmod(target.stat().st_mode | 0o111)

    health = _run_json_binary(
        PublishedBinary("Native AOT host prototype", staged_host),
        "--health",
        expected_returncodes=(2,),
    )
    if health.get("status") != "NOT_SERVING":
        raise RuntimeError(f"Native AOT host prototype did not fail closed: {health}")

    provider_checks: dict[str, dict[str, object]] = {}
    for provider, binary in (
        ("openai", staged_openai),
        ("anthropic", staged_anthropic),
        ("gemini", staged_gemini),
    ):
        readiness = _run_json_binary(
            PublishedBinary(f"{provider} Native AOT provider", binary),
            "--readiness",
            expected_returncodes=(2,),
        )
        if readiness.get("status") != "NOT_SERVING" or readiness.get("aot") is not True:
            raise RuntimeError(f"{provider} provider did not fail closed: {readiness}")
        provider_checks[provider] = {
            "endpoint_status": "NOT_RUN",
            "readiness": readiness,
        }

    symbols = _verify_symbols(staged_library, rid)
    evidence = {
        "rid": rid,
        "host": str(staged_host.relative_to(output_dir)),
        "native_library": str(staged_library.relative_to(output_dir)),
        "providers": {
            provider: str(path.relative_to(output_dir))
            for provider, path in (
                ("openai", staged_openai),
                ("anthropic", staged_anthropic),
                ("gemini", staged_gemini),
            )
        },
        "binary_smoke": {
            "host_endpoint_status": "NOT_RUN",
            "host_process": health,
            "providers": provider_checks,
        },
        "cabi_symbols": symbols,
    }
    (output_dir / "native-aot-evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    for disposable in (
        host_stage,
        native_lib_stage,
        openai_stage,
        anthropic_stage,
        gemini_stage,
    ):
        shutil.rmtree(disposable)
    return evidence


def main() -> int:
    """Publish, smoke-test, and stage one RID's native artifacts."""

    args = _parse_args()
    output_dir = args.output_dir.resolve()
    evidence = _publish_and_smoke(args.rid, output_dir)
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
