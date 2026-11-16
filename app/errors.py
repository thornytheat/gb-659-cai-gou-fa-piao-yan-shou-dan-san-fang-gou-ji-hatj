class DomainError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        # 稳定原因码进入异常文本，便于日志与测试按码定位。
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status

