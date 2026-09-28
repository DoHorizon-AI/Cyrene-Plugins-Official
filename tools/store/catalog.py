#!/usr/bin/env python3
"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 catalog.py                                                      │
│  Module: tools.store                                                │
│  Role: Universal Plugin Catalog Generation & Indexing Engine        │
│  模块职责：多语言通用插件 Catalog 生成、服务画像索引与检索引擎。     │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure repository root is on sys.path for direct script execution
_SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = _SCRIPT_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.store.profiles import get_profiles_for_plugin, load_profiles

SCHEMA_VERSION = "cyrene.plugins.catalog.v1"
DEFAULT_CATALOG_REL_PATH = Path("dist/plugins/plugins-catalog.json")
MANIFEST_GLOB_PATTERN = "plugins/*/*/plugin.manifest.json"

LANGUAGE_CANONICAL_MAP = {
    "python": "python",
    "py": "python",
    "rust": "rust",
    "rs": "rust",
    "csharp": "csharp",
    "c#": "csharp",
    "dotnet": "csharp",
    "cs": "csharp",
}


def canonical_language(raw_lang: str | None) -> str:
    """Normalize language identifier to canonical standard: python, rust, csharp.

    中文: 将语言标识符规范化为标准名称：python, rust, csharp。
    """
    if not raw_lang:
        return "python"
    cleaned = raw_lang.strip().lower()
    return LANGUAGE_CANONICAL_MAP.get(cleaned, cleaned)


def discover_plugin_manifests(root: Path = REPO_ROOT) -> list[Path]:
    """Discover all official plugin manifest files matching plugins/*/*/plugin.manifest.json.

    Excludes rollback versions or deeper nested historical artifacts.

    中文: 自动发现所有符合 plugins/*/*/plugin.manifest.json 的官方插件清单文件。
    排除 rollback 版本与更深层级的历史残留。
    """
    manifests: list[Path] = []
    for path in sorted(root.glob(MANIFEST_GLOB_PATTERN)):
        if "rollback" in path.parts:
            continue
        manifests.append(path)
    return manifests


def parse_plugin_manifest(
    manifest_path: Path,
    root: Path = REPO_ROOT,
    profiles_by_plugin: dict[str, list[str]] | None = None,
    artifacts_by_plugin: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Parse one plugin.manifest.json into standardized catalog plugin metadata.

    Extracts all 11 required fields:
    - id
    - name
    - version
    - description
    - language
    - kind
    - capabilities
    - profiles
    - runtime
    - contributions
    - artifacts

    Also includes helper location metadata: path and manifest_path.

    中文: 将单个 plugin.manifest.json 解析为标准化的 Catalog 插件元数据。
    完整提取 11 个必要字段及路径辅助信息。
    """
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    plugin_id = str(data.get("id", "")).strip()
    runtime = data.get("runtime", {})
    if not isinstance(runtime, dict):
        runtime = {}

    raw_lang = runtime.get("language")
    language = canonical_language(raw_lang)

    # Determine service profiles
    associated_profiles: set[str] = set()
    if profiles_by_plugin is not None:
        associated_profiles.update(profiles_by_plugin.get(plugin_id, []))
    else:
        associated_profiles.update(get_profiles_for_plugin(plugin_id))

    # Also incorporate any profiles explicitly specified inside the manifest itself
    manifest_profiles = data.get("profiles", [])
    if isinstance(manifest_profiles, list):
        for item in manifest_profiles:
            if isinstance(item, str):
                associated_profiles.add(item)
            elif isinstance(item, dict) and "name" in item:
                associated_profiles.add(str(item["name"]))

    # Determine artifacts
    artifacts: list[dict[str, Any]] = []
    if artifacts_by_plugin and plugin_id in artifacts_by_plugin:
        artifacts = list(artifacts_by_plugin[plugin_id])
    elif "artifacts" in data and isinstance(data["artifacts"], list):
        artifacts = list(data["artifacts"])
    elif "distribution" in data and isinstance(data["distribution"], dict):
        dist = data["distribution"]
        if "artifacts" in dist and isinstance(dist["artifacts"], list):
            artifacts = list(dist["artifacts"])

    # Contributions extraction
    contributions = data.get("contributions", {})
    if not isinstance(contributions, dict):
        contributions = {}

    try:
        rel_path = str(manifest_path.parent.relative_to(root))
        rel_manifest = str(manifest_path.relative_to(root))
    except ValueError:
        rel_path = str(manifest_path.parent)
        rel_manifest = str(manifest_path)

    metadata: dict[str, Any] = {
        "id": plugin_id,
        "name": data.get("name", ""),
        "version": data.get("version", "0.1.0"),
        "description": data.get("description", ""),
        "language": language,
        "kind": data.get("kind", "capability-plugin"),
        "capabilities": list(data.get("capabilities", [])),
        "profiles": sorted(associated_profiles),
        "runtime": runtime,
        "contributions": contributions,
        "artifacts": artifacts,
        "path": rel_path,
        "manifest_path": rel_manifest,
    }
    return metadata


def build_catalog(
    output_path: str | Path | None = None,
    root_dir: str | Path | None = None,
    artifacts_by_plugin: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Discover all plugins, associate profiles, and construct unified catalog dictionary.

    If output_path is provided, writes the catalog JSON file with indent=2.

    中文: 发现全部插件，关联服务画像，构建统一的 Catalog 字典。
    若提供了 output_path，则序列化写入 plugins-catalog.json。
    """
    root = Path(root_dir).resolve() if root_dir else REPO_ROOT

    # Load service profiles
    profiles_dict = load_profiles(root / "tools" / "store" / "profiles.yaml")
    profiles_by_plugin: dict[str, list[str]] = {}
    for prof_name, p_list in profiles_dict.items():
        for p_id in p_list:
            profiles_by_plugin.setdefault(p_id, []).append(prof_name)
    for p_id, p_profiles in profiles_by_plugin.items():
        profiles_by_plugin[p_id] = sorted(set(p_profiles))

    # Merge previously built artifacts if catalog already exists at output_path
    combined_artifacts = dict(artifacts_by_plugin or {})
    if output_path is not None:
        target_path = Path(output_path).resolve()
        if target_path.is_file():
            try:
                existing = json.loads(target_path.read_text(encoding="utf-8"))
                for ep in existing.get("plugins", []):
                    ep_id = ep.get("id")
                    if ep_id and ep_id not in combined_artifacts and ep.get("artifacts"):
                        combined_artifacts[ep_id] = ep["artifacts"]
            except (OSError, json.JSONDecodeError):
                pass

    manifest_paths = discover_plugin_manifests(root)
    plugins: list[dict[str, Any]] = []
    for mp in manifest_paths:
        meta = parse_plugin_manifest(
            manifest_path=mp,
            root=root,
            profiles_by_plugin=profiles_by_plugin,
            artifacts_by_plugin=combined_artifacts,
        )
        if meta["id"]:
            plugins.append(meta)

    # Sort plugins by ID for deterministic output
    plugins.sort(key=lambda x: x["id"])

    iso_timestamp = datetime.now(timezone.utc).isoformat()
    catalog: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "schema_version": SCHEMA_VERSION,
        "generatedAt": iso_timestamp,
        "generated_at": iso_timestamp,
        "totalPlugins": len(plugins),
        "total_plugins": len(plugins),
        "profiles": profiles_dict,
        "plugins": plugins,
    }

    if output_path is not None:
        out = Path(output_path).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return catalog


def load_catalog(
    catalog_path: str | Path | None = None,
    root_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Load existing plugins-catalog.json or build on-the-fly if not found.

    中文: 读取现有的 plugins-catalog.json，若未生成且未显式指定路径，则动态构建返回。
    """
    root = Path(root_dir).resolve() if root_dir else REPO_ROOT
    if catalog_path is not None:
        p = Path(catalog_path).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"Catalog file not found: {catalog_path}")
        return json.loads(p.read_text(encoding="utf-8"))

    default_path = root / DEFAULT_CATALOG_REL_PATH
    if default_path.is_file():
        return json.loads(default_path.read_text(encoding="utf-8"))

    # Fallback to dynamic build
    return build_catalog(root_dir=root)


def list_plugins(
    profile: str | None = None,
    language: str | None = None,
    keyword: str | None = None,
    catalog_path: str | Path | None = None,
    catalog: dict[str, Any] | None = None,
    root_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Filter and query plugins by profile, language, or search keyword.

    Parameters:
    - profile: Filter by service profile name (e.g. 'reactor', 'echo', 'yield')
    - language: Filter by language (e.g. 'python', 'rust', 'csharp')
    - keyword: Substring search across id, name, description, capabilities, kind

    中文: 根据服务画像、编程语言或关键词过滤检索插件列表。
    """
    if catalog is None:
        catalog = load_catalog(catalog_path=catalog_path, root_dir=root_dir)

    items: list[dict[str, Any]] = catalog.get("plugins", [])

    if profile is not None:
        p_target = profile.strip().lower()
        items = [
            item
            for item in items
            if any(p.lower() == p_target for p in item.get("profiles", []))
        ]

    if language is not None:
        lang_target = canonical_language(language)
        items = [
            item
            for item in items
            if item.get("language", "").lower() == lang_target
        ]

    if keyword is not None:
        kw = keyword.strip().lower()

        def _matches(plugin: dict[str, Any]) -> bool:
            if kw in plugin.get("id", "").lower():
                return True
            if kw in plugin.get("name", "").lower():
                return True
            if kw in plugin.get("description", "").lower():
                return True
            if kw in plugin.get("kind", "").lower():
                return True
            for cap in plugin.get("capabilities", []):
                if kw in str(cap).lower():
                    return True
            return False

        items = [item for item in items if _matches(item)]

    return items


def get_plugin(
    plugin_id: str,
    catalog_path: str | Path | None = None,
    catalog: dict[str, Any] | None = None,
    root_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """Lookup a single plugin by its ID. Returns None if not found.

    中文: 根据插件 ID 获取单个插件元数据对象，未找到返回 None。
    """
    if catalog is None:
        catalog = load_catalog(catalog_path=catalog_path, root_dir=root_dir)

    for item in catalog.get("plugins", []):
        if item.get("id") == plugin_id:
            return item
    return None


def get_profile_plugins(
    profile_name: str,
    catalog_path: str | Path | None = None,
    catalog: dict[str, Any] | None = None,
    root_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return all plugins associated with the given service profile.

    中文: 查询指定服务画像包含的所有插件元数据对象列表。
    """
    return list_plugins(
        profile=profile_name,
        catalog_path=catalog_path,
        catalog=catalog,
        root_dir=root_dir,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for catalog build, inspect, and query.

    中文: Catalog 构建与查询命令行入口。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="Repository root path")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path for plugins-catalog.json (default: dist/plugins/plugins-catalog.json if --build is passed)",
    )
    parser.add_argument("--build", action="store_true", help="Build unified catalog and write to output")
    parser.add_argument("--list", action="store_true", help="List plugins in table format")
    parser.add_argument("--profile", type=str, help="Filter by service profile (e.g. reactor, echo)")
    parser.add_argument("--language", type=str, help="Filter by language (e.g. python, rust, csharp)")
    parser.add_argument("--keyword", type=str, help="Search keyword in ID, name, description, capabilities")
    parser.add_argument("--get", type=str, help="Lookup plugin by ID")
    parser.add_argument("--json", action="store_true", help="Output results in JSON format")

    args = parser.parse_args(argv)
    root = args.root.resolve()

    if args.build or args.output is not None:
        out_path = args.output if args.output else (root / DEFAULT_CATALOG_REL_PATH)
        catalog = build_catalog(output_path=out_path, root_dir=root)
        print(f"✅ Generated catalog with {catalog['totalPlugins']} plugins at: {out_path}")
        return 0

    if args.get:
        item = get_plugin(args.get, root_dir=root)
        if item is None:
            print(f"Error: Plugin '{args.get}' not found.", file=sys.stderr)
            return 1
        print(json.dumps(item, indent=2, ensure_ascii=False))
        return 0

    if args.list or args.profile or args.language or args.keyword:
        plugins = list_plugins(
            profile=args.profile,
            language=args.language,
            keyword=args.keyword,
            root_dir=root,
        )
        if args.json:
            print(json.dumps(plugins, indent=2, ensure_ascii=False))
            return 0

        print(f"\nFound {len(plugins)} plugins:")
        print(f"{'ID':<38} {'Version':<9} {'Language':<9} {'Kind':<20} {'Profiles'}")
        print("-" * 100)
        for p in plugins:
            profiles_str = ", ".join(p.get("profiles", []))
            print(f"{p['id']:<38} {p['version']:<9} {p['language']:<9} {p['kind']:<20} {profiles_str}")
        return 0

    # Default: build in-memory and print summary
    catalog = build_catalog(root_dir=root)
    print(f"Catalog contains {catalog['totalPlugins']} official plugins across {len(catalog['profiles'])} profiles.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
