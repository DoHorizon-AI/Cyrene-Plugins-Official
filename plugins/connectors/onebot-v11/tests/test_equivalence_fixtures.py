###############################################################################
# File: test_equivalence_fixtures.py
# Module: Cyrene Plugins Official
# Role: Ensure the checked-in fixture still matches the Python behavior baseline.
#
# 模块：Cyrene Plugins Official
# 职责：确保提交的 fixture 仍与 Python 行为基线一致。
###############################################################################
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
TOOLS_ROOT = REPOSITORY_ROOT / "plugins/connectors/onebot-v11/tools"
GENERATOR_PATH = TOOLS_ROOT / "generate_equivalence_fixtures.py"
GENERATOR_SPEC = importlib.util.spec_from_file_location(
    "generate_equivalence_fixtures", GENERATOR_PATH
)
if GENERATOR_SPEC is None or GENERATOR_SPEC.loader is None:
    raise RuntimeError("fixture generator could not be loaded")
GENERATOR = importlib.util.module_from_spec(GENERATOR_SPEC)
GENERATOR_SPEC.loader.exec_module(GENERATOR)
build_fixture = GENERATOR.build_fixture


FIXTURE_PATH = Path(__file__).parent / "fixtures/native-equivalence.json"


def test_checked_in_fixture_matches_python_baseline() -> None:
    """Detect semantic drift before the native runtime becomes formal."""

    checked_in = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert checked_in == build_fixture()
