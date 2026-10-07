"""出网守卫的**复用薄壳**（专项升级 · `回归修复`）。

## 为什么要有这一层

用户可填 URL 的出网面不止 web 工具一处（本轮立项的两处见缺陷清单 `回归修复`），
而**判定本体**一直只有一份：``app.agent.web.ssrf.assert_url_is_safe``。它落在 ``agent``
层，按分包边界（``api → agent/… → pipeline/vector/session → db/core``）**下层不得反向**
**import 上层** —— 本模块 import 它，是这条方向上**唯一**一条反向边（已如实登记，见下）。

本模块就是为这条边界准备的**唯一**接缝：core 层需要出网守卫时**只**调这里的两个函数，
不在本模块里重写任何一条判定。⇒ 地址黑名单、公网判据符号、解嵌逻辑在本仓仍然
**只有 ssrf.py 一份**（本文件对此有机器判据：`B-05` 逐条扫源码里的地址字面量与判定符号名）。所以那批符号名在本文件里**一次都不出现**，连注释与 docstring 里也不写。

## 分层与延迟 import（**实测**结论，如实登记）

卡内预案是「若实测该 import 会破边界，改**延迟 import** 并在 docstring 写明理由」。
逐条实测（cwd = ``<repo>/backend``、``PYTHONPATH=<repo>/backend``）的结果是：
**没有测到任何一条会破边界**，因此**不采用**延迟 import，而是写成模块级直接 import。

① 卡内点名禁的是 ``app.api.*`` —— 本模块**没有**、也不会 import 它。
   实测：把 ``import app.api.settings_routes`` 与本模块放在同一个进程里，两种 import 顺序
   都通过；本模块单独 import 时 ``app.api`` **不出现在** ``sys.modules`` 里。

② 本模块 import 的**唯一**本仓符号是 ``app.agent.web.ssrf``。实测：
   ``import app.core.net_guard`` 在 ``backend`` 目录下（**不经** ``tests/`` 的引导）也通过，
   且不把 ``app.api`` 拖进 ``sys.modules``；在**仅装推荐依赖**（``requirements.txt``，
   无 fastapi）的解释器里同样通过 —— 该模块纯标准库、零本仓 import。

③ 延迟 import **在这条依赖方向上是空操作**：延迟只推后查找，不取消依赖；
   ``core/__init__`` 不 import 本模块、``app.agent`` 的 ``__init__`` 也不 import 本模块，
   所以把 import 挪进函数体**改变不了**任何一条 import 边，只是让代码形状更难判
   （源码腿要判「顶层没 import 上层」反倒绕）。

④ 而且延迟 import 有一个**实测**得到的负面形状：``except _ssrf().UrlNotAllowed as exc``
   这种写法（本模块第一版）会让 ``except`` 表达式**每次求值**，把「判定入口只许一处」
   这条结构判据糊掉；改回模块级 import 后，判定入口收敛成一个模块级赋值
   （见 :data:`assert_url_is_safe` 的注释）。

⚠️ **一条如实登记的边界事实（不粉饰）**：本模块**在导入期** import 了
``app.agent.web.ssrf``，即 ``core`` 在**层序上反向**引用了 ``agent`` 层。这是卡内硬纪律①
（「判定本体一律复用，⛔ 不得重写」）压过分层洁癖后的**已知取舍**。本仓**没有**任何门禁
断言「core 不得 import ``app.agent``」（``check_frozen_paths.py`` / ``i2-diff`` /
``contract_diff`` / alembic 四道门禁都不看这一条；同型的 ``R2-03`` 腿只禁
``app.core.config`` 里出现 ``agent``，本模块**不是**它管的文件）。

⇒ **两条路都带**这条反向依赖（延迟 import 一条也不少），所以取**形状更可判**的那条。
若将来真要消掉它，唯一干净的方向是**把判定本体下沉到 ``core``**（那是另一次搬迁，
超出 ``回归修复`` 本轮范围）—— 登记为已知限制。

## 逃生门（与 web 面**同一条开关**、同一个口径）

:func:`allow_any_host` **每次调用现读** ``settings.WEB_SSRF_ALLOW_ANY_HOST``：
固化在 import 期会让「改 ``.env`` 后生效」变成「**重启才生效**」，而逃生门恰恰是
出事时才需要现场打开的开关。口径与 ``app/agent/web/client.py::_guard_hook`` **逐字对齐**：
开 = 放行 **且每次放行留一条 WARNING**；关 = 拒。

## 拒绝时的异常类型（**故意**不是 ``HTTPException``）

:class:`OutboundBlocked` 继承 ``ValueError``：``core`` 层不认识 fastapi，抛
``HTTPException`` 会把 web 框架拖进核心层。接线方（``api`` 层）自己做映射即可。
"""

from __future__ import annotations

import logging

from app.agent.web import ssrf
from app.core.config import settings

# 本模块的日志通道。判据侧一律**按 logger 名过滤**：``caplog`` 是**全 logger** 收集，
# 不过滤就会把网络栈（传输层客户端 / 事件循环）的 WARNING 也收进来（假红）。
logger = logging.getLogger(__name__)


class OutboundBlocked(ValueError):
    """出网地址被安全判定拒绝（消息是**可读中文**，并写明这次出网**是为了什么**）。

    继承 ``ValueError`` 而不是任何 web 框架的异常：core 层不得依赖 fastapi（见模块 docstring）。
    """


# 判定本体在本模块里的**唯一**引用点（``ssrf`` 的那一个函数在这里被换成模块属性）。
# ⚠️ 写成模块级名字有两个用途，都不是为了包例外：
#    ① 让「薄壳只经这一个点判定」可**结构判**（用例数这个引用点，只许一处）；
#    ② 让接线腿能把它换成**间谍**并验「现读到的开关有没有被**传给判定**」。
#    没有 ②，「忘了传开关」这种缺陷会被「直接调 ssrf」的写法与「直接换 ``ssrf``」的桩
#    **同时**掩盖成假绿（本文件第一版正是如此，已由实测抓出）。
#    ⚠️ 这是**公开**名字：接线方若只想要值不许改它（它是判定入口，不是可调参数）。
assert_url_is_safe = ssrf.assert_url_is_safe


def allow_any_host() -> bool:
    """SSRF 逃生门当前是否开启（默认 ``False``）。

    ⚠️ **每次调用现读** ``settings``，**不得**固化成模块常量：固化会让「改 ``.env``
    后生效」变成「重启才生效」，而这条开关的意义就是出问题时能当场打开
    （回归钉 = ``test_b081_b04_*``；变异 ``M-081-A1`` 把本函数改成返回常量 ``False``
    ⇒ 该腿当场红）。
    """
    return bool(getattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", False))


def assert_outbound_url_safe(url: str, *, purpose: str = "") -> str:
    """校验一次出网是否允许；通过则返回**第一个已校验的地址**（供连接侧固定）。

    ``purpose`` 写清「这次出网是为了什么」（如 ``文件导入`` / ``订阅源抓取``）——
    它**只进拒绝文案**，不进放行路径：用户看到「被拒」时必须同时知道**是哪一件事**被拒。

    判定本体**一律复用** :func:`app.agent.web.ssrf.assert_url_is_safe`，本函数**不**自判
    地址（不复制黑名单、不抄判据符号名）。``resolver`` 用默认（即 ``ssrf.resolve_host``）。

    失败抛 :class:`OutboundBlocked`（``ValueError``，**不是** ``HTTPException``）。
    """
    try:
        address = assert_url_is_safe(str(url or ""), allow_any_host=allow_any_host())
    except ssrf.UrlNotAllowed as exc:
        detail = f"（{purpose}）" if purpose else ""
        raise OutboundBlocked(f"{exc}{detail}") from exc
    if allow_any_host():
        # 逃生门开启时**每次放行都留痕**（默认关 ⇒ 这条分支不存在）。口径与 web 面
        # `app/agent/web/client.py::_guard_hook` 逐字对齐（同一句文案、同一个 logger 名）。
        logger.warning(
            "SSRF 逃生门已开启（WEB_SSRF_ALLOW_ANY_HOST）：放行 %s", _host_of(url)
        )
    return address


def _host_of(url: str) -> str:
    """从 URL 里取出主机名，**只用于日志**（取不到就回退原串，绝不抛、绝不再判一次）。

    本模块刻意不 import ``urllib.parse``：解析属于判定本体（``ssrf.py``）的职责，
    这里再解析一次等于开第二个真相源。
    """
    text = str(url or "")
    try:
        without_scheme = text.split("://", 1)[1] if "://" in text else text
        authority = without_scheme.split("/", 1)[0]
        authority = authority.rsplit("@", 1)[-1]
        if authority.startswith("["):  # IPv6 字面量：``[::1]:9527``
            return authority.split("]", 1)[0] + "]"
        return authority.split(":", 1)[0]
    except Exception:  # noqa: BLE001 —— 日志字段的取值方式**永不**影响判定结果
        return text


__all__ = [
    "OutboundBlocked",
    "allow_any_host",
    "assert_outbound_url_safe",
    "assert_url_is_safe",
]
