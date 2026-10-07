"""条目 6.6 · 桌面默认配置与向量库锚点回归钉（两条安装态发现）。

## ① 桌面默认配置必须**默认启用 MinerU**

`MINERU_ENABLE` 的代码默认是 `False`（开发态"一律 pypdf、不 import 重依赖"），而**PDF 的扫描件分流**
与**图片 OCR** 都挂在它下面。交付默认档若不含这一行：

* 导入模型包**等于白导**——扫描件走 pypdf 拿不到任何文字，只报「该 PDF 未提取到文字」；
* 这正是 2026-09-21 条目 `6.6` 安装态验收实测到的现象（A8/A9 结构性无法达成）。

**修法取"模板"而不是"代码默认"**：`tools/packaging/zhilan-desktop.env.template` 里显式
`MINERU_ENABLE=true`。理由有两条：① 模板是**用户可见可改**的（与 `MINERU_VLM_ENABLE` 同处一段），
② **不动代码默认** ⇒ 开发态与既有测试语义逐字不变（`.env` 未设时仍是 `False`）。
⚠️ 已知边界：`.env` 在 `.iss` 里是 `onlyifdoesntexist`（升级不覆盖）⇒ **升级安装的旧实例不会自动拿到
这一行**，需手工补（本轮如实登记，不假装升级也生效）。

## ② 内嵌向量库落点必须锚到**数据根**

`QDRANT_PATH=data/qdrant` 原先原样交给 Qdrant 客户端 ⇒ 按**进程 CWD** 解析；冻结态 onedir 的
CWD 是 `<exe目录>\\_internal`（**资源根、约定只读**）⇒ 向量库落到 `_internal\\data\\qdrant`：
本机因 `_internal` 恰好可写而"看起来正常"，**装到受保护目录就会写失败**。
修法 = 新增 `Settings.qdrant_path`（相对路径按 `ROOT_DIR` 解析，与 `upload_path` / `sqlite_file` 同一锚点），
`app/vector/__init__.py` 两处取用它。

**变异自证（本轮实测）**：删掉模板里的 `MINERU_ENABLE=true` ⇒ ① 变红；
把 `qdrant_path` 改回 `QDRANT_PATH or None` ⇒ ② 变红；各自还原后全绿。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import config as config_mod
from app.core.config import Settings

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "tools" / "packaging" / "zhilan-desktop.env.template"
ISS = REPO / "tools" / "packaging" / "zhilan.iss"


# ---------------------------------------------------------------------------
# ① 桌面默认配置
# ---------------------------------------------------------------------------


def test_desktop_template_enables_mineru() -> None:
    """模板必须显式写 `MINERU_ENABLE=true`（否则导入模型包等于白导）。"""
    text = TEMPLATE.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert "MINERU_ENABLE=true" in lines, (
        "桌面模板缺 `MINERU_ENABLE=true` ⇒ 安装态扫描件拿不到 OCR（条目 6.6 实测）"
    )


def test_desktop_template_is_actually_used_by_installer() -> None:
    """模板必须真的被安装器搬运（防"改了模板但安装器不用它"）。"""
    iss = ISS.read_text(encoding="utf-8", errors="replace")
    assert "zhilan-desktop.env.template" in iss, "安装器脚本没有引用桌面模板"
    assert ".env" in iss, "安装器脚本没有落 .env"


def _iss_block(iss: str, header: str) -> str:
    """截取 .iss 里某个 `function`/`procedure` 的正文（到下一条顶层声明为止）。"""
    rest = iss[iss.index(header) + len(header):]
    ends = [i for i in (rest.find("\nfunction "), rest.find("\nprocedure ")) if i != -1]
    return rest[: min(ends)] if ends else rest


def test_iss_guard_skips_model_dir_line_for_non_ascii_install_dir() -> None:
    """非 ASCII 安装路径守卫必须在 `AppendModelDirLine` 内、且在追加语句之前（回归修复）。

    ⚠️ **文本级 pin · 不承担行为判别力**：行为判据 = 真安装端到端（由 Lead 与 fix-wire 跑）。
    本腿只防「守卫被挪出该过程 / 删掉 / 挪到写盘之后」这类结构性回归。
    """
    block = _iss_block(
        ISS.read_text(encoding="utf-8", errors="replace"), "procedure AppendModelDirLine();"
    )
    guard = block.index("IsAsciiOnly")
    line_build = block.index("Line := 'MINERU_MODEL_DIR='")
    # 从构造追加行处**往后**找写盘调用（守卫注释里也提到了该 API，不能从头搜）
    append = block.index("SaveStringToFile", line_build)
    assert guard < line_build < append, "守卫必须在构造追加行与写盘之前"
    between = block[guard:line_build]
    assert "Log(" in between and "Exit" in between, (
        "非 ASCII 分支必须 Log 一行说明并 Exit（不写 .env）"
    )
    assert "TrimTrailingSlashes(ExpandConstant('{app}'))" in block[:line_build], (
        "守卫必须对 `{app}` 全路径判定（而非只判末级名）"
    )
    assert "IsAsciiOnly(FullDir)" in between, "守卫必须复用既有 IsAsciiOnly 判全路径"


def test_code_default_stays_false() -> None:
    """**代码默认必须仍是 False**：本修复只动桌面模板，不动开发态语义。"""
    assert Settings.model_fields["MINERU_ENABLE"].default is False, (
        "MINERU_ENABLE 的代码默认被改了 ⇒ 开发态与既有测试语义会变，本修复不取这条路"
    )


# ---------------------------------------------------------------------------
# ② 向量库锚点
# ---------------------------------------------------------------------------


def test_relative_qdrant_path_anchors_to_data_root(monkeypatch, tmp_path) -> None:
    """相对 `QDRANT_PATH` 必须按 `ROOT_DIR`（数据根）解析，**不是**按 CWD。"""
    monkeypatch.setattr(config_mod, "ROOT_DIR", tmp_path, raising=True)
    s = Settings(_env_file=None, QDRANT_PATH="data/qdrant", UPLOAD_DIR="data/uploads")
    got = s.qdrant_path
    assert got is not None
    assert Path(got) == tmp_path / "data" / "qdrant", got
    # 与另外两个锚点同源（同一 ROOT_DIR、同一种相对/绝对口径）
    assert Path(got).parent == s.upload_path.parent


def test_absolute_qdrant_path_is_untouched(monkeypatch, tmp_path) -> None:
    """绝对路径原样返回（单测/多实例隔离依赖这条）。"""
    monkeypatch.setattr(config_mod, "ROOT_DIR", tmp_path / "somewhere-else", raising=True)
    target = tmp_path / "abs" / "qdrant"
    assert Settings(QDRANT_PATH=str(target)).qdrant_path == str(target)


def test_empty_qdrant_path_is_none() -> None:
    """空值 ⇒ `None`（落到 `QDRANT_URL`），**开发态就是这一支** ⇒ 改动对开发态零影响。"""
    assert Settings(QDRANT_PATH="").qdrant_path is None
    assert Settings(QDRANT_PATH="   ").qdrant_path is None


def test_vector_store_uses_the_anchored_path(monkeypatch, tmp_path) -> None:
    """接线层：`get_vector_store()` 真的取 `settings.qdrant_path`（防"加了属性但没人用"）。"""
    from app import vector as vector_mod

    seen: dict = {}

    class _Recorder:
        def __init__(self, *, path=None, url=None, collection=None):
            seen.update(path=path, url=url, collection=collection)

    monkeypatch.setattr(vector_mod, "QdrantVectorStore", _Recorder)
    monkeypatch.setattr(config_mod, "ROOT_DIR", tmp_path, raising=True)
    monkeypatch.setattr(vector_mod.settings, "QDRANT_PATH", "data/qdrant", raising=False)
    # ⚠️ **调用前先清缓存**：`_default_store` 是 `lru_cache`，同进程里只要之前有人用过向量库，
    # 缓存的就**不是**我们装的 `_Recorder` ⇒ 断言会在"空 seen"上失败（全量套件里实测踩到）。
    vector_mod._default_store.cache_clear()
    try:
        vector_mod.get_vector_store()
        assert seen["path"] == str(tmp_path / "data" / "qdrant"), seen
    finally:
        # 调用后也要清：否则这个假 store 会**跨用例存活**在 `lru_cache` 里，
        # 后续 `close_vector_store()` 会打到它没有的 `.close()`（同样实测踩到）。
        vector_mod._default_store.cache_clear()


@pytest.mark.parametrize("attr", ["upload_path", "sqlite_file"])
def test_sibling_anchors_unchanged(monkeypatch, tmp_path, attr) -> None:
    """同一个 `ROOT_DIR` 锚点上的另外两个属性行为不变（防顺手改坏）。"""
    monkeypatch.setattr(config_mod, "ROOT_DIR", tmp_path, raising=True)
    got = getattr(Settings(UPLOAD_DIR="data/uploads", SQLITE_PATH=""), attr)
    assert str(got).startswith(str(tmp_path)), got
