"""Keep the Plugin Runtime dependency metadata aligned with its supported ABI lanes.

中文:确保 Plugin Runtime 依赖元数据与已支持的 ABI 范围一致。
"""

from __future__ import annotations

from pathlib import Path

import tomllib
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version


def test_runtime_dependency_bounds_cover_supported_and_exclude_untested_lanes() -> None:
    """Accept the exercised oldest and Yield lanes without claiming future majors."""
    project_file = Path(__file__).parents[1] / "pyproject.toml"
    project = tomllib.loads(project_file.read_text(encoding="utf-8"))["project"]
    dependencies = {
        requirement.name.lower(): SpecifierSet(str(requirement.specifier))
        for requirement in map(Requirement, project["dependencies"])
    }

    assert Version("1.62.3") in dependencies["grpcio"]
    assert Version("1.83.0") in dependencies["grpcio"]
    assert Version("1.84.0") not in dependencies["grpcio"]
    assert Version("4.25.1") in dependencies["protobuf"]
    assert Version("7.36.0") in dependencies["protobuf"]
    assert Version("8.0.0") not in dependencies["protobuf"]
