#!/usr/bin/env python3
"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 assert_no_test_skips.py                                          │
│  Module: tools.ci                                                    │
│  Role: Reject skipped JUnit/Surefire cases in required CI lanes.     │
│                                                                     │
│  模块职责：拒绝必需 CI lane 中被静默跳过的 JUnit/Surefire 测试。          │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    """Parse the Maven/Surefire report root supplied by the CI job.

        中文:解析 CI 作业提供的 Maven/Surefire 报告根目录。"""

    parser = argparse.ArgumentParser(
        description="Fail when any JUnit/Surefire report contains skipped cases."
    )
    parser.add_argument(
        "report_root",
        type=Path,
        help="Directory containing Maven target/surefire-reports directories.",
    )
    return parser.parse_args()


def _skipped_cases(report_path: Path) -> list[str]:
    """Return fully qualified test names marked as skipped in one XML report.

        中文:返回一个 XML 报告中标记为跳过的完整测试名称。"""

    root = ET.parse(report_path).getroot()
    suite_name = root.attrib.get("name", report_path.stem)
    skipped: list[str] = []
    for case in root.findall(".//testcase"):
        if case.find("skipped") is None:
            continue
        class_name = case.attrib.get("classname", suite_name)
        test_name = case.attrib.get("name", "<unnamed>")
        skipped.append(f"{class_name}#{test_name}")
    return skipped


def main() -> int:
    """Inspect every Surefire report and fail closed on skipped cases.

        中文:检查每份 Surefire 报告,并在存在跳过用例时按失败即拒绝处理。"""

    args = _parse_args()
    report_root = args.report_root.resolve()
    reports = sorted(report_root.rglob("TEST-*.xml"))
    if not reports:
        print(f"No Surefire reports found below {report_root}", file=sys.stderr)
        return 2

    skipped: list[tuple[Path, str]] = []
    for report in reports:
        for case in _skipped_cases(report):
            skipped.append((report, case))

    if skipped:
        print("Required Java tests were skipped:", file=sys.stderr)
        for report, case in skipped:
            print(f"  {report.relative_to(report_root)}: {case}", file=sys.stderr)
        return 1

    print(f"No skipped Java tests found in {len(reports)} Surefire reports.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
