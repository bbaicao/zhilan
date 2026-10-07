"""阶段 6 · 条目 6.2c：冻结安全路径闸门的回归与变异自证。

覆盖验收断言 **A1–A9 / A13 / A14 / A15 / A16**（另含 A8b）。
本文件验证静态闸门规则；不证明安装器行为或整个仓库的回归结果。

设计要点：
- 断言的是**扫描器的判定**，不是「文件里有没有那四个关键词」；
- 变异自证（A7 / A8 / A8b）与 ROOT 越界（A13）都跑在 `tmp_path` 副本上，**真仓只读**；
- 构建接线（A14 / A15）跑**真实 `build.ps1`**，不用「模拟扫描器返回 1」顶替；
- 本文件**不** import 任何产品模块；扫描器只由 ``importlib`` 从磁盘装载。
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]
SCANNER = HERE.parents[1] / "scripts" / "check_frozen_paths.py"
BUILD_PS1 = REPO / "tools" / "packaging" / "build.ps1"
REAL_FILES = ("backend/main.py", "backend/app/core/env_store.py", "client/app.py")
EXPECTED_EXCEPTIONS = (
    "backend/main.py::_roots",
    "backend/app/core/env_store.py::_resolve_root_dir",
    "client/app.py::backend_dir",
    "client/app.py::log_file",
    "client/app.py::ROOT",
)


def _load_scanner():
    spec = importlib.util.spec_from_file_location("check_frozen_paths_6_2c", SCANNER)
    assert spec is not None and spec.loader is not None, f"扫描器不存在：{SCANNER}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass 需要模块已登记，否则 exec_module 直接抛
    spec.loader.exec_module(module)
    return module


cfp = _load_scanner()


# ── 公共小工具 ───────────────────────────────────────────────────────────────────


def _write_files(root: Path, files: dict[str, str | bytes]) -> Path:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    return root


def _probe_tree(tmp_path: Path, source: str, name: str = "backend/app/probe.py") -> Path:
    """合法最小树 + 一个探针文件（三个扫描根齐备，探针默认落在非白名单文件里）。"""
    return _write_files(
        tmp_path,
        {
            "backend/app/__init__.py": "",
            "backend/main.py": "",
            "client/__init__.py": "",
            name: source,
        },
    )


def _real_tree(tmp_path: Path, *, overrides: dict[str, str] | None = None, extra: dict[str, str] | None = None) -> Path:
    """真仓三文件（`main.py` / `env_store.py` / `client/app.py`）的副本树。"""
    files: dict[str, str] = {"backend/app/__init__.py": "", "client/__init__.py": ""}
    for rel in REAL_FILES:
        files[rel] = (REPO / rel).read_text(encoding="utf-8")
    files.update(overrides or {})
    files.update(extra or {})
    return _write_files(tmp_path, files)


def _cli(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(SCANNER), "--root", str(root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(root),
    )


def _report(findings) -> str:
    return "\n".join(f.line() for f in findings) or "（无命中）"


# ── A1 扫描面恰好三个根 ──────────────────────────────────────────────────────────


def test_a1_scan_surface_is_exactly_three_roots(tmp_path: Path) -> None:
    root = _write_files(
        tmp_path,
        {
            # 三个根（各放一个文件；app 与 client 递归）
            "backend/app/__init__.py": "",
            "backend/app/sub/deep.py": "",
            "backend/main.py": "",
            "client/__init__.py": "",
            "client/nested/win.py": "",
            # 各做一个反例：这些都不在扫描面内，必须**不被发现、也不被命中**
            "backend/tests/test_x.py": "from pathlib import Path\nX = Path(__file__).parents[1]\n",
            "backend/scripts/tool.py": "from pathlib import Path\nX = Path(__file__).parents[1]\n",
            "backend/alembic/versions/v.py": "from pathlib import Path\nX = Path(__file__).parents[1]\n",
            "backend/appx/other.py": "from pathlib import Path\nX = Path(__file__).parents[1]\n",
            "frontend/src/x.py": "from pathlib import Path\nX = Path(__file__).parents[1]\n",
        },
    )
    result = cfp.scan(root)
    assert result.errors == []
    assert set(result.files) == {
        "backend/app/__init__.py",
        "backend/app/sub/deep.py",
        "backend/main.py",
        "client/__init__.py",
        "client/nested/win.py",
    }
    assert result.findings == [], _report(result.findings)


# ── A2 四类判据的最小正例（含别名、局部赋值、getattr） ────────────────────────────


A2_CASES = [
    # 类别 file
    ("file", "import pathlib\n\nP = pathlib.Path(__file__)\n", "file", "__file__"),
    ("file-concat", 'X = __file__ + "/a"\n', "file", "__file__"),
    # 类别 parents
    ("parents-1", "def f(p):\n    return p.parents[1]\n", "parents", "parents"),
    ("parents-n", "def f(p, n):\n    return p.parents[n]\n", "parents", "parents"),
    # 类别 cwd：显式 CWD
    ("cwd-os", "import os\n\nX = os.getcwd()\n", "cwd", "getcwd"),
    ("cwd-path", "from pathlib import Path\n\nX = Path.cwd()\n", "cwd", "cwd"),
    # 类别 cwd：未锚定的相对路径消费
    ("cwd-resolve", "from pathlib import Path\n\nX = Path('data').resolve()\n", "cwd", "resolve"),
    ("cwd-absolute", "from pathlib import Path\n\nX = Path().absolute()\n", "cwd", "absolute"),
    ("cwd-open", "X = open('data/a')\n", "cwd", "open()"),
    ("cwd-read", "from pathlib import Path\n\nX = Path('data/a').read_text()\n", "cwd", "read_text"),
    ("cwd-write", "from pathlib import Path\n\nPath('data/a').write_text('x')\n", "cwd", "write_text"),
    ("cwd-mkdir", "from pathlib import Path\n\nPath('data/a').mkdir()\n", "cwd", "mkdir"),
    # 类别 cwd：导入别名
    ("cwd-alias-path", "from pathlib import Path as P\n\nP('data').mkdir()\n", "cwd", "mkdir"),
    ("cwd-alias-module", "import pathlib as pl\n\npl.Path('data').mkdir()\n", "cwd", "mkdir"),
    ("cwd-alias-os", "import os as o\n\nX = o.getcwd()\n", "cwd", "getcwd"),
    ("cwd-alias-from", "from os import getcwd as cwd\n\nX = cwd()\n", "cwd", "cwd"),
    # 类别 cwd：同一作用域直接赋值后消费
    ("cwd-local", "from pathlib import Path\n\np = Path('data')\np.mkdir()\n", "cwd", "mkdir"),
    ("cwd-local-open", "from pathlib import Path\n\np = Path('data/a')\nopen(p / 'b')\n", "cwd", "open()"),
    # 类别 meipass
    ("meipass-attr", "import sys\n\nX = sys._MEIPASS\n", "meipass", "_MEIPASS"),
    ("meipass-getattr", 'import sys\n\nX = getattr(sys, "_MEIPASS", None)\n', "meipass", "getattr"),
    ("meipass-alias", "import sys as s\n\nX = s._MEIPASS\n", "meipass", "_MEIPASS"),
    ("meipass-from", "from sys import _MEIPASS as bundle\n\nX = bundle\n", "meipass", "_MEIPASS"),
]


@pytest.mark.parametrize("case, source, category, needle", A2_CASES, ids=[c[0] for c in A2_CASES])
def test_a2_minimal_positives(tmp_path: Path, case: str, source: str, category: str, needle: str) -> None:
    result = cfp.scan(_probe_tree(tmp_path, source))
    assert result.errors == [], result.errors
    hit = [f for f in result.findings if f.category == category]
    assert hit, f"[{case}] 未报出 {category} 类违规：\n{_report(result.findings)}"
    assert any(needle in f.detail for f in hit), f"[{case}] 命中原因未含 {needle!r}：\n{_report(hit)}"


def test_a2_meipass_import_line_is_reported(tmp_path: Path) -> None:
    """`from sys import _MEIPASS` 本身即读取——不能因为后面没用到就漏掉。"""
    result = cfp.scan(_probe_tree(tmp_path, "from sys import _MEIPASS\n"))
    assert [f.category for f in result.findings] == ["meipass"], _report(result.findings)


# ── A3 注释 / docstring / 普通说明字符串不误报；语义字符串仍参与判断 ──────────────


A3_GREEN = [
    ("comment", "# Path(__file__).parents[1] / sys._MEIPASS / open('data/a')\nX = 1\n"),
    ("docstring", '"""说明：Path(__file__) 与 parents[1] 与 sys._MEIPASS 都不许裸用。"""\nX = 1\n'),
    ("plain-string", 'S = "Path(__file__).parents[1]"\nX = 1\n'),
    ("mode-arg", "def log(target):\n    target.open('a', encoding='utf-8')\n"),
    ("str-replace", "def norm(text):\n    return text.replace('\\r\\n', '\\n').replace('\\n', ' ')\n"),
    ("name-subscript", "parents = [1, 2]\nX = parents[0]\n"),
]


@pytest.mark.parametrize("case, source", A3_GREEN, ids=[c[0] for c in A3_GREEN])
def test_a3_comments_and_strings_are_not_violations(tmp_path: Path, case: str, source: str) -> None:
    result = cfp.scan(_probe_tree(tmp_path, source))
    assert result.errors == [], result.errors
    assert result.findings == [], f"[{case}] 误报：\n{_report(result.findings)}"


def test_a3_getattr_attribute_name_still_judged(tmp_path: Path) -> None:
    """`getattr` 的属性名字符串不能因「字符串一概忽略」被放过。"""
    result = cfp.scan(_probe_tree(tmp_path, 'import sys\n\nX = getattr(sys, "_MEIPASS")\n'))
    assert [f.category for f in result.findings] == ["meipass"], _report(result.findings)


def test_a3_path_argument_string_still_judged(tmp_path: Path) -> None:
    """路径实参里的字符串同样不能被整体忽略。"""
    result = cfp.scan(_probe_tree(tmp_path, "X = open('data/a')\n"))
    assert [f.category for f in result.findings] == ["cwd"], _report(result.findings)


# ── A4 有效例外恰好五项；不豁免整文件、不凭同名符号 ──────────────────────────────


MAIN_WITH_LEAKS = '''import sys
from pathlib import Path


def _roots():
    """合法出口：四个函数之一（本文件 + 本符号）。"""
    return Path(__file__).resolve().parents[1]


def other():
    return Path(__file__)


_LEAK = Path(__file__).resolve().parents[2]


def _roots_outer():
    def _inner():
        return Path(__file__).resolve().parents[1]

    return _inner
'''


def test_a4_exempt_functions_pass_and_everything_else_reports(tmp_path: Path) -> None:
    root = _write_files(
        tmp_path,
        {
            "backend/app/__init__.py": "",
            "client/__init__.py": "",
            "backend/main.py": MAIN_WITH_LEAKS,
        },
    )
    result = cfp.scan(root)
    lines = {f.lineno for f in result.findings if f.path == "backend/main.py"}
    assert 7 not in lines, f"`_roots` 的合法写法被误报：\n{_report(result.findings)}"
    assert lines == {11, 14, 19}, f"同文件其他函数 / 模块级 / 嵌套函数体应各报一处：\n{_report(result.findings)}"
    assert {f.category for f in result.findings} == {"file", "parents"}, _report(result.findings)


def test_a4_same_symbol_in_another_file_is_not_exempt(tmp_path: Path) -> None:
    """按「文件 + 符号」登记：另一个文件里的同名函数**不**获豁免。"""
    root = _real_tree(
        tmp_path,
        extra={"backend/app/probe_same_name.py": "from pathlib import Path\n\n\ndef _roots():\n    return Path(__file__).resolve().parents[1]\n"},
    )
    result = cfp.scan(root)
    assert {f.path for f in result.findings} == {"backend/app/probe_same_name.py"}, _report(result.findings)
    assert {f.category for f in result.findings} == {"file", "parents"}, _report(result.findings)


def test_a4_decorator_and_default_args_are_not_exempt(tmp_path: Path) -> None:
    """豁免不扩到装饰器与默认参数（它们在**外层**作用域求值）。"""
    root = _real_tree(
        tmp_path,
        overrides={
            "backend/main.py": (REPO / "backend/main.py")
            .read_text(encoding="utf-8")
            + '\n\n@(lambda f: f)(Path(__file__).resolve())\ndef _extra():\n    return 1\n\n\n'
            "def _takes_default(p=Path(__file__).resolve()):\n    return p\n"
        },
    )
    result = cfp.scan(root)
    assert len(result.findings) == 2, _report(result.findings)
    assert {f.category for f in result.findings} == {"file"}, _report(result.findings)


def test_a4_root_uses_restricted_exception_not_generic_exemption(tmp_path: Path) -> None:
    """`ROOT` 必须走 §2.2 的结构校验：真实副本通过，且它是**第五项**。"""
    result = cfp.scan(_real_tree(tmp_path))
    assert result.findings == [], _report(result.findings)
    assert [e.target for e in result.exceptions] == list(EXPECTED_EXCEPTIONS)
    assert [e.target for e in result.exceptions if e.used] != [], "五项例外都没有被消费，白名单可能已成装饰"


# ── A5 白名单按文件 + 符号登记，不含行号；行号漂移不失效 ────────────────────────


def test_a5_whitelist_has_no_line_numbers() -> None:
    assert cfp.EXCEPTIONS == cfp._EXPECTED
    assert all(isinstance(part, str) for entry in cfp.EXCEPTIONS for part in entry), cfp.EXCEPTIONS
    assert len(cfp.EXCEPTIONS) == 5


def test_a5_line_drift_keeps_the_gate_green(tmp_path: Path) -> None:
    pad = "\n" * 10 + "# 行号漂移注入（结构未变，判据不应失效）\n"
    main_src = (REPO / "backend/main.py").read_text(encoding="utf-8").replace("\ndef _roots(", pad + "def _roots(", 1)
    env_src = (
        (REPO / "backend/app/core/env_store.py")
        .read_text(encoding="utf-8")
        .replace("\ndef _resolve_root_dir(", pad + "def _resolve_root_dir(", 1)
    )
    client_src = (
        (REPO / "client/app.py")
        .read_text(encoding="utf-8")
        .replace("\ndef backend_dir(", pad + "def backend_dir(", 1)
        .replace("\ndef log_file(", pad + "def log_file(", 1)
        .replace('    if getattr(sys, "frozen", False):', "    # 体内注释注入（AST 不可见）\n    if getattr(sys, \"frozen\", False):", 1)
    )
    root = _real_tree(tmp_path, overrides={"backend/main.py": main_src, "backend/app/core/env_store.py": env_src, "client/app.py": client_src})
    result = cfp.scan(root)
    assert result.findings == [], _report(result.findings)
    assert any(e.used for e in result.exceptions), "漂移后例外应当仍被消费"


# ── A6 真仓：白名单外零命中 + 白名单恰好五项 ────────────────────────────────────


def test_a6_real_repo_is_clean_and_whitelist_is_exactly_five() -> None:
    result = cfp.scan(REPO)
    assert result.errors == [], result.errors
    assert result.findings == [], _report(result.findings)
    assert [e.target for e in result.exceptions] == list(EXPECTED_EXCEPTIONS)
    assert "backend/main.py" in result.files and len(result.files) >= 100
    assert result.exit_code == 0


# ── A7 / A8 / A8b 变异自证（真仓只读，副本上动手） ───────────────────────────────


MUTANT = "from pathlib import Path\n\n\ndef leak():\n    return Path(__file__).parents[1]\n"


def test_a7_injection_turns_the_gate_red(tmp_path: Path) -> None:
    root = _real_tree(tmp_path, extra={"backend/app/probe_mutant.py": MUTANT})
    result = cfp.scan(root)
    hit = [f for f in result.findings if f.path == "backend/app/probe_mutant.py"]
    assert hit, f"注入裸路径后扫描器没变红：\n{_report(result.findings)}"
    assert 5 in {f.lineno for f in hit}, _report(hit)
    assert {f.category for f in hit} <= {"file", "parents"}, _report(hit)


def test_a8_removing_the_injection_turns_it_green_again(tmp_path: Path) -> None:
    root = _real_tree(tmp_path, extra={"backend/app/probe_mutant.py": MUTANT})
    assert cfp.scan(root).findings, "前置条件不成立：注入态本应是红的"
    (root / "backend/app/probe_mutant.py").unlink()
    result = cfp.scan(root)
    assert result.findings == [], _report(result.findings)


def test_a8b_reverse_mutation_stays_green(tmp_path: Path) -> None:
    """反向变异：把裸路径写进 docstring / 注释，**必须仍绿**（否则后人会把这条扫描删掉）。"""
    doc_file = '"""反例：Path(__file__).parents[1] 与 sys._MEIPASS 只是说明文字。"""\n\nX = 1\n'
    comment_line = "# 反例：Path(__file__).resolve().parents[1]\n"
    chunking = (REPO / "backend/app/pipeline/chunking.py").read_text(encoding="utf-8")
    root = _real_tree(
        tmp_path,
        extra={"backend/app/probe_docstring.py": doc_file},
        overrides={"backend/app/pipeline/chunking.py": comment_line + chunking},
    )
    result = cfp.scan(root)
    assert result.findings == [], _report(result.findings)


# ── A9 扫描器不 import 被测模块、不改冻结态（独立子进程） ────────────────────────


SENTINEL = "SENTINEL_IMPORTED_6_2C"


def test_a9_scanner_is_pure_static_in_a_subprocess(tmp_path: Path) -> None:
    boom = f'raise RuntimeError("{SENTINEL}")\n'
    root = _write_files(
        tmp_path,
        {
            "backend/app/__init__.py": boom,
            "backend/app/probe.py": boom,
            "backend/main.py": boom,
            "client/__init__.py": boom,
        },
    )
    done = _cli(root)
    assert done.returncode in (0, 1), f"扫描器在哨兵树上异常退出：{done.returncode}\n{done.stdout}\n{done.stderr}"
    assert SENTINEL not in (done.stdout + done.stderr), "扫描器 import 了被测模块（哨兵被触发）"

    probe = (
        "import json, sys;"
        f"sys.path.insert(0, r'{SCANNER.parent}');"
        "import check_frozen_paths as c;"
        f"r = c.scan(r'{root}');"
        "print(json.dumps({'frozen': bool(getattr(sys, 'frozen', False)),"
        " 'mods': sorted(m for m in sys.modules if m in ('main', 'app', 'client')),"
        " 'findings': len(r.findings), 'errors': len(r.errors)}))"
    )
    done2 = subprocess.run(
        [sys.executable, "-B", "-c", probe], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    assert done2.returncode == 0, done2.stderr
    payload = done2.stdout.strip().splitlines()[-1]
    import json

    info = json.loads(payload)
    assert info["frozen"] is False, "扫描器改动了 sys.frozen"
    assert info["mods"] == [], f"扫描器 import 了产品模块：{info['mods']}"
    assert info["errors"] == 0


# ── A13 ROOT 例外不会放过冻结态误用 ─────────────────────────────────────────────


def _mutate_client(source: str, old: str, new: str) -> str:
    assert old in source, f"变异锚点不存在，测试本身需要维护：{old!r}"
    return source.replace(old, new, 1)


ROOT_VARIANTS = [
    (
        "new-root-read",
        lambda src: _mutate_client(
            src, 'ROOT = Path(__file__).resolve().parents[1]\n', 'ROOT = Path(__file__).resolve().parents[1]\n_EXTRA = ROOT / "data"\n'
        ),
    ),
    (
        "into-frozen-branch",
        lambda src: _mutate_client(
            src,
            '    if is_frozen():\n        return Path(sys.executable).resolve().parent / "start.log"\n    return ROOT / "client" / "start.log"\n',
            '    if is_frozen():\n        return ROOT / "client" / "start.log"\n    return Path(sys.executable).resolve().parent / "start.log"\n',
        ),
    ),
    (
        "frozen-return-removed",
        lambda src: _mutate_client(
            src,
            '    if is_frozen():\n        return Path(sys.executable).resolve().parent / "start.log"\n',
            "    if is_frozen():\n        pass\n",
        ),
    ),
    (
        "assignment-rhs-changed",
        lambda src: _mutate_client(src, "parents[1]", "parents[2]"),
    ),
]


@pytest.mark.parametrize("case, mutate", ROOT_VARIANTS, ids=[c[0] for c in ROOT_VARIANTS])
def test_a13_root_out_of_bounds_variants_turn_red(tmp_path: Path, case: str, mutate) -> None:
    client = mutate((REPO / "client/app.py").read_text(encoding="utf-8"))
    result = cfp.scan(_real_tree(tmp_path, overrides={"client/app.py": client}))
    assert result.findings, f"[{case}] ROOT 越界没有变红"
    assert any(f.path == "client/app.py" for f in result.findings), _report(result.findings)


def test_a13_root_variants_are_green_again_when_reverted(tmp_path: Path) -> None:
    result = cfp.scan(_real_tree(tmp_path))
    assert result.findings == [], _report(result.findings)


def test_a13_comment_and_blank_lines_inside_root_functions_stay_green(tmp_path: Path) -> None:
    client = (REPO / "client/app.py").read_text(encoding="utf-8")
    client = client.replace(
        "def backend_dir() -> Path:",
        "def backend_dir() -> Path:\n    # 注入注释：不应影响结构判定\n",
        1,
    )
    client = client.replace("def log_file() -> Path:", "\n\n# 注入空行与注释\ndef log_file() -> Path:", 1)
    result = cfp.scan(_real_tree(tmp_path, overrides={"client/app.py": client}))
    assert result.findings == [], _report(result.findings)


# ── A14 构建入口真实阻断（临时副本跑**实际** build.ps1） ────────────────────────


BROKEN = "from pathlib import Path\n\n\ndef leak():\n    return Path(__file__).parents[1]\n"


def _build_copy(tmp_path: Path, *, mutation: str | None = BROKEN) -> Path:
    root = tmp_path / "copy"
    for rel in ("backend/app", "client", "backend/main.py"):
        source = REPO / rel
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    (root / "backend/scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(SCANNER, root / "backend/scripts/check_frozen_paths.py")
    (root / "tools/packaging").mkdir(parents=True, exist_ok=True)
    shutil.copy2(BUILD_PS1, root / "tools/packaging/build.ps1")
    if mutation:
        (root / "backend/app/probe_broken.py").write_text(mutation, encoding="utf-8")
    return root


def _run_build(root: Path, *args: str) -> subprocess.CompletedProcess:
    powershell = shutil.which("pwsh") or "pwsh"
    return subprocess.run(
        [powershell, "-NoProfile", "-File", str(root / "tools/packaging/build.ps1"), "-Python", sys.executable, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(root),
    )


@pytest.mark.parametrize("args", [(), ("-SkipDriftCheck",), ("-PathGateOnly",)], ids=["plain", "skip-drift", "gate-only"])
def test_a14_real_build_entry_blocks_before_packaging(tmp_path: Path, args) -> None:
    root = _build_copy(tmp_path)
    done = _run_build(root, *args)
    out = done.stdout + done.stderr
    assert done.returncode == 4, f"构建入口未按退出码 4 阻断（args={args}）：{done.returncode}\n{out[-2000:]}"
    assert "FAIL" in out and "命中计数" in out, f"红灯必须由路径闸门给出，而不是别的缺失：\n{out[-2000:]}"
    assert not (root / "dist").exists() and not (root / "build").exists(), "违规时不得生成 dist / build"


def test_a14_legal_copy_passes_gate_only(tmp_path: Path) -> None:
    root = _build_copy(tmp_path, mutation=None)
    done = _run_build(root, "-PathGateOnly")
    out = done.stdout + done.stderr
    assert done.returncode == 0, f"-PathGateOnly 未退 0：{done.returncode}\n{out[-2000:]}"
    assert "缺少必需资源" not in out, "合法副本不该走到资源预检"
    assert not (root / "dist").exists() and not (root / "build").exists()


def test_a14_legal_copy_is_not_a_path_red_light(tmp_path: Path) -> None:
    """合法副本走普通入口时，红灯必须来自**依赖/资源缺失**（退出码 1），而不是路径闸门（4）。"""
    root = _build_copy(tmp_path, mutation=None)
    done = _run_build(root)
    out = done.stdout + done.stderr
    assert done.returncode != 4, f"合法副本被判成路径红灯：\n{out[-2000:]}"
    assert "冻结安全路径闸门通过" in out, out[-2000:]
    assert "缺少必需资源" in out, "闸门通过后应继续走到资源预检（证明闸门没有短路掉后续步骤）"
    assert not (root / "dist").exists()


def test_a15_missing_scanner_blocks_the_build(tmp_path: Path) -> None:
    root = _build_copy(tmp_path, mutation=None)
    (root / "backend/scripts/check_frozen_paths.py").unlink()
    for args in ((), ("-PathGateOnly",)):
        done = _run_build(root, *args)
        assert done.returncode == 4, f"缺扫描器时未退 4（args={args}）：{done.returncode}"
    assert not (root / "dist").exists()


def test_a15_syntax_damage_blocks_the_build(tmp_path: Path) -> None:
    root = _build_copy(tmp_path, mutation="def broken(:\n")
    done = _run_build(root, "-PathGateOnly")
    assert done.returncode == 4, f"语法损坏时未退 4：{done.returncode}\n{done.stdout[-1500:]}"
    assert "输入错误" in (done.stdout + done.stderr)


# ── A15 输入错误不能假绿（CLI 退 2） ─────────────────────────────────────────────


INPUT_ERROR_CASES = [
    ("missing-app-root", {"backend/main.py": "", "client/__init__.py": ""}),
    ("empty-app-root", {"backend/app/.keep": "", "backend/main.py": "", "client/__init__.py": ""}),
    ("missing-main", {"backend/app/__init__.py": "", "client/__init__.py": ""}),
    ("empty-client-root", {"backend/app/__init__.py": "", "backend/main.py": "", "client/.keep": ""}),
    ("syntax-damage", {"backend/app/__init__.py": "", "backend/app/broken.py": "def f(:\n", "backend/main.py": "", "client/__init__.py": ""}),
    ("read-failure", {"backend/app/__init__.py": "", "backend/app/bad.py": b"\xff\xfe\x00\x00bad", "backend/main.py": "", "client/__init__.py": ""}),
]


@pytest.mark.parametrize("case, files", INPUT_ERROR_CASES, ids=[c[0] for c in INPUT_ERROR_CASES])
def test_a15_input_errors_are_not_reported_green(tmp_path: Path, case: str, files) -> None:
    root = _write_files(tmp_path, files)
    result = cfp.scan(root)
    assert result.errors, f"[{case}] 输入错误没有进 errors"
    assert result.exit_code == 2, f"[{case}] 退出码应为 2，实测 {result.exit_code}"
    done = _cli(root)
    assert done.returncode == 2, f"[{case}] CLI 退出码应为 2，实测 {done.returncode}\n{done.stdout}"
    assert "输入错误" in done.stdout


# ── A16 合法相对路径构造不误报；同一相对变量未锚定则变红 ────────────────────────


A16_GREEN = [
    ("rootdir-join", "from pathlib import Path\n\nROOT_DIR = Path('C:/repo')\n\ndef f():\n    return (ROOT_DIR / 'data' / 'a.txt').read_text()\n"),
    ("absolute", "from pathlib import Path\n\nPath(r'C:\\tmp\\a').mkdir()\n"),
    ("home", "from pathlib import Path\n\nPath('~/zhilan/models').mkdir()\n"),
    ("config-shape", "from pathlib import Path\n\nROOT_DIR = Path('C:/repo')\nsettings = type('S', (), {'UPLOAD_DIR': ''})()\n\ndef upload_path():\n    raw = (settings.UPLOAD_DIR or '').strip()\n    path = Path(raw or 'data/uploads').expanduser()\n    return path if path.is_absolute() else ROOT_DIR / path\n"),
    ("anchor-then-use", "from pathlib import Path\n\nROOT_DIR = Path('C:/repo')\np = Path('data')\nq = ROOT_DIR / p\nq.mkdir()\n"),
    ("is-absolute-branch", "from pathlib import Path\n\nROOT_DIR = Path('C:/repo')\ndef resolve_stored(path):\n    p = Path(path)\n    if not p.is_absolute():\n        p = ROOT_DIR / p\n    return p.resolve()\n"),
]


@pytest.mark.parametrize("case, source", A16_GREEN, ids=[c[0] for c in A16_GREEN])
def test_a16_legal_relative_constructions_are_not_flagged(tmp_path: Path, case: str, source: str) -> None:
    result = cfp.scan(_probe_tree(tmp_path, source))
    assert result.errors == [], result.errors
    assert result.findings == [], f"[{case}] 误报：\n{_report(result.findings)}"


def test_a16_same_relative_variable_without_anchoring_turns_red(tmp_path: Path) -> None:
    source = "from pathlib import Path\n\nROOT_DIR = Path('C:/repo')\np = Path('data')\np.mkdir()\n"
    result = cfp.scan(_probe_tree(tmp_path, source))
    assert [f.category for f in result.findings] == ["cwd"], _report(result.findings)
