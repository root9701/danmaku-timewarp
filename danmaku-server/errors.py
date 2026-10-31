"""业务错误类型。

单独成模块的原因: 对齐核心(alignment.py)与投票判定(align_vote.py)不能 import
server(否则形成循环), 但都要抛出同一个异常类供 error_middleware 识别。
server 从这里导入, 因此 `server.ApiError` 仍是同一个类。
"""


class ApiError(Exception):
    """业务错误: 统一转为 {"ok":false,"error":"中文原因"} 响应。"""

    def __init__(self, msg: str):
        super().__init__(msg)
        self.msg = str(msg)
