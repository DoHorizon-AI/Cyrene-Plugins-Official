"""Typed errors shared by the WeCom connector adapters.

中文:WeCom connector adapters 共用的类型化错误。
"""


class ConnectorError(RuntimeError):
    """Failure returned at the canonical connector boundary.

    中文:在规范 connector 边界返回的失败。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
