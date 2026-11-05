"""Dump Exchange's operation tables by importing the real module.

Runs in a virtual environment with Exchange installed. 中文：导入真实模块，导出 Exchange 的操作表。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from cyrene_exchange_product.integrations import connector_operations as operations


def main() -> None:
    # The virtual environment supplies dependencies; the code under test must be the pinned checkout.
    checkout = Path(sys.argv[2]).resolve()
    if not Path(operations.__file__).resolve().is_relative_to(checkout):
        raise SystemExit(
            f"imported {operations.__file__}, not the checkout at {checkout}"
        )
    tools = operations._TOOL_IDS_BY_PROVIDER
    dump = {
        "toolIds": {provider: dict(rows) for provider, rows in tools.items()},
        "mutating": sorted(operations._MUTATING_OPERATIONS),
        "bindingTools": {
            provider: operations.connector_binding_tools(provider) for provider in tools
        },
    }
    with open(sys.argv[1], "w", encoding="utf-8") as output:
        json.dump(dump, output)


if __name__ == "__main__":
    main()
