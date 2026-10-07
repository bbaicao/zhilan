"""__回归修复__ + __回归修复__ 判据载体：导出面两处健壮性（同文件，条目内串行 167 → 177）。

靶点文件 = __backend/app/export/markdown.py__（510 行 / CRLF）。

**167（P3 · 保留设备名守卫被 __rstrip__ 顺序绕过）**
- 改前：__:76__ 判 __RESERVED__ 在 __:78__ __rstrip(". ")__ **之前** ⇒ __NUL.__ / __CON.__ / __nul..__
  判成「不是保留名」，却因 rstrip 落成裸 __NUL__ / __CON__：Windows 上 __NUL.md__ 是 **NUL 设备**
  （写盘「成功」但磁盘上没有文件）、__CON__ 形态直接炸成 __errors[]__ ⇒ **计数成功但文件不可用**。
- 改后：**先 __rstrip(". ")__ 再判保留名**，且按**去掉扩展名后的 stem** 比对
  （Windows 把 __CON.txt__ 同样当设备名）⇒ __CON.txt__ 变成 __._CON.txt__。
  ⛔ 不得退化成前缀匹配：__CONS__ / __nul2__ / __COM10__ / __CONX.txt__ **一个字都不动**。

**177（P3 · 可写探针覆盖用户同名文件 / 目录形态误判）**
- 改前：探针名固定 __.zhilan-export-probe__ ⇒ ① 用户目录里的同名文件被 __write_text__ **覆盖**
  又被 __unlink__ **删除**（静默数据破坏）；② 同名项若是**目录** ⇒ __IsADirectoryError__（属
  __OSError__）⇒ 误报「不可写」把合法导出拒掉。
- 改后：探针名加随机后缀 + __os.open(..., O_CREAT | O_EXCL | O_WRONLY)__（__O_EXCL__ 天然防覆盖）、
  同名项是目录时**显式放行**、清理放 __finally__。⛔ 探针**不去掉**（真写一个文件才是路径真实性判据）、
  四条 __ExportPathError__ 文案**逐字保留**（含 __path__ 可读性）。

**判别力边界（如实登记，⛔ 不得写「所有平台都能打红」）**：167 的端到端腿靠 **Windows 设备名语义**
变红（Linux 上 __NUL.md__ 只是普通文件）⇒ 判别力**只在 Windows 成立**；__sanitize_name__ 单元腿是
纯函数、跨平台。177 的腿全部跨平台（预置文件被删 / 目录被误判 / 探针残留与平台无关）。

**负对照配方（供非作者独立执行）**：见本文件末尾「负对照配方」节，逐字复现 ⇒ 对应腿必须变红。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.export import markdown as markdown_module
from app.export.markdown import (
    _PROBE_NAME,
    ExportPathError,
    export_markdown,
    resolve_root,
    sanitize_name,
)
from app.models import Note

# ─────────────────────────────────────────────────────────────────────────────
# 167 · 单元腿：保留设备名判定（纯函数，跨平台）
# ─────────────────────────────────────────────────────────────────────────────

#: 判决样本：结尾点 / 空格形态 —— 改前 rstrip 在保留名判定之后 ⇒ 全部落成裸设备名。
_167_TAIL_MARK_CASES = [
    ("NUL.", "_NUL"),
    ("NUL..", "_NUL"),
    ("NUL .", "_NUL"),
    ("nul.", "_nul"),
    ("CON.", "_CON"),
    ("prn..", "_prn"),
    ("AUX.", "_AUX"),
    ("COM1.", "_COM1"),
    ("lpt9..", "_lpt9"),
]


@pytest.mark.parametrize("raw,expected", _167_TAIL_MARK_CASES)
def test_167_trailing_dot_reserved_names_get_prefix(raw, expected):
    """167 判决腿：先 rstrip 再判保留名 ⇒ 结尾点 / 空格形态必须加前缀。

    回退击杀口径：把 __rstrip__ 挪回保留名判定之后（改前形态）⇒ 本腿**全部变红**
    （改前实测 __sanitize_name("NUL.") == "NUL"__、__("CON.") == "CON"__）。
    """
    assert sanitize_name(raw) == expected


def test_167_extension_forms_are_matched_by_stem():
    """带扩展名的保留名同样不可用（Windows 把 __CON.txt__ 当设备）⇒ 按去扩展名后的 stem 比对。

    改后语义：**给整个名字加前缀**（不重组扩展名）⇒ __CON.txt__ 变成 __._CON.txt__。
    """
    assert sanitize_name("CON.txt") == "_CON.txt"
    assert sanitize_name("con.txt") == "_con.txt"
    assert sanitize_name("NUL.md") == "_NUL.md"
    assert sanitize_name("COM1.log") == "_COM1.log"
    # 结尾点 + 扩展名 + 大小写混合一起过一遍（LPT1 是保留名；LuL 不是 ⇒ 兼作负控）
    assert sanitize_name("lPt1.TXT.") == "_lPt1.TXT"
    assert sanitize_name("LuL.TXT.") == "LuL.TXT"


def test_167_negative_controls_are_not_prefix_matched():
    """负控腿（回退击杀时本腿必须仍绿）：⛔ 不得退化成前缀匹配。

    __CONS__ / __nul2__ / __COM10__ / __CONX.txt__ 这些**不是**保留名，一个字都不许动；
    否则正常笔记会被莫名改名（误伤面比原缺陷还大）。
    """
    for raw in (
        "CONS",
        "CONS.txt",
        "CONX.txt",
        "nul2",
        "NUL2.md",
        "COM10",
        "COM10.txt",
        "LPT0",
        "AUX2",
        "PRINTER",
        "MYCON.txt",
        "COM",  # _RESERVED 只有 COM1-9，裸 COM 不是保留名
    ):
        assert sanitize_name(raw) == raw, raw


def test_167_empty_and_dot_only_names_still_empty():
    """顺序对调**不得**改变既有空名语义：空 / 纯空白 / 纯点 ⇒ 仍返回空串。"""
    assert sanitize_name("") == ""
    assert sanitize_name("   ") == ""
    assert sanitize_name("...") == ""
    assert sanitize_name(". .") == ""
    assert sanitize_name(None) == ""  # 产品入口兼容 None


def test_167_illegal_chars_still_replaced_first():
    """既有口径不动：非法字符仍**先**换成下划线（__NUL:__ ⇒ __NUL___，不是保留名）。"""
    assert sanitize_name('a<b>c:d"e/f' + chr(92) + "g|h?i*j") == "a_b_c_d_e_f_g_h_i_j"
    assert sanitize_name("NUL:") == "NUL_"
    assert sanitize_name("NUL<") == "NUL_"


# ─────────────────────────────────────────────────────────────────────────────
# 167 · 端到端：真 DB + 真写盘（Windows 设备名语义下才有判别力）
# ─────────────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def _clean_notes(db):
    """__notes__ 表不在 conftest 的清理序里（照 __test_bug136_yaml_cr.py__ 的写法）。"""
    await db.execute(text("DELETE FROM notes"))
    await db.commit()
    yield


@pytest.mark.asyncio
async def test_167_reserved_note_title_lands_a_real_file(db, tmp_path, _clean_notes):
    """端到端：标题 __NUL.__ ⇒ 导出必须产出**磁盘上真实存在**的 __.md__ 文件（改前是设备名黑洞）。

    改前实测形态（Windows）：__notes_exported == 1__、__errors == []__，但文件根本没落盘
    （__NUL.md__ 被解释成 NUL 设备）⇒ 「计数成功但文件不可用」。本腿断「文件真的在」。
    """
    db.add(Note(title="NUL.", content="正文" + chr(10), category="", tags=None, kb_id=1))
    await db.commit()

    result = await export_markdown(db, tmp_path, include_documents=False)

    assert result.errors == [], result.errors
    assert result.notes_exported == 1, result
    files = [path for path in Path(result.output_dir).rglob("*.md") if path.is_file()]
    assert len(files) == 1, files
    assert files[0].name == "_NUL.md", files[0]
    assert files[0].read_text(encoding="utf-8").startswith("---" + chr(10) + "title: NUL." + chr(10))


@pytest.mark.asyncio
async def test_167_reserved_category_creates_a_real_directory(db, tmp_path, _clean_notes):
    """__category__ 走同一个 __sanitize_name__ ⇒ 保留名分类必须落成真目录 __._NUL__，不是设备名。"""
    db.add(Note(title="普通标题", content="正文" + chr(10), category="NUL.", tags=None, kb_id=1))
    await db.commit()

    result = await export_markdown(db, tmp_path, include_documents=False)

    assert result.errors == [], result.errors
    assert result.notes_exported == 1, result
    dirs = {path.name for path in Path(result.output_dir).rglob("*") if path.is_dir()}
    assert "_NUL" in dirs, dirs
    assert "NUL" not in dirs, dirs


# ─────────────────────────────────────────────────────────────────────────────
# 177 · 探针不得碰用户同名文件 / 目录形态 / 文案逐字 / 不残留
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_177_preexisting_same_named_file_is_byte_identical(db, tmp_path, _clean_notes):
    """① + ④ 判决腿：预置 __.zhilan-export-probe__（内容逐字）⇒ 导出后**仍在且逐字节未变**，探针不残留。

    改前实测：__write_text("ok")__ 覆盖它、__unlink()__ 再删掉它 ⇒ 用户文件被静默销毁。
    回退击杀口径：把探针段换回固定名 + __write_text__ / __unlink__ ⇒ 本腿两处红（文件消失）。
    """
    sentinel = tmp_path / _PROBE_NAME
    sentinel.write_text("keep" + chr(10), encoding="utf-8")
    before = sentinel.read_bytes()

    db.add(Note(title="普通标题", content="正文" + chr(10), category="", tags=None, kb_id=1))
    await db.commit()
    result = await export_markdown(db, tmp_path, include_documents=False)

    assert result.errors == [], result.errors
    assert result.notes_exported == 1, result
    assert sentinel.exists(), "用户的同名文件被探针删掉了"
    assert sentinel.read_bytes() == before, "用户的同名文件被覆盖了"
    leftovers = [
        item.name
        for item in tmp_path.iterdir()
        if item.name.startswith(_PROBE_NAME) and item != sentinel
    ]
    assert leftovers == [], leftovers


@pytest.mark.asyncio
async def test_177_preexisting_same_named_directory_is_not_misreported(db, tmp_path, _clean_notes):
    """② 判决腿：预置同名**目录** ⇒ __resolve_root__ 不得抛「不可写」（改前是 IsADirectoryError）。"""
    user_dir = tmp_path / _PROBE_NAME
    user_dir.mkdir()
    (user_dir / "note.txt").write_text("keep", encoding="utf-8")

    assert resolve_root(tmp_path) == tmp_path  # 改前在这里抛 ExportPathError

    db.add(Note(title="普通标题", content="正文" + chr(10), category="", tags=None, kb_id=1))
    await db.commit()
    result = await export_markdown(db, tmp_path, include_documents=False)

    assert result.errors == [], result.errors
    assert result.notes_exported == 1, result
    assert user_dir.is_dir(), "用户的同名目录被动了"
    assert (user_dir / "note.txt").read_text(encoding="utf-8") == "keep"


def test_177_probe_path_dir_form_is_explicitly_exempt(tmp_path, monkeypatch):
    """② 分支腿：**显式区分「同名项是目录」** —— 让探针名恰好撞上一个目录，也必须放行。

    做法：把 __uuid4__ 钉成常量、在 __f"{_PROBE_NAME}-{token}"__ 预置目录；同时预置**旧的固定名**
    目录以保留判决力（改前走固定名 ⇒ 在这里就抛「不可写」）。
    """
    token = "0123456789abcdef0123456789abcdef"
    monkeypatch.setattr(markdown_module, "uuid4", lambda: SimpleNamespace(hex=token))
    legacy_dir = tmp_path / _PROBE_NAME
    legacy_dir.mkdir()
    probe_dir = tmp_path / (_PROBE_NAME + "-" + token)
    probe_dir.mkdir()

    assert resolve_root(tmp_path) == tmp_path
    assert probe_dir.is_dir(), "探针同名目录被删了"
    entries = sorted(item.name for item in tmp_path.iterdir())
    assert entries == sorted([_PROBE_NAME, _PROBE_NAME + "-" + token]), entries


def test_177_unwritable_dir_still_raises_the_exact_message(tmp_path, monkeypatch):
    """③ 判决腿：真不可写 ⇒ 仍抛 __ExportPathError__，文案**逐字**保留（含 path 可读性）。

    不可写用 **OSError 注入**（__os.open__ 抛 __PermissionError__）—— 本机 Windows ACL 只读目录
    在沙箱下未实跑（见执行记录「挂账」）。判据只认「OSError ⇒ 逐字文案」，与注入方式无关。
    """

    def denied(*args, **kwargs):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(markdown_module.os, "open", denied)
    with pytest.raises(ExportPathError) as excinfo:
        resolve_root(tmp_path)
    assert str(excinfo.value) == f"导出目录 {tmp_path} 不可写，请检查权限或换一个位置。"


def test_177_probe_is_cleaned_up_even_when_write_fails(tmp_path, monkeypatch):
    """④ 分支腿：探针**真被造出来之后**写入失败（磁盘满 / 配额）⇒ 清理仍要发生、不留残骸。

    注入点选 __os.write__：__os.open__ 成功（探针已落盘）、随后写入失败 ⇒ 异常路径也必须把探针
    删掉（清理放 __finally__）。改前代码走 __Path.write_text__ 且不用 __O_EXCL__，本腿的注入点
    不生效 ⇒ __pytest.raises__ 直接变红。
    """
    seen = {}

    def write_then_fail(fd, payload):
        seen["probe_exists"] = True
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(markdown_module.os, "write", write_then_fail)
    with pytest.raises(ExportPathError) as excinfo:
        resolve_root(tmp_path)
    assert seen.get("probe_exists") is True
    assert str(excinfo.value) == f"导出目录 {tmp_path} 不可写，请检查权限或换一个位置。"
    assert list(tmp_path.glob(_PROBE_NAME + "*")) == [], "探针残留在用户目录里"


def test_177_probe_does_not_residue_on_success(tmp_path):
    """④ 判决腿：正常成功路径上探针**不残留**（目录里的条目集合与调用前逐字一致）。"""
    before = sorted(item.name for item in tmp_path.iterdir())
    assert resolve_root(tmp_path) == tmp_path
    assert sorted(item.name for item in tmp_path.iterdir()) == before


def test_177_four_error_messages_are_verbatim(tmp_path):
    """⛔ 三条前置校验文案 + 不可写文案**逐字保留**（照改前 __:224 / :229 / :231 / :237__ 抄写）。"""
    with pytest.raises(ExportPathError) as empty:
        resolve_root("   ")
    assert str(empty.value) == "导出目录不能为空，请先填写或选择一个文件夹。"

    missing = tmp_path / "不存在的目录"
    with pytest.raises(ExportPathError) as absent:
        resolve_root(missing)
    assert str(absent.value) == f"导出目录不存在：{missing}，请先选择一个已存在的文件夹。"

    a_file = tmp_path / "a.txt"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(ExportPathError) as as_file:
        resolve_root(a_file)
    assert str(as_file.value) == f"导出位置 {a_file} 是一个文件，不能当作导出目录，请选择文件夹。"


# ─────────────────────────────────────────────────────────────────────────────
# 负对照配方（非作者独立执行 · 仓外字节副本 + sys.meta_path 源码重定向）
# ─────────────────────────────────────────────────────────────────────────────
#
# 167：把 __sanitize_name__ 换回「先判 __RESERVED__ 再 __rstrip(". ")__、且不按 stem」的改前形态
#      ⇒ __test_167___ 开头的腿全部变红（单元腿 + 两条端到端腿）。
# 177：把 __resolve_root__ 的探针段换回
#      probe = path / _PROBE_NAME; probe.write_text("ok", encoding="utf-8"); probe.unlink()
#      ⇒ test_177_preexisting_same_named_file_is_byte_identical（文件被删）、
#        test_177_preexisting_same_named_directory_is_not_misreported（IsADirectoryError 误报）、
#        test_177_probe_is_cleaned_up_even_when_write_fails（os.open 未被调用）变红。
# 上述负对照应使对应行为断言失败，以确认测试能识别错误实现。
