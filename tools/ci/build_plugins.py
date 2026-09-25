"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 build_plugins.py                                                │
│  Module: tools.ci                                                   │
│  Role: Multi-language Modular Plugin Build, Packaging & Indexer     │
│  模块职责：多语言模块化插件分别独立构建、打包与清单发布工具。       │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class PluginInfo:
    id: str
    name: str
    version: str
    description: str
    language: str
    entrypoint: str
    path: str
    manifest_path: str
    capabilities: list[str]


def discover_plugins(root: Path) -> list[PluginInfo]:
    plugins: list[PluginInfo] = []
    plugins_dir = root / "plugins"

    for manifest_path in sorted(plugins_dir.glob("**/plugin.manifest.json")):
        if "rollback" in manifest_path.parts:
            continue
        try:
            with open(manifest_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"Warning: Failed to parse {manifest_path}: {e}", file=sys.stderr)
            continue

        runtime = data.get("runtime", {})
        info = PluginInfo(
            id=data.get("id", ""),
            name=data.get("name", ""),
            version=data.get("version", "0.1.0"),
            description=data.get("description", ""),
            language=runtime.get("language", "python").lower(),
            entrypoint=runtime.get("entrypoint", ""),
            path=str(manifest_path.parent.relative_to(root)),
            manifest_path=str(manifest_path.relative_to(root)),
            capabilities=data.get("capabilities", []),
        )
        if info.id:
            plugins.append(info)

    return plugins


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def build_python_plugin(plugin: PluginInfo, root: Path, out_dir: Path) -> list[Path]:
    plugin_dir = root / plugin.path
    pyproject = plugin_dir / "pyproject.toml"
    if not pyproject.exists():
        print(f"Skipping {plugin.id}: no pyproject.toml at {plugin_dir}")
        return []

    print(f"\n>> [Python] Building wheel for {plugin.id} ({plugin.version})...")
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "build",
        "--wheel",
        "--no-isolation",
        "--outdir",
        str(out_dir),
        str(plugin_dir),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Build failed for {plugin.id}:\n{res.stderr}\n{res.stdout}", file=sys.stderr)
        return []

    # Find created wheel
    wheels = list(out_dir.glob(f"*{plugin.id.split('.')[-1]}*.whl"))
    if not wheels:
        # Fallback to any recently modified wheel in out_dir
        wheels = [w for w in out_dir.glob("*.whl") if w.name.startswith("cyrene_")]

    return wheels


def build_rust_plugin(plugin: PluginInfo, root: Path, out_dir: Path) -> list[Path]:
    print(f"\n>> [Rust] Building native package for {plugin.id} ({plugin.version})...")
    out_dir.mkdir(parents=True, exist_ok=True)

    server_dir = root / "runtime/rust/cyrene-plugin-server"
    if not server_dir.exists():
        print(f"Skipping Rust build: {server_dir} not found")
        return []

    cmd = ["cargo", "build", "--release", "--manifest-path", str(server_dir / "Cargo.toml")]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Rust build failed:\n{res.stderr}\n{res.stdout}", file=sys.stderr)
        return []

    bin_path = root / "runtime/rust/target/release/cyrene-plugin-server"
    if not bin_path.exists():
        bin_path = root / "target/release/cyrene-plugin-server"

    tar_name = f"{plugin.id}-{plugin.version}-linux-x64.tar.gz"
    tar_path = out_dir / tar_name

    with tarfile.open(tar_path, "w:gz") as tar:
        if bin_path.exists():
            tar.add(bin_path, arcname="bin/cyrene-plugin-server")
        manifest_file = root / plugin.manifest_path
        if manifest_file.exists():
            tar.add(manifest_file, arcname="plugin.manifest.json")

    print(f"   Created Rust package: {tar_path.name}")
    return [tar_path]


def build_dotnet_plugin(plugin: PluginInfo, root: Path, out_dir: Path) -> list[Path]:
    print(f"\n>> [.NET] Building Native AOT package for {plugin.id} ({plugin.version})...")
    out_dir.mkdir(parents=True, exist_ok=True)

    proj_name = "Cyrene.OneBot.V11.Host" if "onebot" in plugin.id else "Cyrene.Im.Host"
    proj_dir = root / f"runtime/dotnet-native-aot/{proj_name}"
    proj_file = proj_dir / f"{proj_name}.csproj"

    if not proj_file.exists():
        print(f"Skipping .NET build: {proj_file} not found")
        return []

    publish_dir = out_dir / f"publish-{plugin.id.split('.')[-1]}"
    cmd = [
        "dotnet",
        "publish",
        str(proj_file),
        "-c",
        "Release",
        "-o",
        str(publish_dir),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f".NET build failed:\n{res.stderr}\n{res.stdout}", file=sys.stderr)
        return []

    tar_name = f"{plugin.id}-{plugin.version}-linux-x64.tar.gz"
    tar_path = out_dir / tar_name

    with tarfile.open(tar_path, "w:gz") as tar:
        for f in publish_dir.iterdir():
            tar.add(f, arcname=f"bin/{f.name}")
        manifest_file = root / plugin.manifest_path
        if manifest_file.exists():
            tar.add(manifest_file, arcname="plugin.manifest.json")

    # Clean temporary publish dir
    shutil.rmtree(publish_dir, ignore_errors=True)
    print(f"   Created .NET package: {tar_path.name}")
    return [tar_path]


def main():
    parser = argparse.ArgumentParser(
        description="Cyrene Multi-language Modular Plugin Builder & Indexer"
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="Repository root path")
    parser.add_argument(
        "--output-dir", type=Path, default=REPO_ROOT / "dist/plugins", help="Output directory"
    )
    parser.add_argument("--list", action="store_true", help="List all discovered plugins")
    parser.add_argument("--plugin", type=str, help="Build only specific plugin by ID or path")
    parser.add_argument(
        "--language",
        type=str,
        choices=["python", "rust", "csharp", "all"],
        default="all",
        help="Filter by language",
    )
    parser.add_argument(
        "--all", action="store_true", help="Build all plugins across all languages"
    )

    args = parser.parse_args()
    root = args.root.resolve()
    out_dir = args.output_dir.resolve()

    plugins = discover_plugins(root)

    if args.list:
        print(f"\nDiscovered {len(plugins)} official plugins:")
        print(f"{'ID':<38} {'Version':<9} {'Language':<9} {'Path'}")
        print("-" * 80)
        for p in plugins:
            print(f"{p.id:<38} {p.version:<9} {p.language:<9} {p.path}")
        return

    # Filter target plugins
    targets = plugins
    if args.plugin:
        targets = [
            p
            for p in plugins
            if args.plugin in (p.id, p.path, p.name) or p.id.endswith(args.plugin)
        ]
        if not targets:
            print(f"Error: No plugin matching '{args.plugin}' found.", file=sys.stderr)
            sys.exit(1)

    if args.language != "all":
        targets = [p for p in targets if p.language == args.language]

    print(f"============================================================")
    print(f"  Cyrene Modular Plugin Builder                             ")
    print(f"  Target plugins to build: {len(targets)}                   ")
    print(f"  Output directory: {out_dir}                               ")
    print(f"============================================================")

    manifest_entries: list[dict[str, Any]] = []

    for p in targets:
        built_files: list[Path] = []
        if p.language == "python":
            built_files = build_python_plugin(p, root, out_dir)
        elif p.language == "rust":
            built_files = build_rust_plugin(p, root, out_dir)
        elif p.language in ("csharp", "dotnet"):
            built_files = build_dotnet_plugin(p, root, out_dir)

        artifacts = []
        for bf in built_files:
            if bf.exists():
                artifacts.append(
                    {
                        "filename": bf.name,
                        "size_bytes": bf.stat().st_size,
                        "sha256": sha256_file(bf),
                    }
                )

        entry = asdict(p)
        entry["artifacts"] = artifacts
        manifest_entries.append(entry)

    # Write consolidated index manifest
    index_manifest = {
        "version": "1.0",
        "generated_at": "2026-09-25T16:30:00Z",
        "total_plugins": len(manifest_entries),
        "plugins": manifest_entries,
    }

    index_file = out_dir / "plugins-manifest.json"
    with open(index_file, "w", encoding="utf-8") as f:
        json.dump(index_manifest, f, indent=2, ensure_ascii=False)

    print(f"\n✅ All selected plugins processed!")
    print(f"   Published catalog index: {index_file}")


if __name__ == "__main__":
    main()
