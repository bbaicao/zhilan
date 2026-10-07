"""A13b · 前端白名单与后端白名单的**等价性**门禁（工具升级-3 · 回归检查）。

**问题**：前端是 TS、后端是 Python ⇒「两边等价」**不能靠 import 比对**。
本仓此前**无运行时读取前端文件的既有判据**（现读：`tests/` 下 20 处 `read_text` 先例全部读
`.py` / `.json` / `.jsonl`，**零处读 TS**）⇒ 本文件是新立的抽法。

**抽法 = 锚定切片 + `json.loads`（不是自由正则）**（包内 `D-回归检查` 逐字）：
- `App.tsx` 的 `ALLOWED_DOC` 是**纯字符串数组字面量** ⇒ 合法 JSON ⇒ 可 `json.loads`；
- `accept="…"` 是**逗号串** ⇒ 按 `","` 切；
- 文案是 `/` 分隔的 token 串 ⇒ 先切掉「支持 」与「，单文件」，再按 `"/"` 切、补 `.` 前缀。

🔴 **与 `A17` 的边界**：`D-回归检查` 的「不得用正则」**只管 `_paths.py` 的 import 面**，
**不是全仓禁令**。本文件用 `str.index` 定位 + `json.loads`，**不是自由正则** ⇒ 不冲突、零新依赖。

🔴 **必须把「抽取失败」与「值不等」分开断言**（`D-回归检查` + `K-回归检查`）：
`json.JSONDecodeError` = **抽取器失效（须换抽法）**；`set` 不等 = **两端真漂移（须改代码）**。
混为一谈会把「抽取器坏了」误报成「白名单漂移」。

**变异（两处，缺一即不合格）**：
- `M9″`：把 `ALLOWED_DOC` 故意错开一项 ⇒ **字面量腿**必红；
- `M9‴`：把 hint 文案故意错开一项 ⇒ **文案腿**必红。
"""

import json
from pathlib import Path

import pytest

from app.pipeline.parsing import SUPPORTED_FORMATS_HINT, SUPPORTED_SUFFIXES

REPO_ROOT = Path(__file__).resolve().parents[3]      # backend/tests/pipeline/x.py -> 仓库根
APP_TS = REPO_ROOT / "frontend" / "src" / "App.tsx"
DOCS_VIEW = REPO_ROOT / "frontend" / "src" / "components" / "DocsView.tsx"


def _slice_array(src: str, name: str) -> list:
    """锚定切片：取 `name = [` 之后、**与之配对的 `]`** 之前的正文，再 `json.loads`。

    三条必要性（都是本笔实测踩到的）：
    1. 偏移必须落在 `[` **之后**（不是 `name` 之后）—— 数组跨多行时否则会把 `[` 喂进 `json.loads`；
    2. 终止符必须是**配对的** `]` —— 直接 `index("]")` 会命中注释里的 `]`（本笔就踩到）；
    3. **必须先剥注释** —— `ALLOWED_DOC` 上方与行内的中文注释含全角标点，混进 JSON 即解析失败。
       ⚠️ 剥注释是**行级**的（`//` 到行尾），不写自由正则匹配字符串（避免把 `"//"` 里的斜杠也当注释）。
    """
    needle = f"{name} = ["
    i = src.index(needle)
    start = i + len(needle)

    # 括号配对（JSON 数组体里不会有裸 `[` / `]`，但保守起见仍按配对找）
    depth = 1
    j = start
    while j < len(src) and depth:
        if src[j] == "[":
            depth += 1
        elif src[j] == "]":
            depth -= 1
            if depth == 0:
                break
        j += 1
    assert depth == 0, "ALLOWED_DOC 的数组没有配对的 ']'"

    body_lines = []
    for raw in src[start:j].splitlines():
        line = raw.split("//", 1)[0].rstrip()      # 行级剥注释
        if line.strip():
            body_lines.append(line.strip())
    body = "\n".join(body_lines)
    # TS 允许**尾随逗号**，JSON 不允许（K-回归检查 记载的正是这一类前提）。
    # 这里显式剥掉数组体末尾的那个逗号 —— 它是 TS 合法、JSON 非法的**唯一**常见形态。
    body = body.rstrip()
    if body.endswith(","):
        body = body[:-1]
    return json.loads("[" + body + "]")


def _slice_attr(src: str, name: str) -> str:
    """取 `name="…"` 的属性值（返回 **str**，调用方自己按分隔符切）。"""
    i = src.index(f'{name}="')
    j = src.index('"', i + len(name) + 2)
    return src[i + len(name) + 2 : j]


def _hint_tokens(text: str) -> set:
    """把「支持 a / b / …，单文件 ≤ …」整句或裸 token 串都归一成后缀集合。

    ⚠️ 前端该常量是**多行字符串拼接**（`"a" +\n  "b"`）⇒ 必须先剥掉 TS 的引号与加号/换行，
    否则 token 里会混入 `" +\n  "` 这类残渣（本笔实测踩到）。
    剥法 = 行级：去掉行首行尾的 `+`、再去掉所有 `"`，最后把换行折成空格。
    """
    if "支持 " in text:
        text = text.split("支持 ", 1)[1]
    if "，" in text:
        text = text.split("，", 1)[0]
    cleaned = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.endswith("+"):
            line = line[:-1].rstrip()
        if line.startswith("+"):
            line = line[1:].lstrip()
        cleaned.append(line.replace('"', "").rstrip(";"))
    text = " ".join(cleaned)
    parts = [p.strip() for p in text.split("/")]
    # 🔴 **终局审计 A-10（本版就地修）**：原先用 `part.isalnum()` 过滤 —— 那会**静默丢弃**
    # 含非字母数字的后缀（.7z / .c++ / .h5 之类），使判据**假红**（方向安全，但会误报）。
    # 改为**显式判定**：合法 token = 可选点开头 + 字母数字与 . + _ -，且首字符是字母或数字；
    # 不合法的一律**显式报错**（**不得静默丢弃** —— 静默丢弃正是 A-10 的病根）。
    tokens = set()
    for part in parts:
        if not part:
            continue
        candidate = part.lstrip(".")
        ok = bool(candidate) and candidate[0].isalnum() and all(
            ch.isalnum() or ch in ".+_-" for ch in candidate
        )
        if not ok:
            raise AssertionError(
                "A13b 抽取失败：hint 里出现无法识别的 token " + repr(part)
                + "（既不是后缀也不是已知分隔符）—— 抽取器需更新，**不得静默丢弃**。"
            )
        tokens.add(f".{candidate}")
    return tokens


def _app_src() -> str:
    return APP_TS.read_text(encoding="utf-8")


def _docs_src() -> str:
    return DOCS_VIEW.read_text(encoding="utf-8")


def test_slice_array_raises_distinctly_on_non_json():
    """K-回归检查：抽取器失效必须与「值不等」**分开**（这里证明抽取器真会抛）。"""
    with pytest.raises(json.JSONDecodeError):
        _slice_array('const X = [1, 2, /* c */];', "const X")


def test_frontend_allowed_doc_equals_backend_whitelist():
    """**A13b 字面量腿**：`ALLOWED_DOC` == `SUPPORTED_SUFFIXES`（定档 44，断言不写死 15）。"""
    try:
        items = _slice_array(_app_src(), "const ALLOWED_DOC")
    except json.JSONDecodeError as exc:
        raise AssertionError(
            "A13b 抽取失败：ALLOWED_DOC 已不再是纯字符串字面量（需改用支持 TS 的抽法），"
            f"原始错误 = {exc}"
        ) from None
    assert set(items) == set(SUPPORTED_SUFFIXES), sorted(set(items) ^ set(SUPPORTED_SUFFIXES))
    assert len(items) == 44


def test_frontend_accept_equals_backend_whitelist():
    """**A13b 字面量腿（第二处）**：`accept="…"` 与白名单等价（**两份独立字面量**）。"""
    accept = _slice_attr(_docs_src(), "accept")
    tokens = {s.strip() for s in accept.split(",") if s.strip()}
    assert tokens == set(SUPPORTED_SUFFIXES), sorted(tokens ^ set(SUPPORTED_SUFFIXES))
    assert len(tokens) == 44


def test_frontend_hint_text_matches_backend_hint():
    """**A13b 文案腿**：前端整句 == 后端 hint（**43 token = 44 − `.markdown`**）。

    ⚠️ 直接对整句 `split("/")` 会得到「webp，单文件 ≤ 20MB」⇒ **开工前就是假红**。
    必须先切掉「支持 」与「，单文件」（`_hint_tokens` 做的正是这件事）。
    """
    src = _docs_src()
    # 锚在**常量定义**上，不锚「支持 」——后者会一路吃到下一个含「，单文件」的地方，
    # 把后面的注释也圈进来（本笔实测踩到）。
    i = src.index("const DOC_FORMAT_HINT =")
    j = src.index(";", i)
    assert _hint_tokens(src[i:j]) == set(SUPPORTED_SUFFIXES) - {".markdown"}
    assert _hint_tokens(SUPPORTED_FORMATS_HINT) == set(SUPPORTED_SUFFIXES) - {".markdown"}
    assert len(_hint_tokens(SUPPORTED_FORMATS_HINT)) == 43


def test_accept_and_allowed_doc_are_independent_literals():
    """两份字面量**必须各自独立存在**（不是一处引用另一处）—— 否则下面那条「人工对齐」纪律失效。

    没有这条，把 `accept` 改成从 `ALLOWED_DOC` 派生也不会红，而本仓口径要求它们是同源两份。
    """
    assert "ALLOWED_DOC" not in _docs_src()
    assert "accept=" in _docs_src()


def test_docs_view_hint_constant_exists_and_is_the_literal():
    """文案腿的载体：`DOC_FORMAT_HINT` 常量必须真的出现在 JSX 里（防「常量加了但没人用」）。"""
    src = _docs_src()
    assert "const DOC_FORMAT_HINT =" in src
    assert "{DOC_FORMAT_HINT}" in src


def test_frontend_has_no_fifth_whitelist_site():
    """实测**无第 5 处**（包内 §2.1 逐字）：聊天图片面 `Composer.tsx` 的 `accept="image/*"` 不属本阶段。

    全 `frontend/src` 扫「同时含 5 个以上已知后缀」的地方，必须恰好是那两处。
    """
    src_root = REPO_ROOT / "frontend" / "src"
    probes = [".docx", ".xlsx", ".pptx", ".tiff", ".webp"]
    sites = []
    for path in src_root.rglob("*.ts*"):
        text = path.read_text(encoding="utf-8")
        # 按**语句**扫（不是按行）—— 数组字面量跨多行，逐行扫会漏。
        # 判据：该片段的**非空白字符**里同时含这 5 个探针后缀。
        for chunk in text.split(";"):
            if all(p in chunk for p in probes):
                sites.append(path.name)
    # 恰好两处：App.tsx 的 ALLOWED_DOC 与 DocsView.tsx 的 accept=
    # （⚠️ DocsView 的 DOC_FORMAT_HINT 用的是**去点**形态 py / js，不含 .docx ⇒ 不会被计入）
    assert sorted(set(sites)) == ["App.tsx", "DocsView.tsx"], sites
