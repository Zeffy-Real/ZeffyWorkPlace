"""LLM 自定义异常分层。

P0 阶段建壳子，接口先定型；P1 的 Agent 循环基于这些异常做重试/降级/熔断。

- LLMConfigError：    配置错误（缺 key、provider 不合法等）→ 不重试
- LLMConnectionError：连接/超时类错误 → 可重试
- LLMProviderError：  服务商返回错误（限流、拒绝、429/5xx 等）→ 按错误码决定是否重试
"""


class LLMError(Exception):
    """LLM 调用基类异常。"""


class LLMConfigError(LLMError):
    """LLM 配置错误：无 api key、provider 未识别、缺少必要配置。不重试。"""


class LLMConnectionError(LLMError):
    """LLM 网络连接/超时错误。可重试。"""


class LLMProviderError(LLMError):
    """LLM 服务商返回错误：限流、被拒、上游 5xx。"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
