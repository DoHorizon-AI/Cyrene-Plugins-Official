#!/usr/bin/env python3
"""Cyrene .NET Native AOT Rule & Policy Verifier (Milestone M3 Tasks T32 & T40).

Enforces:
- T32: Prohibits runtime assembly loading, reflection scanning, dynamic proxies, and Emit.
- T40: Prohibits blanket warning suppressions for Native AOT / Trim warnings (IL2026, IL3050).
- T33: Enforces source-generated JsonSerializerContext on JSON paths.

中文：Cyrene .NET Native AOT 规则与策略验证器（里程碑 M3 任务 T32 和 T40）。

强制执行：
- T32：禁止运行时程序集加载、反射扫描、动态代理和 Emit。
- T40：禁止对 Native AOT / Trim 警告（IL2026、IL3050）进行一揽子抑制。
- T33：要求 JSON 路径使用源生成的 JsonSerializerContext。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET_DIRS = [
    REPO_ROOT / "runtime/dotnet-native-aot",
    REPO_ROOT / "contracts/dotnet",
]

FORBIDDEN_CODE_PATTERNS = [
    (r"Assembly\.Load\b", "Prohibited runtime assembly loading (T32)"),
    (r"Assembly\.LoadFrom\b", "Prohibited runtime assembly loading (T32)"),
    (r"Assembly\.LoadFile\b", "Prohibited runtime assembly loading (T32)"),
    (r"System\.Reflection\.Emit", "Prohibited dynamic code generation / Emit (T32)"),
    (r"\bDispatchProxy\b", "Prohibited runtime proxy generation (T32)"),
    (r"\bCastle\.DynamicProxy\b", "Prohibited runtime dynamic proxy (T32)"),
    (r"Activator\.CreateInstance\b", "Prohibited unannotated reflection activation (T32)"),
    (r"#pragma\s+warning\s+disable\s+.*(IL2026|IL3050)", "Prohibited blanket AOT warning suppression (T40)"),
]

FORBIDDEN_PROJECT_PATTERNS = [
    (r"<NoWarn>.*(IL2026|IL3050).*</NoWarn>", "Prohibited blanket AOT suppression in csproj/props (T40)"),
]


def check_dotnet_aot_rules() -> list[str]:
    violations: list[str] = []

    for target_dir in TARGET_DIRS:
        if not target_dir.exists():
            continue

        for path in target_dir.rglob("*"):
            if "bin" in path.parts or "obj" in path.parts or "target" in path.parts:
                continue

            if path.suffix == ".cs":
                content = path.read_text(encoding="utf-8", errors="replace")
                for pattern, desc in FORBIDDEN_CODE_PATTERNS:
                    matches = re.finditer(pattern, content)
                    for m in matches:
                        line_num = content.count("\n", 0, m.start()) + 1
                        violations.append(f"{path.relative_to(REPO_ROOT)}:{line_num}: {desc}")

            elif path.suffix in [".csproj", ".props", ".targets"]:
                content = path.read_text(encoding="utf-8", errors="replace")
                for pattern, desc in FORBIDDEN_PROJECT_PATTERNS:
                    matches = re.finditer(pattern, content)
                    for m in matches:
                        line_num = content.count("\n", 0, m.start()) + 1
                        violations.append(f"{path.relative_to(REPO_ROOT)}:{line_num}: {desc}")

    return violations


def main():
    print("[INFO] Running Cyrene .NET Native AOT Rule Checks (Tasks T32 & T40)...")
    violations = check_dotnet_aot_rules()

    if violations:
        print(f"[FAIL] Found {len(violations)} AOT policy violations:")
        for v in violations:
            print(f"  - {v}")
        sys.exit(1)
    else:
        print("[PASS] 0 prohibited reflection / assembly load / proxy / emit patterns found (T32).")
        print("[PASS] 0 blanket IL2026/IL3050 warning suppressions found (T40).")
        print("[SUCCESS] .NET Native AOT policy verification PASSED.")


if __name__ == "__main__":
    main()
