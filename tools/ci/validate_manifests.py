#!/usr/bin/env python3
"""Validate Plugin manifests against manifests/plugin.manifest.schema.json.

This gate runs before capability catalog generation so malformed manifests
never become catalog input. jsonschema is a pinned CI/dev dependency only;
production runtime packages must not depend on it.

中文:根据 manifests/plugin.manifest.schema.json 验证 Plugin 清单。

中文:此门禁在生成能力目录之前运行,确保格式错误的清单不会成为目录输入。jsonschema 仅作为固定版本的 CI/开发依赖;生产运行时软件包不得依赖它。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCHEMA_PATH = "manifests/plugin.manifest.schema.json"
MANIFEST_GLOB = "plugins/*/*/plugin.manifest.json"
PINNED_DEPENDENCY = "jsonschema==4.23.0"


class ManifestSchemaError(ValueError):
    """Raised when a plugin manifest does not satisfy the canonical schema.

        中文:当 Plugin 清单不符合规范架构时引发。"""


def _load_validator(root: Path):
    """Build a draft 2020-12 validator from the canonical repository schema.

        中文:根据仓库规范架构构建一个 Draft 2020-12 验证器。"""

    try:
        import jsonschema
    except ImportError as error:
        raise ManifestSchemaError(
            "jsonschema is required for manifest validation (pinned CI/dev dependency: "
            f"{PINNED_DEPENDENCY})"
        ) from error
    schema_path = root / SCHEMA_PATH
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ManifestSchemaError(f"Cannot parse schema {SCHEMA_PATH}: {error}") from error
    return jsonschema.Draft202012Validator(schema)


def validate_root(root: Path) -> list[str]:
    """Validate every plugin manifest and return the validated relative paths.

        中文:验证每份 Plugin 清单并返回已验证的相对路径。"""

    validator = _load_validator(root)
    manifests = sorted(root.glob(MANIFEST_GLOB))
    if not manifests:
        raise ManifestSchemaError("No plugin manifests found under plugins/*/*")
    validated: list[str] = []
    for manifest_path in manifests:
        relative = manifest_path.relative_to(root).as_posix()
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ManifestSchemaError(f"Cannot parse {relative}: {error}") from error
        errors = sorted(validator.iter_errors(data), key=lambda error: list(error.path))
        if errors:
            details = "; ".join(
                f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
                for error in errors[:5]
            )
            raise ManifestSchemaError(f"{relative} violates {SCHEMA_PATH}: {details}")
        validated.append(relative)
    return validated


def main(argv: list[str] | None = None) -> int:
    """Run manifest schema validation from the command line.

        中文:从命令行运行清单架构验证。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        validated = validate_root(args.root.resolve())
    except ManifestSchemaError as error:
        print(f"MANIFEST_SCHEMA: FAIL: {error}", file=sys.stderr)
        return 2
    print(f"MANIFEST_SCHEMA: PASS manifests={len(validated)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
