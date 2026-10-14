"""与 HTTP 无关的业务异常定义。"""


class BusinessError(Exception):
    """提供可在应用边界映射的稳定错误代码和安全提示。"""

    code: str
    message: str

    def __init__(self) -> None:
        """使用固定业务提示初始化异常，不携带用户输入。"""
        super().__init__(self.message)


class EmailAlreadyExists(BusinessError):
    """表示规范化邮箱已被其他账户使用。"""

    code = "EMAIL_ALREADY_EXISTS"
    message = "邮箱已注册"


class InvalidCredentials(BusinessError):
    """对邮箱不存在与密码错误使用相同的对外提示。"""

    code = "INVALID_CREDENTIALS"
    message = "邮箱或密码错误"


class InvalidToken(BusinessError):
    """表示令牌或关联用户无法通过认证。"""

    code = "INVALID_TOKEN"
    message = "令牌无效或已过期"


class TripNotFound(BusinessError):
    """对行程不存在与不属于当前用户使用相同的对外提示。"""

    code = "TRIP_NOT_FOUND"
    message = "行程不存在"


class TripBusy(BusinessError):
    """表示行程正被另一轮对话占用。"""

    code = "TRIP_BUSY"
    message = "上一条消息仍在处理，请稍后再试"


class DraftVersionConflict(BusinessError):
    """表示客户端提交的草稿版本不是行程当前版本。"""

    code = "DRAFT_VERSION_CONFLICT"
    message = "行程草稿已更新，请刷新后再确认"


class TripNotConfirmable(BusinessError):
    """表示行程尚未生成草稿，不能确认。"""

    code = "TRIP_NOT_CONFIRMABLE"
    message = "行程尚未生成草稿，暂不能确认"


class TripTurnFailed(BusinessError):
    """表示本轮规划执行失败或超时，未写入任何结果。"""

    code = "TRIP_TURN_FAILED"
    message = "行程规划服务暂时不可用，请稍后重试"
