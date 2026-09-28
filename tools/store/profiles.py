#!/usr/bin/env python3
"""Cyrene Service Profile Mapping and Helper Utilities.

Loads standard service profile to plugin mappings from profiles.yaml
with a transparent JSON fallback (profiles.json) when PyYAML is unavailable.

中文: Cyrene 服务画像映射及读取工具。
从 profiles.yaml 读取标准服务画像与插件清单映射，
在缺少 PyYAML 依赖时自动回退读取 profiles.json。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

STORE_DIR = Path(__file__).resolve().parent
REPO_ROOT = STORE_DIR.parents[1]
YAML_PATH = STORE_DIR / "profiles.yaml"
JSON_PATH = STORE_DIR / "profiles.json"


def _normalize_profiles(data: Any) -> dict[str, list[str]]:
    """Normalize raw parsed YAML/JSON profiles dictionary into {profile: [plugin_ids]}.

    Supports both:
    - {profile: [plugin_id, ...]}
    - {profile: {"plugins": [plugin_id, ...], "description": ...}}

    中文: 将原始解析后的 profiles 配置归一化为 {画像名: [插件ID列表]} 结构。
    """
    if not isinstance(data, dict):
        return {}
    profiles_dict = data.get("profiles", data)
    if not isinstance(profiles_dict, dict):
        return {}

    normalized: dict[str, list[str]] = {}
    for name, entry in profiles_dict.items():
        if isinstance(entry, list):
            normalized[name] = [str(x) for x in entry]
        elif isinstance(entry, dict) and "plugins" in entry:
            plugins = entry.get("plugins", [])
            if isinstance(plugins, list):
                normalized[name] = [str(x) for x in plugins]
            else:
                normalized[name] = []
        elif isinstance(entry, dict):
            normalized[name] = []
    return normalized


def load_profiles(config_path: Path | None = None) -> dict[str, list[str]]:
    """Load standard service profile to plugin ID mappings.

    Reads from profiles.yaml if available and pyyaml is present,
    otherwise falls back to profiles.json.

    中文: 读取标准服务画像到插件 ID 的映射关系。
    优先读取 profiles.yaml，若缺少环境依赖则自动回退至 profiles.json。
    """
    target = config_path
    if target is None:
        target = YAML_PATH if YAML_PATH.is_file() else JSON_PATH

    if target.suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]

            with open(target, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                return _normalize_profiles(data)
        except ImportError:
            # Fall back to profiles.json if PyYAML is not installed
            if JSON_PATH.is_file():
                with open(JSON_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return _normalize_profiles(data)
        except Exception:
            if JSON_PATH.is_file():
                with open(JSON_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return _normalize_profiles(data)
            raise

    # Fallback / Direct JSON load
    if JSON_PATH.is_file():
        with open(JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return _normalize_profiles(data)
    elif YAML_PATH.is_file():
        try:
            import yaml  # type: ignore[import-untyped]

            with open(YAML_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                return _normalize_profiles(data)
        except (OSError, yaml.YAMLError, ValueError, TypeError):
            pass
    return {}


def get_plugins_for_profile(profile_name: str, config_path: Path | None = None) -> list[str]:
    """Return plugin IDs matching a given service profile.

    中文: 查询指定服务画像包含的所有插件 ID 列表。
    """
    profiles = load_profiles(config_path)
    return profiles.get(profile_name, [])


def get_profiles_for_plugin(plugin_id: str, config_path: Path | None = None) -> list[str]:
    """Return service profiles that include the given plugin ID.

    中文: 查询包含指定插件 ID 的所有服务画像列表。
    """
    profiles = load_profiles(config_path)
    matching: list[str] = []
    for profile, plugins in profiles.items():
        if plugin_id in plugins:
            matching.append(profile)
    return matching


def main(argv: list[str] | None = None) -> int:
    """CLI helper to inspect and query service profiles.

    中文: 用于查看与检索服务画像的命令行入口。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=str, help="Lookup plugin IDs for a profile (e.g. reactor, echo)")
    parser.add_argument("--plugin", type=str, help="Lookup profiles containing a plugin ID")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    args = parser.parse_args(argv)

    profiles = load_profiles()
    if args.profile:
        result = get_plugins_for_profile(args.profile)
        if args.json:
            print(json.dumps({args.profile: result}, indent=2))
        else:
            print(f"Profile '{args.profile}': {len(result)} plugins")
            for item in result:
                print(f"  - {item}")
        return 0

    if args.plugin:
        result = get_profiles_for_plugin(args.plugin)
        if args.json:
            print(json.dumps({args.plugin: result}, indent=2))
        else:
            print(f"Plugin '{args.plugin}' is in profiles: {', '.join(result)}")
        return 0

    if args.json:
        print(json.dumps(profiles, indent=2))
    else:
        print("Available service profiles:")
        for profile, plugins in profiles.items():
            print(f"  [{profile}] ({len(plugins)} plugins):")
            for p in plugins:
                print(f"    - {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
