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
