"""工具正文**落盘件**（专项工具升级-2 · R2-05 · 阶段一）。

## 这个模块解决什么

工具正文超阈值时，通道里只留**头尾预览 + 标记**（R2-06）；**全文不再丢** —— 落盘一份，
标记里带一个**可核对的身份**（内容哈希前 12 位）。模型与人都能顺着 id 把被省略的那段找回来。

## 口径（R2-05 卡片 + 开工包 D-回归检查 / D-回归检查 / D-回归检查 / D-回归检查）

- **落盘根** = **数据根**下的 ``data/tool_artifacts/<yyyyMMdd>/``，走
  :attr:`app.core.config.Settings.tool_artifact_path`（与 ``upload_path`` / ``qdrant_path`` /
  ``sqlite_file`` **同型**：相对路径按 ``ROOT_DIR`` 解析、可 monkeypatch）。
  ⚠️ **禁止** ``main._roots()``：它的真身在 ``backend/main.py:41``，而 ``main.py`` 顶层
  ``import app.api.*`` ⇒ 从 ``app/**`` 反拉**成环**（违分包边界）；冻结态 ``_roots()[0]``
  还优先 ``_MEIPASS`` / ``<exe>/_internal``（**只读资源根**，写盘必失败）。
- ``id`` = **内容哈希（sha256）前 12 位**。⚠️ **这是本阶段新立的口径，仓内没有先例**
  （既有是 ``[:8]`` / ``[:16]``，以及一个**非内容哈希**的 uuid 12 位）⇒ 别去找「对齐先例」。
- **幂等**：同一份内容 ⇒ 同一个 id ⇒ 同一个文件名；已存在就不重写（可核对、可去重）。
- 失败（盘满 / 无权限 / **超 2 MiB**）⇒ **返回失败信号，不抛**（I5；回落由 R2-06 的调用点做）。
- 落盘目录**不是**产品数据，可随时删。

## 为什么取时在这个模块（开工包 D-回归检查）

``<yyyyMMdd>`` 的取时**必须**在这里算，**严禁**在 ``runner.py`` 出现 ``datetime.now(``
这个字面（**连注释与文档串里出现也算**）：既有**源码级**判据
``tests/api/test_tool_context_t0.py:510-516`` 读 ``runner.__file__`` 全文后断言
``"datetime.now(" not in text``（实测是纯子串判定）⇒ 在 runner 里拼日期**必红**。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path

from app.core import derived_counters
from app.core.config import settings

log = logging.getLogger(__name__)

#: 单件硬上限（**事实仓 ``:157`` 的建议值**，终检未标定）——超过 ⇒ 不落盘、走回落形态 + warning。
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024

#: 内容哈希取前几位作为 id（可核对、可去重、幂等）
ARTIFACT_ID_LENGTH = 12


def _fail(reason: str) -> None:
    """落盘失败的**唯一**出口：记日志 + 计一次降级计数、返回失败信号（**不抛**）。

    ⚠️ 这是 I5「工具失败转观察，绝不炸穿」在**件级**的落点；R2-06 的调用点据此走回落形态（A8）。
    🔴 `回归修复` **#30**（2026-09-28 批次三）：计进既有键 `tool_result_channel`（工具结果通道降级）。
    ⚠️ 计数点必须落在这**唯一出口内部**：:func:`save` 的两条失败路径（超上限 / 写盘异常）都经它
    ⇒ 若在两个调用点各计一次，同一件失败会被记**两笔**（重复计数）。
    """
    log.warning("工具正文落盘失败（走回落形态）：%s", reason)
    derived_counters.bump("tool_result_channel")
    return None


def artifact_id(text: str) -> str:
    """内容哈希 sha256 的前 12 位（**纯函数**，可核对 / 可去重）。"""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:ARTIFACT_ID_LENGTH]


def artifact_dir(today: str | None = None) -> Path:
    """当天的落盘目录 ``<落盘根>/<yyyyMMdd>``；``today`` 只给判据注入用。"""
    stamp = today or datetime.now().strftime("%Y%m%d")
    return Path(settings.tool_artifact_path) / stamp


def save(text: str) -> str | None:
    """把 ``text`` 落盘并返回它的 ``id``；**失败返回 ``None``，绝不抛**。"""
    value = str(text or "")
    raw = value.encode("utf-8")
    if len(raw) > MAX_ARTIFACT_BYTES:
        return _fail(f"单件 {len(raw)} 字节 > 上限 {MAX_ARTIFACT_BYTES} 字节")
    identity = artifact_id(value)
    try:
        directory = artifact_dir()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{identity}.txt"
        if not target.exists():  # 幂等：同内容同 id，已存在就不重写
            target.write_bytes(raw)
    except Exception as exc:  # noqa: BLE001 —— 落盘失败绝不抛穿（I5 / A8）
        return _fail(f"{exc.__class__.__name__}: {exc}")
    return identity


__all__ = [
    "ARTIFACT_ID_LENGTH",
    "MAX_ARTIFACT_BYTES",
    "artifact_dir",
    "artifact_id",
    "save",
]
