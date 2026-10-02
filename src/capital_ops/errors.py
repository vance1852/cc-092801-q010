"""联合资本承诺与结算服务向 API 和 CLI 暴露的稳定错误。"""


class SyndicateError(RuntimeError):
    code = "syndicate_error"
    status = 400


class NotFound(SyndicateError):
    code = "not_found"
    status = 404


class Conflict(SyndicateError):
    code = "conflict"
    status = 409


class Forbidden(SyndicateError):
    code = "forbidden"
    status = 403


class InvalidState(SyndicateError):
    code = "invalid_state"
    status = 409


class ValidationFailed(SyndicateError):
    code = "validation_failed"
    status = 422
