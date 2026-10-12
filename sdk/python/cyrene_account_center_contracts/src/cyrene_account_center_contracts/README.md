# Account Center contract package

This package provides typed DTOs and canonical signing helpers for `account.center.v1`.

| File | Responsibility |
| --- | --- |
| `contracts.py` | Public method DTOs, resource values, and trusted-context signing/verification |
| `__init__.py` | Supported package exports |
| `py.typed` | Marks this package as typed |

Read `contracts.py` to check exact callback and sender-resolution request shapes before building a transport adapter.
