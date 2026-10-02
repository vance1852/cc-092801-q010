"""联合资本承诺与结算服务向 API 和 CLI 暴露的稳定错误。"""


class CapitalOpsError(RuntimeError):
    code = "capital_error"
    status = 400


class NotFound(CapitalOpsError):
    code = "not_found"
    status = 404


class Conflict(CapitalOpsError):
    code = "conflict"
    status = 409


class Forbidden(CapitalOpsError):
    code = "forbidden"
    status = 403


class InvalidState(CapitalOpsError):
    code = "invalid_state"
    status = 409

    def __init__(self, message: str = "状态不允许该操作", details: object | None = None) -> None:
        super().__init__(message)
        self.details = details


class ValidationFailed(CapitalOpsError):
    code = "validation_failed"
    status = 422
