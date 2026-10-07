"""知澜 · 冻结安全路径闸门（阶段 6 · 条目 6.2c）。

把「三类锚点 + 禁止裸路径」的规则
做成**可执行的静态闸门**：新代码里出现下列写法即判违规。判定只看源码文本与 ``ast``，
**不 import 任何被测模块、不修改 ``sys.frozen``、不依赖 pytest**。

判据四类（阶段 6 §四 6.2c 重建规格 1 / 开工包 §2.1）：

① ``file``    ：裸 ``__file__``
② ``parents`` ：裸 ``parents[N]``（未经统一锚点就往上爬）
③ ``cwd``     ：显式 CWD（``os.getcwd()`` / ``Path.cwd()``）与**未锚定的相对路径消费**
④ ``meipass`` ：``sys._MEIPASS`` 散用（含 ``getattr`` 属性名与 ``from sys import _MEIPASS``）

唯一合法出口 = **恰好五项**：四个函数体（按「文件 + 符号」豁免，**不按行号**）+ 一项受限模块常量例外：
``backend/main.py::_roots`` · ``backend/app/core/env_store.py::_resolve_root_dir`` ·
``client/app.py::backend_dir`` · ``client/app.py::log_file`` · ``client/app.py::ROOT``。
白名单**不豁免整文件**、不凭同名符号放行、不新增第六项。

CLI：``0`` = 通过 / ``1`` = 违规 / ``2`` = 输入、读取或语法错误（**输入错误绝不返回全绿**）。

**未覆盖项**（如实登记，见证据 §未覆盖）：动态 ``eval`` / 字符串拼接出的路径 / 跨模块别名 /
跨作用域数据流 / 推导式与生成器表达式的独立作用域 / 不透明的函数返回值来源。本闸门只做
「本表语法 + 局部来源」检查，**不建通用 Python 数据流分析器**。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── 扫描面：三个根，递归 ``.py``（开工包 §2 第 1 条） ────────────────────────────────
APP_ROOT = "backend/app"
MAIN_FILE = "backend/main.py"
CLIENT_ROOT = "client"

# ── 唯一合法出口：恰好五项。**按「文件 + 符号」登记，数据结构里没有任何行号**（裁决 4 / A5）──
_EXPECTED: tuple[tuple[str, str, str], ...] = (
    ("backend/main.py", "_roots", "function"),
    ("backend/app/core/env_store.py", "_resolve_root_dir", "function"),
    ("client/app.py", "backend_dir", "function"),
    ("client/app.py", "log_file", "function"),
    ("client/app.py", "ROOT", "module-constant"),
)
EXCEPTIONS: tuple[tuple[str, str, str], ...] = _EXPECTED

ROOT_FILE = "client/app.py"
ROOT_NAME = "ROOT"

# ── ``client/app.py::ROOT`` 的冻结基准（§2.2：**从已验收版本固化，不每次现算期望值**）──
# 基准 = 去 docstring 后的 AST 归一化 dump 的 SHA256；不含行号 / 空白 / 注释，
# 故只对**结构变化**敏感（插入空行、加注释仍绿）。基准取自 6.2 验收态 `client/app.py`。
ROOT_ASSIGN_SHA256 = "7efba0850e8eeaef93e5c3de6b7d47927a3e4bde0e8161dfb3d03fc10717d3c2"
ROOT_BACKEND_DIR_SHA256 = "d0a8ca3da3b6c2e3ff789cf366c4067e129d4a25cb5c820534fe3700bc50f633"
ROOT_LOG_FILE_SHA256 = "cd70b3a0186f22fd4806881a7d447c591241295d8b5610368c9fc726e714ebbb"

# 路径消费方法（开工包 §2.1：resolve/absolute/open/read_*/write_*/mkdir/exists/is_*/iterdir/glob/rglob/unlink/rename/replace）
_CONSUME_METHODS = frozenset(
    {
        "resolve", "absolute", "open", "read_text", "read_bytes", "write_text", "write_bytes",
        "mkdir", "exists", "is_file", "is_dir", "iterdir", "glob", "rglob", "unlink",
        "rename", "replace", "touch",
    }
)
# ``os.<name>(...)``：实参是路径，全部检查（含 rename/replace 的**目标**——开工包 §2.1）
_OS_PATH_FUNCS = frozenset(
    {"chdir", "mkdir", "makedirs", "remove", "unlink", "rename", "replace", "listdir", "scandir", "walk", "rmdir"}
)
# 归一化动作：不改变「相对 / 绝对」性质，故状态随接收者传递（``Path("data").resolve()`` 仍是违规）
_NORMALIZE_ATTRS = frozenset({"resolve", "absolute", "expanduser", "parent", "parents", "with_name", "with_suffix"})

_ABS_TEXT = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|//|/)")


def is_anchored_text(text: str) -> bool:
    """字符串字面量是否「已锚定」：绝对路径或 ``~``（home 锚点）。"""
    return bool(_ABS_TEXT.match(text)) or text.startswith("~")


def _dotted(node: ast.AST) -> str:
    """把 ``Name`` / ``Attribute`` 链还原成点分名（``os.path.join`` / ``Path``），其它返回空串。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


def _sha256_dump(node: ast.AST) -> str:
    return hashlib.sha256(ast.dump(node, annotate_fields=True, include_attributes=False).encode("utf-8")).hexdigest()


def _strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(
        body[0].value.value, str
    ):
        return body[1:]
    return body


def body_digest(func: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """函数**体**（去 docstring）的归一化结构摘要——不含签名、行号、空白与注释。"""
    return _sha256_dump(ast.Module(body=_strip_docstring(func.body), type_ignores=[]))


# ── 结果结构 ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    """一条违规：``路径:行号  类别  原因``（排序与打印都用它）。"""

    path: str
    lineno: int
    category: str
    detail: str

    def line(self) -> str:
        return f"{self.path}:{self.lineno}  [{self.category}]  {self.detail}"


@dataclass
class ExceptionUsage:
    target: str  # 例：backend/main.py::_roots
    kind: str  # function / module-constant
    used: int = 0  # 该例外实际压掉的命中数

    @property
    def state(self) -> str:
        return "已消费" if self.used else "未消费"


@dataclass
class ScanResult:
    root: Path
    files: list[str] = field(default_factory=list)
    exceptions: list[ExceptionUsage] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    python: str = ""

    @property
    def ok(self) -> bool:
        return not self.findings and not self.errors

    @property
    def exit_code(self) -> int:
        if self.errors:
            return 2
        return 1 if self.findings else 0


class _Frame:
    """词法作用域帧：``relative`` / ``pathish`` 只在同一帧内传递（不跨函数泄漏）。"""

    __slots__ = ("kind", "node", "name", "relative", "pathish")

    def __init__(self, kind: str, node: ast.AST | None, name: str = "") -> None:
        self.kind = kind
        self.node = node
        self.name = name
        self.relative: dict[str, bool] = {}  # 名字 -> 是否为「未锚定的相对路径」
        self.pathish: dict[str, bool] = {}  # 名字 -> 是否可能持有一个 Path


class _FileScanner:
    """一个文件的扫描器：四类判据 + 白名单豁免。只读源码，不改动任何运行期状态。"""

    def __init__(self, rel: str, src: str, tree: ast.Module) -> None:
        self.rel = rel
        self.src = src
        self.tree = tree
        self.findings: list[Finding] = []
        self.suppressed = 0
        self.used: dict[str, int] = {}
        self.frames: list[_Frame] = []
        self._aliases = self._collect_aliases()
        self._exempt_nodes: list[ast.AST] = []
        self._exempt_funcs: set[int] = set()
        self._root_err: str = ""
        self._parent: dict[int, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self._parent[id(child)] = node

    # ---- 别名 ------------------------------------------------------------------
    def _collect_aliases(self) -> dict[str, set[str]]:
        path_ctors = {"Path"}
        cwd_calls: set[str] = set()
        os_alias = {"os"}
        meipass_names: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    alias = a.asname or a.name
                    if a.name == "pathlib":
                        path_ctors.add(f"{alias}.Path")
                    elif a.name == "os":
                        os_alias.add(alias)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for a in node.names:
                    alias = a.asname or a.name
                    if mod == "pathlib" and a.name == "Path":
                        path_ctors.add(alias)
                    elif mod == "os" and a.name == "getcwd":
                        cwd_calls.add(alias)
                    elif mod == "sys" and a.name == "_MEIPASS":
                        meipass_names.add(alias)
        for o in os_alias:
            cwd_calls.add(f"{o}.getcwd")
        for c in path_ctors:
            cwd_calls.add(f"{c}.cwd")
        return {"path_ctors": path_ctors, "cwd_calls": cwd_calls, "os_alias": os_alias, "meipass_names": meipass_names}

    # ---- 作用域 ----------------------------------------------------------------
    def _scope_state(self) -> dict[str, bool]:
        return self.frames[-1].relative if self.frames else {}

    def _scope_pathish(self) -> dict[str, bool]:
        return self.frames[-1].pathish if self.frames else {}

    def _current_function(self) -> _Frame | None:
        for frame in reversed(self.frames):
            if frame.kind == "function":
                return frame
        return None

    # ---- 表达式状态：relative / clean -------------------------------------------
    def _ctor_state(self, node: ast.Call) -> bool:
        args = node.args
        if any(isinstance(a, ast.Starred) for a in args):
            return False
        if not args:
            return True  # Path() → CWD
        if all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in args):
            return not is_anchored_text(args[0].value)  # type: ignore[arg-type]
        return False  # 含变量 / 表达式实参：静态判不出，不判违规（现有 config/skills 的构造形状）

    def _is_relative(self, node: ast.AST | None) -> bool:
        """``True`` = 该表达式的值是一条**未锚定的相对路径**。"""
        if node is None:
            return False
        if isinstance(node, ast.Name):
            return bool(self._scope_state().get(node.id, False))
        if isinstance(node, ast.Constant):
            return isinstance(node.value, str) and not is_anchored_text(node.value)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return self._is_relative(node.left)  # 相对左边拼 → 仍相对；锚定左边拼 → 已锚定
        if isinstance(node, ast.IfExp):
            return self._is_relative(node.body) or self._is_relative(node.orelse)
        if isinstance(node, ast.Call):
            dotted = _dotted(node.func)
            if dotted in self._aliases["cwd_calls"]:
                return True
            if dotted in self._aliases["path_ctors"]:
                return self._ctor_state(node)
            if dotted in self._aliases["meipass_names"]:
                return False
            if isinstance(node.func, ast.Attribute) and node.func.attr in _NORMALIZE_ATTRS:
                return self._is_relative(node.func.value)
            return False
        if isinstance(node, ast.Attribute):
            if node.attr in ("parent", "parents"):
                return self._is_relative(node.value)
            return False
        if isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Attribute) and node.value.attr in ("parent", "parents"):
                return self._is_relative(node.value.value)
            return False
        return False

    def _is_pathish(self, node: ast.AST | None) -> bool:
        """``True`` = 该表达式的值**可能是 Path**。

        「路径消费」判据必须以它为前提：``str.replace(...)`` / ``str.strip()`` 与 ``Path.replace(...)``
        同名不同物，若只看方法名，全仓 25 处字符串处理会被误判成违规
        （5.6 的实测教训：判据比现实更严 ⇒ 照字面实现必红 ⇒ 后人会去「修」本来正确的东西）。
        """
        if node is None:
            return False
        if isinstance(node, ast.Name):
            return bool(self._scope_pathish().get(node.id, False))
        if isinstance(node, ast.Call):
            dotted = _dotted(node.func)
            if dotted in self._aliases["path_ctors"] or dotted in self._aliases["cwd_calls"]:
                return True
            if isinstance(node.func, ast.Attribute) and node.func.attr in _NORMALIZE_ATTRS:
                return self._is_pathish(node.func.value)
            return False
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return self._is_pathish(node.left) or self._is_pathish(node.right)
        if isinstance(node, ast.IfExp):
            return self._is_pathish(node.body) or self._is_pathish(node.orelse)
        if isinstance(node, ast.Attribute):
            return node.attr in ("parent", "parents") and self._is_pathish(node.value)
        if isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Attribute) and node.value.attr in ("parent", "parents"):
                return self._is_pathish(node.value.value)
            return False
        return False

    # ---- 记录命中 ---------------------------------------------------------------
    def _add(self, node: ast.AST, category: str, detail: str) -> None:
        frame = self._current_function()
        if frame is not None and id(frame.node) in self._exempt_funcs:
            self.suppressed += 1
            self.used[frame.name] = self.used.get(frame.name, 0) + 1
            return
        if self._exempt_nodes and any(
            node is n or any(node is c for c in ast.walk(n)) for n in self._exempt_nodes
        ):
            self.suppressed += 1
            self.used[ROOT_NAME] = self.used.get(ROOT_NAME, 0) + 1
            return
        self.findings.append(Finding(self.rel, getattr(node, "lineno", 0), category, f"{detail}｜{self._snippet(node)}"))

    def _snippet(self, node: ast.AST) -> str:
        try:
            text = ast.get_source_segment(self.src, node) or ""
        except Exception:  # pragma: no cover - 兜底，源码片段拿不到不影响判定
            text = ""
        text = " ".join(text.split())
        return text if len(text) <= 90 else text[:87] + "..."

    # ---- 四类判据（单节点） ------------------------------------------------------
    def _visit_node(self, node: ast.AST) -> None:
        if isinstance(node, ast.Name) and node.id == "__file__":
            self._add(node, "file", "裸 __file__（拼路径会落在冻结解包目录）")
        if isinstance(node, ast.Attribute) and node.attr == "parents":
            parent = self._parent.get(id(node))
            if isinstance(parent, ast.Subscript) and parent.value is node:
                self._add(node, "parents", "裸 parents[N]（未经统一锚点往上爬）")
        if isinstance(node, ast.Attribute) and node.attr == "_MEIPASS":
            self._add(node, "meipass", "散用 _MEIPASS（冻结资源根只应由统一锚点函数持有）")
        if isinstance(node, ast.ImportFrom) and node.module == "sys" and any(a.name == "_MEIPASS" for a in node.names):
            self._add(node, "meipass", "直接导入 _MEIPASS —— 导入本身即读取")
        if isinstance(node, ast.Name) and node.id in self._aliases["meipass_names"]:
            self._add(node, "meipass", "散用从 sys 导入的 _MEIPASS")
        if isinstance(node, ast.Call):
            self._check_call(node)

    def _check_call(self, node: ast.Call) -> None:
        dotted = _dotted(node.func)
        # getattr(x, "_MEIPASS", ...) —— 属性名字符串必须参与语义判断，不能一概忽略
        if dotted == "getattr" and len(node.args) >= 2:
            attr = node.args[1]
            if isinstance(attr, ast.Constant) and attr.value == "_MEIPASS":
                self._add(node, "meipass", 'getattr(..., "_MEIPASS", ...) 散用')
        # 显式 CWD
        if dotted in self._aliases["cwd_calls"]:
            self._add(node, "cwd", f"显式读取 CWD：{dotted}()")
        # os.<path func>(...)：实参全查
        head, _, tail = dotted.rpartition(".")
        if tail and head in self._aliases["os_alias"] and tail in _OS_PATH_FUNCS:
            for arg in node.args:
                if self._is_relative(arg):
                    self._add(node, "cwd", f"{head}.{tail}() 的路径实参是未锚定的相对路径")
        # 内建 open(<路径>)：只查第一个实参（模式串不是路径）
        if dotted == "open" and node.args and self._is_relative(node.args[0]):
            self._add(node, "cwd", "open() 的路径实参是未锚定的相对路径")
        # 方法是路径消费：接收者必须是**可能是 Path** 的值（否则 str.replace 之类会被误判）
        if isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            recv = node.func.value
            if attr in _CONSUME_METHODS and self._is_pathish(recv) and self._is_relative(recv):
                self._add(node, "cwd", f"未锚定的相对路径被 .{attr}() 消费")
            elif (
                attr in ("rename", "replace")
                and self._is_pathish(recv)
                and node.args
                and self._is_relative(node.args[0])
            ):
                self._add(node, "cwd", f".{attr}() 的目标是未锚定的相对路径")

    # ---- 遍历（自己走，保证作用域与语句顺序） -------------------------------------
    def _visit_body(self, stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            self._visit_stmt(stmt)

    def _visit_stmt(self, stmt: ast.AST) -> None:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in stmt.decorator_list:  # 装饰器/默认参数按**外层**作用域判定
                self._visit_tree(dec)
            self._visit_tree(stmt.args)
            if stmt.returns is not None:
                self._visit_tree(stmt.returns)
            self.frames.append(_Frame("function", stmt, stmt.name))
            self._visit_body(stmt.body)
            self.frames.pop()
            return
        if isinstance(stmt, ast.ClassDef):
            for dec in stmt.decorator_list:
                self._visit_tree(dec)
            self.frames.append(_Frame("class", stmt, stmt.name))
            self._visit_body(stmt.body)
            self.frames.pop()
            return
        if isinstance(stmt, ast.Lambda):
            self._visit_tree(stmt.args)
            self.frames.append(_Frame("function", stmt, "<lambda>"))
            self._visit_tree(stmt.body)
            self.frames.pop()
            return
        if isinstance(stmt, ast.Assign):
            self._visit_tree(stmt.value)
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    self._scope_state()[target.id] = self._is_relative(stmt.value)
                    self._scope_pathish()[target.id] = self._is_pathish(stmt.value)
                else:
                    self._visit_tree(target)
            return
        if isinstance(stmt, ast.AnnAssign):
            self._visit_tree(stmt.value)
            if isinstance(stmt.target, ast.Name):
                self._scope_state()[stmt.target.id] = self._is_relative(stmt.value)
                self._scope_pathish()[stmt.target.id] = self._is_pathish(stmt.value)
            else:
                self._visit_tree(stmt.target)
            return
        self._visit_tree(stmt)

    def _visit_tree(self, node: ast.AST | None) -> None:
        """深度优先：语句交给 ``_visit_stmt``（管作用域与赋值），其余表达式逐个判。"""
        if node is None:
            return
        self._visit_node(node)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                self._visit_stmt(child)
            else:
                self._visit_tree(child)

    # ---- 入口 -------------------------------------------------------------------
    def run(self, exempt_funcs: set[str]) -> None:
        self._exempt_funcs = {id(n) for n in self._module_functions() if n.name in exempt_funcs}
        self._pin_root_exception()
        self.frames.append(_Frame("module", self.tree, "<module>"))
        self._visit_body(self.tree.body)
        self.frames.pop()

    def _module_functions(self) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
        return [n for n in self.tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    # ---- ``client/app.py::ROOT`` 受限例外（开工包 §2.2） --------------------------
    def _pin_root_exception(self) -> None:
        if self.rel != ROOT_FILE:
            return
        defs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {n.name: n for n in self._module_functions()}
        assigns: list[ast.stmt] = []
        simple: ast.Assign | None = None
        assign_line = 0
        for node in self.tree.body:  # ROOT 只许是**顶层**赋值
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == ROOT_NAME for t in node.targets
            ):
                assigns.append(node)
                assign_line = node.lineno
                if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    simple = node
        for node in ast.walk(self.tree):  # 函数内等其它位置的重赋值同样不许
            if isinstance(node, ast.Assign) and not any(node is a for a in assigns):
                if any(isinstance(t, ast.Name) and t.id == ROOT_NAME for t in node.targets):
                    self._root_error(f"`{ROOT_NAME}` 被再次赋值（只允许顶层唯一一次赋值）")
                    break
        if not self._root_error_text and len(assigns) != 1:
            self._root_error(f"`{ROOT_NAME}` 顶层赋值应恰好 1 处，实测 {len(assigns)} 处")
        if not self._root_error_text and simple is None:
            self._root_error(f"`{ROOT_NAME}` 必须是单目标简单赋值（不得元组 / 复合目标）")
        if not self._root_error_text and simple is not None:
            if _sha256_dump(simple) != ROOT_ASSIGN_SHA256:
                self._root_error(f"`{ROOT_NAME}` 赋值表达式结构已偏离冻结基准（RHS 被改）")
            else:
                self._exempt_nodes.append(simple.value)

        for name, pin in (("backend_dir", ROOT_BACKEND_DIR_SHA256), ("log_file", ROOT_LOG_FILE_SHA256)):
            func = defs.get(name)
            if func is None:
                self._root_error(f"缺少 `{name}()`（受限例外的载体不存在）")
                continue
            if body_digest(func) != pin:
                self._root_error(f"`{name}()` 函数体结构已偏离冻结基准（{self._why(func)}）")

        # ROOT 的**读取**只许出现在上面两个函数体（即那两条开发态 return）里
        allowed: set[int] = set()
        for name in ("backend_dir", "log_file"):
            func = defs.get(name)
            if func is not None:
                allowed |= {id(n) for n in ast.walk(func) if isinstance(n, ast.Name) and n.id == ROOT_NAME}
        extra = [
            n
            for n in ast.walk(self.tree)
            if isinstance(n, ast.Name) and n.id == ROOT_NAME and isinstance(n.ctx, ast.Load) and id(n) not in allowed
        ]
        if extra:
            self._root_error(f"`{ROOT_NAME}` 被两处开发态 return 之外的地方读取（{len(extra)} 处）")

        if self._root_error_text:
            self.findings.append(
                Finding(
                    self.rel,
                    assign_line or 1,
                    "root-exception",
                    f"{self._root_error_text}｜{self.rel}::{ROOT_NAME}",
                )
            )

    @property
    def _root_error_text(self) -> str:
        return self._root_err

    def _root_error(self, text: str) -> None:
        if not self._root_err:
            self._root_err = text

    def _why(self, func: ast.FunctionDef) -> str:
        body = _strip_docstring(func.body)
        if not body:
            return "函数体为空"
        first = body[0]
        if not (isinstance(first, ast.If) and _dotted(first.test) == "is_frozen"):
            return "首个语句不是 if is_frozen(): 的冻结分支"
        if not first.body or not isinstance(first.body[-1], ast.Return):
            return "冻结分支缺少无条件 return"
        if any(isinstance(n, ast.Name) and n.id == ROOT_NAME for n in ast.walk(first)):
            return f"冻结分支内出现了 `{ROOT_NAME}` 读取"
        if not isinstance(body[-1], ast.Return):
            return "开发态分支未以 return 结束"
        return "结构不在冻结基准内"


# ── 目录级扫描 ───────────────────────────────────────────────────────────────────


def _discover(root: Path) -> tuple[list[Path], list[str]]:
    """按三个根枚举 ``.py``；返回 ``(文件列表, 输入错误)``。"""
    errors: list[str] = []
    files: list[Path] = []
    app = root / APP_ROOT
    if not app.is_dir():
        errors.append(f"缺少扫描根目录：{APP_ROOT}")
    else:
        found = sorted(p for p in app.rglob("*.py") if p.is_file())
        if not found:
            errors.append(f"{APP_ROOT} 下没有任何 .py 文件")
        files += found
    main = root / MAIN_FILE
    if not main.is_file():
        errors.append(f"缺少必需文件：{MAIN_FILE}")
    else:
        files.append(main)
    client = root / CLIENT_ROOT
    if not client.is_dir():
        errors.append(f"缺少扫描根目录：{CLIENT_ROOT}")
    else:
        found = sorted(p for p in client.rglob("*.py") if p.is_file())
        if not found:
            errors.append(f"{CLIENT_ROOT} 下没有任何 .py 文件")
        files += found
    return files, errors


def scan(root: str | Path) -> ScanResult:
    """扫描一个仓库：返回结构化结果（不抛异常）。"""
    root_path = Path(root).resolve()
    result = ScanResult(
        root=root_path,
        python=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )
    if not root_path.is_dir():
        result.errors.append(f"仓库根不存在或不是目录：{root_path}")
        return result

    files, errors = _discover(root_path)
    result.errors += errors

    # 白名单自检：**恰好五项**，多一项少一项都算失败（不能变成放行条）
    if EXCEPTIONS != _EXPECTED or len(EXCEPTIONS) != 5:
        result.errors.append(
            f"白名单登记被改动：应为固定五项，实测 {len(EXCEPTIONS)} 项 —— 新增例外须走评审，不得就地放行"
        )
    if [k for _, _, k in EXCEPTIONS].count("module-constant") != 1:
        result.errors.append("受限模块常量例外应恰好 1 项")

    fn_table: dict[str, set[str]] = {}
    for path, symbol, kind in EXCEPTIONS:
        if kind == "function":
            fn_table.setdefault(path, set()).add(symbol)
    usage = {f"{path}::{symbol}": ExceptionUsage(f"{path}::{symbol}", kind) for path, symbol, kind in EXCEPTIONS}

    for path in files:
        rel = path.relative_to(root_path).as_posix()
        result.files.append(rel)
        try:
            src = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            result.errors.append(f"源码读取失败：{rel}（{exc}）")
            continue
        try:
            tree = ast.parse(src, filename=rel)
        except SyntaxError as exc:
            result.errors.append(f"语法解析失败：{rel}:{exc.lineno}（{exc.msg}）")
            continue
        scanner = _FileScanner(rel, src, tree)
        try:
            scanner.run(fn_table.get(rel, set()))
        except RecursionError as exc:  # pragma: no cover - 防御性
            result.errors.append(f"分析失败：{rel}（{exc}）")
            continue
        result.findings += scanner.findings
        for name, count in scanner.used.items():
            key = f"{rel}::{name}"
            if key in usage:
                usage[key].used = count

    result.exceptions = [usage[f"{p}::{s}"] for p, s, _ in EXCEPTIONS]
    result.findings.sort(key=lambda f: (f.path, f.lineno, f.category, f.detail))
    result.errors.sort()
    return result


# ── 命令行 ───────────────────────────────────────────────────────────────────────


def _default_root() -> Path:
    return Path(__file__).resolve().parents[2]


def render(result: ScanResult) -> str:
    lines: list[str] = [
        "知澜 · 冻结安全路径闸门（阶段 6 · 条目 6.2c）",
        f"仓库根        ：{result.root}",
        f"扫描根        ：{APP_ROOT}  {MAIN_FILE}  {CLIENT_ROOT}",
        f"扫描文件数    ：{len(result.files)}",
        f"有效例外（{len(result.exceptions)} 项）："
        + "、".join(f"{e.target}（{e.state}）" for e in result.exceptions),
    ]
    if result.errors:
        lines.append("")
        for e in result.errors:
            lines.append(f"[输入错误] {e}")
    if result.findings:
        lines.append("")
        for f in result.findings:
            lines.append(f"[违规] {f.line()}")
    counts: dict[str, int] = {}
    for f in result.findings:
        counts[f.category] = counts.get(f.category, 0) + 1
    lines.append("")
    lines.append(f"命中计数      ：{len(result.findings)}" + (f"（{counts}）" if counts else ""))
    if result.errors:
        lines.append("结论          ：输入 / 读取 / 语法错误（退出码 2）——**不得当作通过**")
    elif result.findings:
        lines.append("结论          ：FAIL（退出码 1）——新增裸路径必须先改成统一锚点，或按流程申请例外")
    else:
        lines.append("结论          ：PASS（退出码 0）——白名单外零命中")
    lines.append(f"Python        ：{result.python}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="知澜冻结安全路径闸门（阶段 6 · 条目 6.2c）")
    parser.add_argument("--root", default=None, help="待扫描仓库根；省略时从扫描器自身位置推导（不依赖 CWD）")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else _default_root()
    result = scan(root)
    print(render(result))
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover - CLI 分支
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
