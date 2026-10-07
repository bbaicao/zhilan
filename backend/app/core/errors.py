"""v2 统一错误：对外错误必须是用户能看懂的中文提示。"""


class RagError(Exception):
    """RAG 层可读错误基类。"""


class ProviderNotConfigured(RagError):
    """模型未配置。"""


class ProviderAuthError(RagError):
    """认证失败（→ **401**，见 `接口说明/02-API契约冻结清单.md` §4 异常语义表）。

    ⚠️ ``回归修复``：本条原写「认证失败**或限流**」，但 ``02`` §4 的异常语义表**只声明了认证失败**
    ⇒ 「限流」**没有独立出口**（既无 429 分支、也无生产者）。措辞已**对齐契约**。
    用户可见的 :data:`AUTH_HINT` 仍提「额度」—— 那是**引导用户检查 Key 与额度**，不是状态声明，故**未动**。
    """


class ProviderTimeout(RagError):
    """模型响应超时。"""


class DocumentParseError(RagError):
    """文档解析失败。"""


class DuplicateDocument(RagError):
    """内容指纹命中已有文档：前端据此给「跳过 / 覆盖重建」两个选项。"""

    def __init__(self, message: str, document_id: int, filename: str) -> None:
        super().__init__(message)
        self.document_id = document_id
        self.filename = filename


NOT_CONFIGURED_HINT = "尚未配置大模型，请在「设置 → API Key 服务」里添加一条服务并选择对话模型（也可直接改 .env）。"
AUTH_HINT = "模型服务认证失败或触发限流，请检查 API Key 和额度。"
TIMEOUT_HINT = "模型响应超时，请稍后重试。"
