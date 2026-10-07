# -*- coding: utf-8 -*-
"""阶段 6 · 条目 6.4：离线模型包完整性清单 / 校验脚本的回归测试。

**为什么必须有这个文件**：`tools/packaging/models_manifest.py` 是**分发给用户的校验依据**——
用户拿到模型包后要靠它回答「我手上这份和发布时是否一致」。这类工具的危险失败模式不是「报错」，
而是**报绿**：清单生成时少写一件、verify 忘了算哈希、路径匹配宽了一格，都会让它安安静静地
说「PASS」，而用户拿着一个残缺模型包却以为没问题。故本文件的两条主线是：

1. **变异自证（A11）**：改一字节 / 删一件 / 多一件，verify **各自必须变红**且给中文原因；
   还原后必须变绿。只测「正常通过」的用例等于没测——它证明不了判据真的在判。
2. **零依赖零出网（A12）**：脚本只许用 stdlib、不许出现任何联网 / 子进程 import（用 AST 静态钉住）。

外加两条「清单内容」的硬断言：恰好 13 件、总字节 858,204,914、清单里不含禁进包件（A9/A10）。

纪律：**不连库、不联网、不起服务、不 import 任何产品模块**——全程只在临时目录里造小文件，
唯一读真实模型根的那条用例在没有该目录的机器上显式 skip（skip 理由会打出来）。
"""

from __future__ import annotations

import ast
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]
SCRIPT = REPO / "tools" / "packaging" / "models_manifest.py"
MANIFEST = REPO / "tools" / "packaging" / "models-manifest.txt"

#: 官方 ONNX 件的根（`<MINERU_HOME>/models/MinerU-4_models_onnx`）。
#: 默认值是**构建机**上的位置；其它机器用环境变量指过去，或让「与本地实测比对」那条 skip。
MODEL_ROOT = Path(
    os.environ.get("ZL_MINERU_ONNX_ROOT", r"E:\zhilan-models\models\MinerU-4_models_onnx")
)

#: A9 钉死的两个数（= 就绪核查 §三 实测）。
EXPECTED_COUNT = 13
EXPECTED_TOTAL_BYTES = 858_204_914

EXIT_OK, EXIT_VERIFY_FAILED, EXIT_INPUT_ERROR = 0, 1, 2


# ======================================================================================
# 工具函数
# ======================================================================================
def _run(*args, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """跑校验脚本（子进程，退出码才是判据）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *[str(a) for a in args]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd) if cwd else None,
        env=env,
        check=False,
    )


def _out(result: subprocess.CompletedProcess) -> str:
    """stdout + stderr 合起来（判据可能在任一路）。"""
    return (result.stdout or "") + (result.stderr or "")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _make_tree(root: Path, files: dict[str, bytes]) -> Path:
    """在 `root` 下按 `files`（相对路径 → 内容）造一棵小模型树，返回 root。"""
    for rel, payload in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return root


def _manifest_lines(path: Path) -> list[str]:
    """取清单的**数据行**（跳过注释与空行）。"""
    return [
        line.rstrip("\r")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


@pytest.fixture
def toy(tmp_path: Path):
    """一个两件小树 + 为它生成的清单（`(root, manifest, files)`）。"""
    files = {
        "A/one.onnx": b"first-model-bytes",
        "B/two.yml": b"second: config\n",
    }
    root = _make_tree(tmp_path / "models" / "MinerU-4_models_onnx", files)
    manifest = tmp_path / "toy-manifest.txt"
    gen = _run("gen", "--root", root, "--out", manifest, "--generated-at", "2026-01-01T00:00:00+08:00")
    assert gen.returncode == EXIT_OK, _out(gen)
    return root, manifest, files


# ======================================================================================
# 0 · 基本形态：gen → verify 绿
# ======================================================================================
def test_gen_then_verify_is_green(toy):
    root, manifest, _files = toy
    result = _run("verify", "--root", root, "--manifest", manifest)
    assert result.returncode == EXIT_OK, _out(result)
    assert "PASS" in _out(result)


def test_gen_is_deterministic_and_sorted(toy, tmp_path):
    """同一棵树 + 同一 `--generated-at` ⇒ 输出逐字节相同；且按相对路径升序。"""
    root, manifest, _files = toy
    again = tmp_path / "again.txt"
    second = _run("gen", "--root", root, "--out", again, "--generated-at", "2026-01-01T00:00:00+08:00")
    assert second.returncode == EXIT_OK, _out(second)
    assert again.read_bytes() == manifest.read_bytes()

    rels = [line.split("  ")[1] for line in _manifest_lines(manifest)]
    assert rels == sorted(rels), rels
    assert rels == ["A/one.onnx", "B/two.yml"], rels


def test_gen_writes_posix_relative_paths_only(toy):
    """清单里的相对路径必须是正斜杠、相对形态——不得出现盘符 / 盘符分隔符。"""
    _root, manifest, _files = toy
    for line in _manifest_lines(manifest):
        rel = line.split("  ")[1]
        assert "\\" not in rel, rel
        assert ":" not in rel, rel
        assert not rel.startswith("/"), rel


def test_gen_header_declares_scope_and_carries_no_machine_path(toy):
    """文件头必须写明来源仓库 / 生成命令 / 「只覆盖官方 ONNX 件」声明，且**不含本机绝对路径**。"""
    _root, manifest, _files = toy
    text = manifest.read_text(encoding="utf-8")
    assert "OpenDataLab/MinerU-4_models_onnx" in text
    assert "models_manifest.py gen" in text
    assert "只覆盖官方 ONNX 件" in text
    assert "生成时间" in text
    # 本机路径零容忍：清单是要入库、要发给用户的文件，不能泄漏构建机目录结构。
    for leaked in ("E:\\", "E:/", "C:\\", "C:/", str(Path.home())):
        assert leaked not in text, f"清单里出现本机路径字样 {leaked!r}"


# ======================================================================================
# 1 · ⭐ 变异自证（A11）：改一字节 / 删一件 / 多一件 ⇒ 各自变红；还原 ⇒ 绿
# ======================================================================================
def test_a11_one_byte_changed_turns_red_then_green(toy):
    root, manifest, _files = toy
    target = root / "A" / "one.onnx"
    original = target.read_bytes()

    target.write_bytes(b"first-model-bytez")  # 末字节 t→z，**字节数不变**
    try:
        result = _run("verify", "--root", root, "--manifest", manifest)
        assert result.returncode == EXIT_VERIFY_FAILED, _out(result)
        assert "哈希不符" in _out(result), _out(result)
        assert "FAIL" in _out(result), _out(result)
    finally:
        target.write_bytes(original)

    restored = _run("verify", "--root", root, "--manifest", manifest)
    assert restored.returncode == EXIT_OK, _out(restored)


def test_a11_missing_file_turns_red_then_green(toy):
    root, manifest, _files = toy
    victim = root / "B" / "two.yml"
    backup = victim.read_bytes()

    victim.unlink()
    try:
        result = _run("verify", "--root", root, "--manifest", manifest)
        assert result.returncode == EXIT_VERIFY_FAILED, _out(result)
        assert "缺件" in _out(result), _out(result)
    finally:
        victim.write_bytes(backup)

    restored = _run("verify", "--root", root, "--manifest", manifest)
    assert restored.returncode == EXIT_OK, _out(restored)


def test_a11_extra_file_turns_red_then_green(toy):
    root, manifest, _files = toy
    intruder = root / "A" / "surprise.onnx"
    intruder.write_bytes(b"not-in-manifest")
    try:
        result = _run("verify", "--root", root, "--manifest", manifest)
        assert result.returncode == EXIT_VERIFY_FAILED, _out(result)
        assert "多件" in _out(result), _out(result)
    finally:
        intruder.unlink()

    restored = _run("verify", "--root", root, "--manifest", manifest)
    assert restored.returncode == EXIT_OK, _out(restored)


def test_a11_truncated_file_reports_size_mismatch(toy):
    """截断 = 字节数不符（与「内容变了但长度没变」的哈希不符是两条独立判据）。"""
    root, manifest, _files = toy
    target = root / "A" / "one.onnx"
    original = target.read_bytes()
    target.write_bytes(original[:5])
    try:
        result = _run("verify", "--root", root, "--manifest", manifest)
        assert result.returncode == EXIT_VERIFY_FAILED, _out(result)
        assert "字节数不符" in _out(result), _out(result)
    finally:
        target.write_bytes(original)


def test_a11_tampered_manifest_hash_is_red(tmp_path):
    """清单本身被改（hash 换成别人的）也必须红——不能只校目录、不校清单。"""
    files = {"A/one.onnx": b"payload"}
    root = _make_tree(tmp_path / "root", files)
    manifest = tmp_path / "m.txt"
    assert _run("gen", "--root", root, "--out", manifest).returncode == EXIT_OK

    lines = manifest.read_text(encoding="utf-8").splitlines()
    swapped = 0
    for index, line in enumerate(lines):
        if line.strip() and not line.lstrip().startswith("#"):
            lines[index] = "0" * 64 + line[64:]
            swapped += 1
    assert swapped == 1
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = _run("verify", "--root", root, "--manifest", manifest)
    assert result.returncode == EXIT_VERIFY_FAILED, _out(result)
    assert "哈希不符" in _out(result), _out(result)


# ======================================================================================
# 2 · 输入类错误 ⇒ 退 2（不许把坏输入当通过）
# ======================================================================================
def test_missing_root_is_input_error(toy, tmp_path):
    _root, manifest, _files = toy
    result = _run("verify", "--root", tmp_path / "nope", "--manifest", manifest)
    assert result.returncode == EXIT_INPUT_ERROR, _out(result)


def test_missing_manifest_is_input_error(toy, tmp_path):
    root, _manifest, _files = toy
    result = _run("verify", "--root", root, "--manifest", tmp_path / "nope.txt")
    assert result.returncode == EXIT_INPUT_ERROR, _out(result)


def test_empty_manifest_is_input_error(toy, tmp_path):
    """空清单不能当通过依据——否则「清单丢了」会伪装成「全部一致」。"""
    root, _manifest, _files = toy
    empty = tmp_path / "empty.txt"
    empty.write_text("# 只有注释\n", encoding="utf-8")
    result = _run("verify", "--root", root, "--manifest", empty)
    assert result.returncode == EXIT_INPUT_ERROR, _out(result)


def test_broken_manifest_line_is_input_error(tmp_path):
    files = {"A/one.onnx": b"payload"}
    root = _make_tree(tmp_path / "root", files)
    bad = tmp_path / "bad.txt"
    bad.write_text("这不是一行合法清单\n", encoding="utf-8")
    result = _run("verify", "--root", root, "--manifest", bad)
    assert result.returncode == EXIT_INPUT_ERROR, _out(result)
    assert "不合法" in _out(result) or "清单" in _out(result), _out(result)


def test_duplicate_manifest_path_is_input_error(tmp_path):
    files = {"A/one.onnx": b"payload"}
    root = _make_tree(tmp_path / "root", files)
    manifest = tmp_path / "m.txt"
    assert _run("gen", "--root", root, "--out", manifest).returncode == EXIT_OK
    line = _manifest_lines(manifest)[0]
    manifest.write_text(line + "\n" + line + "\n", encoding="utf-8")
    result = _run("verify", "--root", root, "--manifest", manifest)
    assert result.returncode == EXIT_INPUT_ERROR, _out(result)


def test_gen_refuses_root_containing_gguf(tmp_path):
    """模型根里出现 .gguf ⇒ **拒绝生成清单**（退 2），而不是悄悄跳过。

    为什么必须拒绝：悄悄跳过会让清单看起来干净，而根目录其实脏——用户拿到的「通过」
    就变成一个不成立的结论。这条是 A10 的负向钉子。
    """
    root = _make_tree(
        tmp_path / "root",
        {"A/one.onnx": b"ok", "vlm/MinerU2.5-Pro.gguf": b"third-party-weights"},
    )
    out = tmp_path / "m.txt"
    result = _run("gen", "--root", root, "--out", out)
    assert result.returncode == EXIT_INPUT_ERROR, _out(result)
    assert not out.exists(), "拒绝生成时不该留下清单文件"
    assert ".gguf" in _out(result), _out(result)


def test_gen_refuses_root_containing_mineru_llama_cpp(tmp_path):
    root = _make_tree(tmp_path / "root", {"mineru_llama_cpp/llama.dll": b"no-license-here"})
    out = tmp_path / "m.txt"
    result = _run("gen", "--root", root, "--out", out)
    assert result.returncode == EXIT_INPUT_ERROR, _out(result)
    assert not out.exists()


def test_gen_refuses_root_containing_mineru_complete_marker(tmp_path):
    """`.mineru_complete` / `.gitattributes` 都是**上游下载标记**，不是模型件，不该进清单。"""
    root = _make_tree(tmp_path / "root", {"A/one.onnx": b"ok", ".mineru_complete": b""})
    out = tmp_path / "m.txt"
    result = _run("gen", "--root", root, "--out", out)
    assert result.returncode == EXIT_INPUT_ERROR, _out(result)
    assert not out.exists()


def test_gen_on_empty_root_is_input_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    out = tmp_path / "m.txt"
    result = _run("gen", "--root", empty, "--out", out)
    assert result.returncode == EXIT_INPUT_ERROR, _out(result)


# ======================================================================================
# 3 · ⭐ 清单内容硬断言（A9 / A10）——**离线可跑**，不依赖本机模型目录
# ======================================================================================
def test_a9_manifest_has_exactly_13_entries_and_pinned_total():
    assert MANIFEST.is_file(), f"清单不存在：{MANIFEST}"
    lines = _manifest_lines(MANIFEST)
    assert len(lines) == EXPECTED_COUNT, f"清单件数 {len(lines)} ≠ {EXPECTED_COUNT}"

    total = 0
    for line in lines:
        parts = line.split("  ")
        assert len(parts) == 3, f"清单行不是三段式：{line!r}"
        sha, rel, size = parts
        assert len(sha) == 64 and all(c in "0123456789abcdef" for c in sha), sha
        assert rel and not rel.startswith("/") and "\\" not in rel and ":" not in rel, rel
        total += int(size)
    assert total == EXPECTED_TOTAL_BYTES, f"清单总字节 {total} ≠ {EXPECTED_TOTAL_BYTES}"


def test_a10_manifest_contains_no_forbidden_artifacts():
    """A10：**条目行**里零命中 .gguf / mineru_llama_cpp / 第三方仓库 id / 下载标记。

    只查数据行——文件头的**声明段**本来就会写到「不含任何 .gguf」这类字样（那是在声明范围），
    拿整份文件做子串匹配会把声明本身判成违规。
    """
    entries = "\n".join(_manifest_lines(MANIFEST))
    for needle in (".gguf", "mineru_llama_cpp", ".mineru_complete", ".gitattributes",
                   "jinzhenj", "MinerU2.5-Pro-2605-1.2B-GGUF"):
        assert needle not in entries, f"清单条目里出现禁进包件字样：{needle}"
    # 正向：条目只应挂在官方 ONNX 仓库这一条来源下（来源 id 写在文件头）。
    assert "OpenDataLab/MinerU-4_models_onnx" in MANIFEST.read_text(encoding="utf-8")
    # 条目行不许有第三段以外的东西（防止「备注列」里夹带来源/许可等未受控文本）。
    for line in _manifest_lines(MANIFEST):
        assert len(line.split("  ")) == 3, line


def test_a9_manifest_matches_real_model_root_if_present(tmp_path):
    """把本机真实模型根**重新算一遍**，逐行比对已入库的清单（A9 的「与本地实测一致」）。

    没有该目录的机器上跳过——但 skip 理由是显式打印的，不会被误读成「已经验过了」。
    """
    if not MODEL_ROOT.is_dir():
        pytest.skip(f"本机没有官方 ONNX 模型根（{MODEL_ROOT}）；本用例只在有模型包的机器上生效")

    fresh = tmp_path / "fresh-manifest.txt"
    result = _run("gen", "--root", MODEL_ROOT, "--out", fresh,
                  "--generated-at", "2026-01-01T00:00:00+08:00")
    assert result.returncode == EXIT_OK, _out(result)

    fresh_lines = _manifest_lines(fresh)
    committed_lines = _manifest_lines(MANIFEST)
    assert fresh_lines == committed_lines, (
        "入库清单与本机实测不一致 —— 要么模型包变了，要么清单没重建：\n"
        f"  仅实测有：{sorted(set(fresh_lines) - set(committed_lines))[:3]}\n"
        f"  仅清单有：{sorted(set(committed_lines) - set(fresh_lines))[:3]}"
    )

    # 顺带把 verify 也真跑一遍（同一份真实数据）。
    verified = _run("verify", "--root", MODEL_ROOT, "--manifest", MANIFEST)
    assert verified.returncode == EXIT_OK, _out(verified)


def test_a9_first_entry_hash_matches_file_bytes(tmp_path):
    """抽样自证：清单第 1 条的 sha256 必须等于**真算出来**的那个（防止清单是手写的）。"""
    if not MODEL_ROOT.is_dir():
        pytest.skip(f"本机没有官方 ONNX 模型根（{MODEL_ROOT}）")
    first = _manifest_lines(MANIFEST)[0].split("  ")
    target = MODEL_ROOT / first[1]
    assert target.is_file(), target
    assert _sha256(target) == first[0]


# ======================================================================================
# 4 · ⭐ A12：只 stdlib、零出网
# ======================================================================================
#: 允许出现的顶层 import（stdlib 白名单）——多一个都要在评审里说清为什么。
_ALLOWED_IMPORTS = {
    "__future__",
    "argparse",
    "hashlib",
    "os",
    "re",
    "sys",
    "datetime",
}

#: 明令禁止的字样：联网 / 下载 / 起子进程 / 动态执行 —— 校验器一旦有这些入口就不叫「零出网」了。
_FORBIDDEN_NAMES = (
    "socket", "ssl", "http", "urllib", "urllib2", "urllib3", "requests", "httpx",
    "aiohttp", "ftplib", "smtplib", "telnetlib", "xmlrpc", "webbrowser",
    "subprocess", "multiprocessing", "ctypes", "pickle", "shutil", "tempfile",
    "importlib", "runpy", "eval", "exec", "__import__",
)


def test_a12_script_only_imports_whitelisted_stdlib():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                seen.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                seen.add((node.module or "").split(".")[0])
            else:
                raise AssertionError("脚本不该有相对 import（它不是包内模块）")
    assert seen <= _ALLOWED_IMPORTS, f"出现白名单外的 import：{sorted(seen - _ALLOWED_IMPORTS)}"
    # 再确认白名单本身都是 stdlib（防止把第三方名写进白名单）。
    assert seen <= set(sys.stdlib_module_names), sorted(seen - set(sys.stdlib_module_names))


def test_a12_script_has_no_network_or_exec_surface():
    """静态核对：**代码**里不得出现任何联网 / 子进程 / 动态执行入口。

    只认标识符与调用名，不认注释与 docstring ——脚本的说明文字里**会**提到 `socket`/`urllib`
    （解释「为什么不用」），拿原文做子串匹配会误报；用 AST 就不会。
    """
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))
    used: set[str] = set()
    calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Import):
            used.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            used.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.add(node.func.id)

    hit = sorted(used & set(_FORBIDDEN_NAMES))
    assert not hit, f"脚本代码里出现被禁标识符：{hit}"
    dynamic = sorted(calls & {"eval", "exec", "__import__", "compile"})
    assert not dynamic, f"脚本代码里出现动态执行调用：{dynamic}"


def test_a12_script_is_not_under_backend_scripts():
    """它属**分发侧工具**，必须留在 `tools/packaging/`——放进产品包目录就会被一起打进去。"""
    assert SCRIPT.parent.name == "packaging"
    assert SCRIPT.parent.parent.name == "tools"
    assert not str(SCRIPT).replace("\\", "/").startswith(
        str(REPO / "backend").replace("\\", "/")
    )


# ======================================================================================
# 5 · CLI 形态
# ======================================================================================
def test_cli_requires_subcommand_and_root(tmp_path):
    no_sub = _run()
    assert no_sub.returncode != EXIT_OK, _out(no_sub)
    no_root = _run("gen", "--out", tmp_path / "x.txt")
    assert no_root.returncode != EXIT_OK, _out(no_root)


def test_help_mentions_both_subcommands():
    result = _run("--help")
    text = _out(result)
    assert "gen" in text and "verify" in text, text
