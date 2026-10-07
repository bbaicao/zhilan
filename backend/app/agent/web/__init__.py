"""联网能力包（阶段 4 · 条目 4.1）：服务商登记表 + 搜索/抓取客户端。

三层分工，别互相越界：

- :mod:`app.agent.web.spec` —— 服务商与渠道的**数据表**（设置页与工具层共用的唯一真相源）；
- :mod:`app.agent.web.client` —— **纯能力**：``search_web`` / ``fetch_page`` / ``html_to_text`` /
  ``web_material``；不发 ToolObservation、不进工具清单，因此可被别的域（如笔记补全）直接复用；
- :mod:`app.agent.tools.web_search` / :mod:`app.agent.tools.fetch_page` —— **契约工具**：
  把上面的能力包成 ``ToolSpec``（含参数模型与 ``needs_web=True``）。

本包**不认识 CubeLoop**（框架适配在 ``app/agent/bootstrap.py``），也不 import ``app.api``
（分层铁律：``agent`` 与 ``api`` 反向 import 会把契约适配层拖进工具层）。

这里只做「公共名字的转出口」，让 ``from app.agent.web import search_web`` 这类写法成立；
真正的实现永远只有一份，在 ``client`` 里。
"""

from app.agent.web.client import (
    WebHit,
    WebToolError,
    fetch_page,
    html_to_text,
    search_web,
    web_material,
)
from app.agent.web.pinning import PinnedBackend, make_backend
from app.agent.web.spec import (
    CHANNELS,
    CHANNEL_API,
    CHANNEL_LLM,
    WEB_PROVIDERS,
    WebProvider,
    normalize_provider,
    provider_spec,
)

__all__ = [
    "CHANNELS",
    "CHANNEL_API",
    "CHANNEL_LLM",
    "PinnedBackend",
    "WEB_PROVIDERS",
    "WebHit",
    "WebProvider",
    "WebToolError",
    "fetch_page",
    "html_to_text",
    "make_backend",
    "normalize_provider",
    "provider_spec",
    "search_web",
    "web_material",
]
