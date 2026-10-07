"""`回归检查` · **B3 缓存不陈旧（写完清缓存）** 的判据载体 —— 工具升级-3 · 阶段 4 · 语料一致性轨；
**阶段 S3 · 条目 `157` 起本件同笔改写为「接线后判据」**（接线已落地）。

判据 = **`A11-b`**（开工包 §1 判据表 / `D-回归检查` 定档取(甲)），**四面**：

| 面 | 断言 | 变异靶子 |
|---|---|---|
| **① 效果腿** | 调**产品清缓存入口** ⇒ 同一查询**从命中变为未命中** | **`M-回归检查`**（入口改成空操作） |
| **② 接线面 · 静态** | 生产调用点**恰为 13 处 / 4 个文件**（裸名 `ast.Name` 形态，含 U2 构建发布/恢复/回滚） | **`M-回归检查`**（漏接 / 属性式 `_cache_store.invalidate_all(...)` 规避） |
| **③ 接线面 · 行为** | 八条写路径（导入 / 重建 / 上传 / 文档软删 / 笔记元数据 / 笔记软删 / 恢复 / 彻底删除）**必须**清；清后同题**必须重跑**并拿到**新答案** | **`M-回归检查`**（删掉某条写路径的调用） |
| **④ 反向腿** | **读路径不得**清缓存（`GET /api/agent/audit` 代理观察点） | **`M-回归检查`**（越权接到读路径） |

## 🔴 本笔（`157`）改写了什么 —— 逐腿留痕（⛔ 不得静默）

| 原（阶段 4） | 现（阶段 S3 · 条目 `157`） | 为什么 |
|---|---|---|
| 静态腿「生产调用点 = **0**」 | 静态腿「= **9 处 / 3 文件**」 | 接线落地 ⇒ 原判据**必然变红**；改为**钉住接线面**（AST 只认 `ast.Name` ⇒ 属性式会让计数掉下去，规避当场打红） |
| 行为腿「导入 / 重建**不得**清」 | 行为腿「导入 / 重建 / 上传 / 文档软删**必须**清」+ 新增 笔记元数据 / 笔记软删→恢复 / 彻底删除 三条 | 拍板 **`#6` = A**（正确性优先）：新增材料 / 元数据改动 / 恢复 / 彻底删除**全失效** |
| 效果腿「关态也清 + 返回已清条数」（例 1 / 例 2） | **原样保留** | 判别力仍在 ⇒ 作**反向保护**（不许退回「早退 / 不返回条数」） |
| 红线 9 禁语扫描「接线前不得声称已接线」 | 反转为「**不得再留「无生产调用点 / 接线移交阶段 5」这类与落地矛盾的陈旧声明**」 | 声明必须与落地一致；陈旧声明正是读者会信的载体 |

⚠️ **版本承接**：U6 生产写回使用完整 `source:note:<id>` 来源快照，包含标题 / 分类 / 标签；
   被软删或硬删的来源无法捕获快照，本轮不写缓存。恢复 / 彻底删除仍必须全失效。

## ⚠️ 效果腿的空靶防线（**必读**，开工包 `D-回归检查` / `§2` `回归检查`）

`tests/api/conftest.py:164-190` 的 ``pin_semantic_cache_off`` 是 **autouse** 钉子：**每个用例都调
`reset_cache()`** ⇒ 用例间缓存**恒空**；且 ``_STORE`` 是**进程内 dict** ⇒ 若只断「用完就是空的」，
**恒真**（空靶、假绿）。
⇒ 本件的效果腿**自建「先命中、后清空」两态**：先真跑一轮 ``/api/chat`` 写出条目 ⇒ 断言
   ``cache_size() == 1`` **且**第二轮**真命中**（``cache_size()` 变 0 **不可能**由「本来就没写进去」
   蒙过）⇒ 再调产品入口 ⇒ 第三轮**变未命中**。
   （同型教训：`开发约定` §13-⑤ 之①「观察量必须对被测决策敏感」。）

⚠️ 本件**不依赖** `回归检查`（`app/pipeline/clean_status.py`）的任何新函数 —— 只依赖 `app/cache/**`
   与既有产品路径。

## 🔴 已删的「空靶例」登记（审计发现，如实留痕）

本件原有 `test_no_parse_sse_regression_placeholder`，**实为空靶**：
`parse_sse("event: token\ndata: {}\n\n")` 恒返回 `[]`（`for block in raw.split("\n\n")` 后两块都不以
`data: ` 开头 ⇒ 全被跳过）⇒ `assert isinstance([], list)` **恒真**，**把 `parse_sse` 改成什么都解不出来
也照样绿** ⇒ 对「SSE 解析回归」**零判别力**（违反 §4 硬纪律①「观察量必须对被测决策敏感」）。
**真实动机（auditor-1 的建议 · 必须写明）**：它是**为让契约套件导入的 `parse_sse` 不触发 lint 未使用**
而加的**凑数例** —— 即把 **lint 便利凌驾于判定力之上**。这正是本案值得留痕的原因：
一个「为了让 linter 闭嘴」而写的用例，会以「**有判据**」的外观混进证据面。

⇒ **已删**（它对本件任何腿零贡献，留着只虚增例数）；`parse_sse` 的形状校对付诸**契约套件**
（其真身在 `tests/api/test_chat_contract.py`，本件不再借用 —— 随之清理了 `:34` 的 import）。

⚠️ **例数口径（不得留不可复现的数字）**：删前本件**曾报过 9**（更早时点读数，**已不可复现**）；
**Lead 实测 `--collect-only` 与本件 AST 自查一致 = 删后 8**。⇒ 本登记**不写「9 → 8」**
（那会留下一个复现不出来的数字，见 `K-回归检查`）。

⚠️ **`157` 同笔改写后的权威读数 = 13 例** = 原 8 例**全部保留**（4 例原样 + 4 例语义反转改写）
**+ 新增 5 例**（上传要清 / 文档软删要清 / 笔记元数据要清 / 笔记软删→恢复要清 / 彻底删除要清）。
⇒ **逐例可解释、可 `--collect-only` 复现**；本条不写「零回归」类总体断言。
"""

from __future__ import annotations

import pytest

from app.api import chat_routes
from app.cache import store as cache_store
from tests.api.test_chat_contract import _StubClassifier, client  # noqa: F401
from tests.retrieval.conftest import seed_document

QUESTION = "向量库用什么存储？"
MATERIAL_CHUNK = "向量库使用 Qdrant 内嵌模式，向量化由上游接口完成。"
MATERIAL_FILENAME = "缓存失效手册.md"
ANSWER = "据资料 [1]，向量库用 Qdrant 内嵌模式。"
ANSWER_FRESH = "据资料 [1]，向量库用 Qdrant 内嵌模式（重跑之后的新答案）。"

# `157` 接线面：裸名 `invalidate_all(...)` 的生产调用点（文件 ⇒ 处数）。
# ⛔ 属性式 `_cache_store.invalidate_all(...)` 不会被 `ast.Name` 捕获 ⇒ 计数掉下去 ⇒ 静态腿打红。
EXPECTED_CALL_SITES = {
    "app/session/note_service.py": 3,
    "app/api/document_routes.py": 4,
    "app/api/trash_routes.py": 2,
    "app/pipeline/builds.py": 4,
}

NOTE_TITLE = "缓存失效笔记.md"
NOTE_TITLE_NEW = "缓存失效笔记（改过标题）.md"
NOTE_CATEGORY_NEW = "改过的分类"


class _FakeStream:
    """最小流：把整段回答当**一个** ``text_delta`` 发（与 CubeLoop 的事件形状一致）。"""

    def __init__(self, parts: list[str]) -> None:
        self._parts = parts

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for part in self._parts:
            yield _ns(type="text_delta", delta=part)


def _ns(**kwargs):
    from types import SimpleNamespace

    return SimpleNamespace(**kwargs)


class _CountingModel:
    """假 bound model：**记录被调用次数**（「命中轮不再调模型」就断在它身上）。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []
        self.spec = _ns(id="cache-change-fake", kind="chat", provider="fake",
                        base_url="http://127.0.0.1/cache-fixture", model="cache-change-fake")

    async def generate(self, messages, system_prompt=None, max_output_tokens=None, **extra):  # noqa: ANN001
        self.calls.append({"mode": "generate", "system_prompt": system_prompt})
        return _ns(content=[_ns(type="text", text=self.reply)], stop_reason="end_turn")

    async def stream(self, messages, system_prompt=None, **extra):  # noqa: ANN001
        self.calls.append({"mode": "stream", "system_prompt": system_prompt})
        return _FakeStream([self.reply])

    @property
    def count(self) -> int:
        return len(self.calls)


class _RetrievalCounter:
    """``chat_routes.retrieve_materials`` 的计数包装（观察「检索有没有重跑」）。"""

    def __init__(self, materials, text: str) -> None:
        self.materials = materials
        self.text = text
        self.calls: list[dict] = []

    async def __call__(self, db, text, filters=None, *, stats=None):  # noqa: ANN001
        self.calls.append({"text": text, "filters": filters})
        if stats is not None:
            stats.status = "no_candidates" if not self.materials else "disabled"
            stats.candidates = len(self.materials)
        return list(self.materials), self.text, None

    @property
    def count(self) -> int:
        return len(self.calls)


def _install(monkeypatch, reply: str = ANSWER) -> _CountingModel:
    """装模型 + 路由分类器（不连真上游）。"""
    model = _CountingModel(reply)
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db")
    )
    return model


@pytest.fixture
def cache_on(monkeypatch):
    """开态 + 独立清表（autouse 钉子把默认态钉成关，这里显式打开，照 5.2 先例）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", True)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_TTL_SECONDS", 86400)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_MAX_ENTRIES", 256)
    cache_store.reset_cache()
    cache_store.reset_failure_counts()
    yield settings
    cache_store.reset_cache()
    cache_store.reset_failure_counts()


async def _materials_from(db, texts=(MATERIAL_CHUNK,), filename=MATERIAL_FILENAME):
    """造真语料并返回 (材料快照, 渲染正文) —— 材料用**真 chunk_id**，让存活校验真的过。"""
    from sqlalchemy import select

    from app.models import Chunk
    from app.retrieval.types import Material, number, render_materials

    document = await seed_document(db, list(texts), filename=filename)
    rows = (
        (await db.execute(select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.id)))
        .scalars()
        .all()
    )
    materials = [
        Material(
            chunk_id=row.id,
            document_id=document.id,
            content=row.content,
            filename=filename,
            kb_id=1,
            block_ordinal=row.ordinal,
            keyword_score=0.9,
        )
        for row in rows
    ]
    number(materials)
    return materials, render_materials(materials)


async def _flush_cache_writes() -> None:
    """等缓存写回的后台任务落定（fire-and-forget ⇒ 用例必须自己等）。"""
    await chat_routes.drain_cache_tasks()


async def _run_chat(client) -> str:  # noqa: ANN001
    """跑一轮 ``/api/chat``（未流式）并返回正文 —— 各行为腿共用的观察入口。"""
    resp = await client.post("/api/chat", json={"message": QUESTION})
    assert resp.status_code == 200, resp.text
    return resp.json()["message"]


def _only_entry():
    """取当前**唯一**缓存条目（先断条数 ⇒ 防「测错条」/「压根没写进去」两种空靶）。"""
    assert cache_store.cache_size() == 1, (
        "前置：本条得先有一条**真写回**的条目（写侧没落地的话下面全是空靶）"
    )
    return next(iter(cache_store._STORE.values()))


async def _note_materials(db, *, title: str = NOTE_TITLE, category: str = "原始分类"):
    """造**真笔记行** + 对应材料快照（真 ``note_id`` ⇒ 写侧落完整 ``source:note:<id>`` 指纹）。

    形状照 ``app/retrieval/keyword.py`` 的笔记材料：``chunk_id`` 取 ``note_chunk_id``（负数），
    ``document_id=0``、``filename`` = 标题。
    """
    from app.models import Note
    from app.retrieval.types import Material, note_chunk_id, number, render_materials

    note = Note(title=title, content=MATERIAL_CHUNK, category=category, kb_id=1)
    db.add(note)
    await db.commit()
    await db.refresh(note)
    materials = [
        Material(
            chunk_id=note_chunk_id(int(note.id)),
            document_id=0,
            content=MATERIAL_CHUNK,
            filename=title,
            kb_id=1,
            block_ordinal=0,
            keyword_score=0.9,
            note_id=int(note.id),
        )
    ]
    number(materials)
    return note, materials, render_materials(materials)


async def _fresh_answer_is_served(client, model, first: str) -> str:  # noqa: ANN001
    """换一个「新答案」再问同一题：**若仍回旧答案，本函数必红**（判别点集中在这里）。

    ``first`` = 清缓存前那一轮的正文（各腿自己传进来；不设模块级寄存位，免得用例间串味）。
    """
    before = model.count
    model.reply = ANSWER_FRESH
    third = await _run_chat(client)
    assert model.count == before + 1, "清缓存之后未重跑模型 ⇒ 回的仍是旧答案"
    assert third != first, "清缓存之后又拿到旧答案（缓存没真清或又写回了）"
    return third
def _cache_call_sites() -> list[str]:
    """静态扫描：``backend/app/`` 内 ``invalidate_all`` 的**生产调用点**（返回 ``文件:行号: 行``）。

    🔴 **判法写死 = AST 结构化判定**（不是「命中数 == 0」，也**不是**按行外形猜）：

- **证据取 `ast.Name`**：`invalidate_all(...)` 必然产生一个 `Name` 节点 ⇒ 节点存在即**调用点**；
- **豁免面只有四类**（全部由 AST 结构判定，不看行外形）：① **真 docstring**（模块 / 类 / 函数体的
  **首条**表达式语句，且其值是非空字符串）；② `def invalidate_all` 的**定义行**；③ `import` 的
  **别名行**（`from app.cache.store import (… invalidate_all, …)` 与 `app/cache/__init__.py` 的再导出块）；
  ④ **`__all__` 列表字面量内**的成员行。
    - ⇒ **阶段 S3 · 条目 `157` 起**：接线落地 ⇒ 真仓库上**必须**返回 `EXPECTED_CALL_SITES`
      （13 处 / 4 个文件，含 U2 构建发布/恢复/回滚）；`scanned > 50` 仍是**空靶自检**。

⚠️ **为什么不用「按行外形豁免」**（如 `line.startswith('def invalidate_all(')` / `line in {'"invalidate_all",'}`）：
那种判法**同时**有两个洞 —— 对 `app/cache/__init__.py` 的**文档表行**与 `    invalidate_all,`
（无引号）会**误判成调用点（恒红）**；而对「同一行内既含字符串常量、又含真调用」的接线会**漏放（假绿）**。

    🔴 **本静态腿曾有一版按行外形豁免，实测对 `M-回归检查` 返回 `[]`（零判别力）**：
    `invalidate_all("x")` 的**实参也是字符串常量**，且 `ast.Name` 与常量**同一行号** ⇒
    按行豁免会把名字**连带**放走（同型：`app/cache/__init__.py:18` 的文档表行恒红、`:54` 的
    `    invalidate_all,` 恒红）。
    **现改为 AST 结构化判定**（豁免面 = 真 docstring / 定义行 / import 别名行 / `__all__` 成员），
    并把 `scanned > 50` 作为**空靶自检**。
    本用例直接遍历公开源码并验证导入结构，不依赖额外脚本或日志。
    """
    import ast
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[2] / "app"
    violations: list[str] = []
    scanned = 0
    for path in sorted(app_dir.rglob("*.py")):
        scanned += 1
        source = path.read_text(encoding="utf-8")
        if "invalidate_all" not in source:
            continue
        relative = path.relative_to(app_dir.parent).as_posix()
        tree = ast.parse(source)  # 语法错会当场抛 ⇒ 扫描面本身不会被静默跳过

        # 🔴 豁免一律**按节点身份**（`id(node)`），**不按行号** —— 按行号有一个实测可复现的洞：
        #    `__all__ = ["invalidate_all"]; invalidate_all("z")`（同一行既在 __all__ 内、又是真调用）
        #    会被「该行在 __all__ 里 ⇒ 豁免」放走。按节点身份则只有那个 **Constant 节点**被豁免，
        #    同一行的 `ast.Name`（真调用）**照样被扫出来**。
        exempt_nodes: set[int] = set()

        def _is_docstring(node) -> bool:  # noqa: ANN001
            """该节点是否**是**某定义的首条表达式语句（= docstring）。

            只有**真 docstring** 才豁免 —— 模块 docstring、类 docstring、函数 docstring。
            ⚠️ 不豁免「任意字符串常量」：那会把 `f("invalidate_all")` 里的实参阅掉。
            """
            return (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            )

        for parent in ast.walk(tree):
            body = getattr(parent, "body", None)
            if isinstance(body, list) and body and _is_docstring(body[0]):
                exempt_nodes.add(id(body[0]))
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if parent.name == "invalidate_all":
                    exempt_nodes.add(id(parent))
            if isinstance(parent, ast.alias) and "invalidate_all" in (parent.name, parent.asname):
                exempt_nodes.add(id(parent))

        # ② `__all__` 的成员：只豁免**列表里那个常量节点本身**（不豁免整行）。
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
            ):
                for element in ast.walk(node.value):
                    if isinstance(element, ast.Constant) and element.value == "invalidate_all":
                        exempt_nodes.add(id(element))

        # ③ 真正算证据的是**名字引用**（`ast.Name`）：`invalidate_all(...)` 必然产生一个 Name 节点。
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "invalidate_all":
                if id(node) in exempt_nodes:
                    continue
                violations.append(f"{relative}:{node.lineno}: invalidate_all")

    assert scanned > 50, f"静态扫描只看到 {scanned} 个文件 ⇒ 路径探针本身失效了（空靶）"
    return violations


# ---------------------------------------------------------------------------
# 腿① · 效果腿：调产品入口 ⇒ 同一查询从**命中**变**未命中**
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a11b_leg1_product_entry_turns_a_hit_into_a_miss(
    client, db, cache_on, monkeypatch
):
    """**`A11-b` 腿①**：自建「先命中、后清空」两态 ⇒ 清缓存**确有效果**。

    三态链条（每一条都是**必要**的，缺一条就会退化成空靶）：

    1. 第一轮（未命中）跑完 ⇒ ``cache_size() == 1``（**证明条目真的写进去了**）；
    2. 第二轮同问 ⇒ ``hit`` 计数 +1 **且**检索与模型**都没再跑**（**证明它真的是命中**）；
    3. 调产品入口 ``app.cache.invalidate_all("…")`` ⇒ 返回**已清条数 == 1**、``cache_size() == 0``；
    4. 第三轮同问 ⇒ **变未命中**：检索与模型各**再跑一次**（``hit`` 计数不再增长）。

    ⇒ **`M-回归检查`（把入口改成空操作 / 不再清）** 会在第 4 步变红（第三轮仍命中、检索与模型不重跑）。
    """
    from app.cache import invalidate_all as product_invalidate  # 走**包级再导出**的符号

    materials, text = await _materials_from(db)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    model = _install(monkeypatch)

    # —— 态 0：起点空（autouse 钉子留下的状态；**本件不依赖它**，只作链条第一环的可读性）
    assert cache_store.cache_size() == 0, "起点应当是空表（清干净才能分辨后面两次写入）"

    # —— 态 1：第一轮 = 未命中 ⇒ 真写回一条
    first = await client.post("/api/chat", json={"message": QUESTION})
    assert first.status_code == 200, first.text
    await _flush_cache_writes()
    assert cache_store.cache_size() == 1, "第一轮没写回缓存 ⇒ 本用例后面什么都没测到"
    assert counter.count == 1 and model.count == 1, "未命中那轮该各跑一次"
    hits_before = cache_store.failure_counts()["hit"]

    # —— 态 2：第二轮 = **真命中**（检索与模型都不再跑）
    second = await client.post("/api/chat", json={"message": QUESTION})
    assert second.status_code == 200, second.text
    assert cache_store.failure_counts()["hit"] == hits_before + 1, (
        "第二轮**没有命中** ⇒ 「先命中」这一态没建立起来，本用例无从取证"
    )
    assert counter.count == 1, "命中轮不该再检索"
    assert model.count == 1, "命中轮不该再调模型"
    assert second.json()["message"] == first.json()["message"]

    # —— 态 3：调**产品入口**（包级再导出）⇒ 条目**确被清掉**，且返回清掉的条数
    cleared = product_invalidate("t3_4_leg1_probe")
    assert cleared == 1, f"产品入口该报「清掉 1 条」，实际 {cleared}"
    assert cache_store.cache_size() == 0, "产品入口没真清（`M-T4-3` 空操作变异会死在这里）"

    # —— 态 4：同一查询 ⇒ 从**命中**变**未命中**（检索与模型各再跑一次）
    third = await client.post("/api/chat", json={"message": QUESTION})
    assert third.status_code == 200, third.text
    assert cache_store.failure_counts()["hit"] == hits_before + 1, (
        "清缓存之后同一查询**仍命中** ⇒ 产品入口没有效果（`M-T4-3`）"
    )
    assert counter.count == 2, "清缓存之后必须重跑检索（否则仍是旧答案）"
    assert model.count == 2, "清缓存之后必须重跑模型（否则仍是旧答案）"
    assert third.json()["message"] == first.json()["message"], "重跑拿到的正文应当一致"


@pytest.mark.asyncio
async def test_a11b_leg1_returns_cleared_count_and_exports_symbol(client):
    """**`A11-b` 腿①** 的形状面：包级再导出可用、返回**真条数**、**关态也不早退**。

    - ``app.cache.invalidate_all`` 与 ``app.cache.store.invalidate_all`` 是**同一个**函数对象；
    - **关态**（autouse 钉子把 `SEMANTIC_CACHE_ENABLE` 钉成 `False`）下直接塞两条条目：
      清缓存入口**照样清得动**（不许早退 —— 否则关态下该动作无从取证）。

    🔴 **刻意不请求 `cache_on` 夹具** —— 那个夹具会把开关**打开**，与「关态也不早退」
    这条腿的**被测前提相反**（早前版本误加了它，预检当场打红）。
    """
    from app.cache import invalidate_all as exported
    from app.cache import store as store_mod

    assert exported is store_mod.invalidate_all, "包级再导出断裂（`from app.cache import …` 拿不到产品入口）"

    # 关态：直接塞条目（不经过产品路径，`store_snapshot` 在关态会早退）
    from app.cache.entry import CacheEntry

    # ⚠️ 字段名照 `app/cache/entry.py:291-329` 的 dataclass **真实定义**（无 `answer`，正文叫 `raw_answer`）
    monkey = CacheEntry(
        question=QUESTION,
        materials=[],
        materials_text="",
        raw_answer=ANSWER,
        retrieval_stats={},
    )
    store_mod._STORE["t3_4_probe_a"] = monkey
    store_mod._STORE["t3_4_probe_b"] = monkey
    assert store_mod.cache_size() == 2
    assert cache_store.enabled() is False, "本夹具应当把开关钉成关（autouse 钉子）"

    assert exported("t3_4_leg1_disabled_probe") == 2, "关态下产品入口早退了（该动作在关态也得能清）"
    assert store_mod.cache_size() == 0


# ---------------------------------------------------------------------------
# 腿② · 接线面（**接线后判据**）：静态钉住 13 处调用点 + 写路径行为逐条**必须**清
# ---------------------------------------------------------------------------


def test_a11b_leg2_static_wiring_is_pinned_and_not_evaded():
    """**`A11-b` 腿②(a) 静态查（接线后判据）**：``backend/app/**`` 内 ``invalidate_all`` 的
    生产调用点 = **13 处 / 4 个文件**（笔记域 3 / 文档域 4 / 回收站域 2 / 构建域 4）。

    - **漏接**（删掉某条写路径的调用）⇒ 该文件计数掉下去 ⇒ 本腿变红；
    - **属性式规避**（``_cache_store.invalidate_all(...)``）⇒ AST 只认 ``ast.Name``
      ⇒ 计数同样掉下去 ⇒ 本腿变红（这正是「裸名 + 显式 import」写进落库口径的原因）；
    - **时序**（调用点排在写提交之后）由行为腿（例 4–例 11）正面取证，静态腿不做时序推断。
    """
    violations = _cache_call_sites()
    grouped: dict[str, int] = {}
    for item in violations:
        file_part = item.split(":", 1)[0]
        grouped[file_part] = grouped.get(file_part, 0) + 1
    assert grouped == EXPECTED_CALL_SITES, (
        "生产调用点与接线面不符（漏接 / 多接 / 属性式规避都会走到这里）：\n"
        f"  期望 = {EXPECTED_CALL_SITES}\n  实测 = {grouped}\n" + "\n".join(violations)
    )
    assert len(violations) == sum(EXPECTED_CALL_SITES.values()) == 13


@pytest.mark.asyncio
async def test_a11b_leg2_ingest_path_clears_cache(
    client, db, cache_on, monkeypatch, tmp_path
):
    """**`A11-b` 腿②(b) 行为查 · 入库路径**：``POST /api/documents/import-file`` **必须**清缓存。

    观察量 = **先命中、后清空、再换答案**：先真跑一轮写出条目（``cache_size() == 1``）⇒
    走真入库 ⇒ 条目**必须没了**（``== 0``）⇒ 换一个新答案再问同题 ⇒ **不得**拿到旧答案。

    ⚠️ 上传落档目录重定向到 ``tmp_path``（照 `test_documents_contract.py:34-38` 先例）。
    """
    from app.core.config import settings

    monkeypatch.setattr(type(settings), "upload_path", property(lambda self: tmp_path))

    materials, text = await _materials_from(db)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    model = _install(monkeypatch)

    first = await _run_chat(client)
    await _flush_cache_writes()
    _only_entry()

    source = tmp_path / "入库路径探针.md"
    source.write_text("# 探针\n\n这是入库路径要清缓存的观察样本，正文足够长以切出分块。\n" * 3, encoding="utf-8")
    resp = await client.post("/api/documents/import-file", json={"path": str(source)})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready", resp.text

    assert cache_store.cache_size() == 0, (
        "新增材料（导入本地文件）**必须**全失效 —— 否则同题仍回入库前的旧答案（`157` 判据③）"
    )
    await _fresh_answer_is_served(client, model, first)


@pytest.mark.asyncio
async def test_a11b_leg2_reindex_path_clears_cache(client, db, cache_on, monkeypatch):
    """**`A11-b` 腿②(b) 行为查 · 重建路径**：``POST /api/documents/reindex`` **必须**清缓存。

    理由：重建会**重切块**（chunk_id 与内容都可能变）⇒ 旧条目里的块指纹不再可信。
    """
    materials, text = await _materials_from(db)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    model = _install(monkeypatch)

    first = await _run_chat(client)
    await _flush_cache_writes()
    _only_entry()

    resp = await client.post("/api/documents/reindex", json={})
    assert resp.status_code == 200, resp.text

    assert cache_store.cache_size() == 0, (
        "重建索引**必须**全失效（重切块 ⇒ 旧块指纹已不可信）—— `157` 判据③"
    )
    await _fresh_answer_is_served(client, model, first)


@pytest.mark.asyncio
async def test_a11b_leg2_export_path_does_not_clear_cache(client, db, cache_on, monkeypatch):
    """**`A11-b` 腿②(b) 行为查 · 导出路径**：``GET /api/agent/audit`` **不得**清缓存。

    ⚠️ **一条如实登记**：本仓**没有**「导出」端点（`app/api/**` 内零 `/export` 路由；
    `backend/scripts/export_agent_trace.py` 是**离线脚本**、不参与请求链路）。⇒ 行为腿取**API 面**
    最接近的读取端 ``GET /api/agent/audit`` 作**代理观察点**（它是「把数据往外吐」的那条路径）。
    真『导出』= 离线脚本 ``backend/scripts/export_agent_trace.py``（**不参与请求链路**），故行为腿以
    ``GET /api/agent/audit`` 为代理点；**静态腿已对 `backend/app/**` 全量取证**（含将来任何导出路径）。
    **它不改变结论的强度**：静态腿（上一条用例）已对**全部** ``backend/app/`` 源码取证，
    含任何未来的导出路径。
    """
    materials, text = await _materials_from(db)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    _install(monkeypatch)

    await client.post("/api/chat", json={"message": QUESTION})
    await _flush_cache_writes()
    assert cache_store.cache_size() == 1, "前置：本条得先有一条**真命中过**的条目"

    resp = await client.get("/api/agent/audit", params={"limit": 5})
    assert resp.status_code == 200, resp.text

    assert cache_store.cache_size() == 1, (
        "**读取/导出路径**把缓存清了 ⇒ 越权接线（`M-T4-6`）：读路径不得进失效面"
    )


# ---------------------------------------------------------------------------
# 腿③ · 笔记域 / 上传 / 文档软删 / 回收站：**判据 ②④⑤** 的行为腿（同题不得回旧答案）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a11b_leg2_upload_path_clears_cache(client, db, cache_on, monkeypatch, tmp_path):
    """**`A11-b` 腿②(b) 行为查 · 上传路径**：``POST /api/documents/upload`` **必须**清缓存。"""
    from app.core.config import settings

    monkeypatch.setattr(type(settings), "upload_path", property(lambda self: tmp_path))

    materials, text = await _materials_from(db)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    model = _install(monkeypatch)

    first = await _run_chat(client)
    await _flush_cache_writes()
    _only_entry()

    resp = await client.post(
        "/api/documents/upload",
        files={
            "file": (
                "上传路径探针.md",
                "# 上传\n\n上传也是新增材料，正文足够长以切出分块。\n" * 3,
                "text/markdown",
            )
        },
    )
    assert resp.status_code == 200, resp.text
    assert cache_store.cache_size() == 0, "上传入库**必须**全失效（`157` 判据③）"
    await _fresh_answer_is_served(client, model, first)


@pytest.mark.asyncio
async def test_a11b_leg2_document_soft_delete_clears_cache(client, db, cache_on, monkeypatch):
    """**`A11-b` 腿②(b) 行为查 · 文档软删**：``DELETE /api/documents/{id}`` **必须**清缓存。"""
    materials, text = await _materials_from(db)
    document_id = int(materials[0].document_id)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    model = _install(monkeypatch)

    first = await _run_chat(client)
    await _flush_cache_writes()
    _only_entry()

    resp = await client.delete(f"/api/documents/{document_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True, resp.text
    assert cache_store.cache_size() == 0, "文档软删**必须**全失效（软删后该文档不再可检索）"
    await _fresh_answer_is_served(client, model, first)


@pytest.mark.asyncio
async def test_a11b_leg3_note_metadata_change_is_not_stale(client, db, cache_on, monkeypatch):
    """**判据 ②**：改标题 / 改分类（**正文一字不动**）⇒ 同题**不得**回旧答案。

    先自检缓存确实捕获 U6 完整笔记来源快照，且同题能真命中，再走产品路径改元数据。
    """
    note, materials, text = await _note_materials(db)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    model = _install(monkeypatch)

    first = await _run_chat(client)
    await _flush_cache_writes()
    entry = _only_entry()
    assert f"source:note:{int(note.id)}" in entry.fingerprints, (
        "空靶自检：缓存条目里没有这条笔记的指纹 ⇒ 本腿测不到「元数据盲区」"
    )

    hits = cache_store.failure_counts()["hit"]
    second = await _run_chat(client)
    assert cache_store.failure_counts()["hit"] == hits + 1, (
        "第二轮该真命中（反向保护①：**内容未变**时仍要命中，⛔ 不许靠「一律不命中」蒙）"
    )
    assert second == first and model.count == 1

    resp = await client.put(
        f"/api/notes/{int(note.id)}",
        json={"title": NOTE_TITLE_NEW, "category": NOTE_CATEGORY_NEW},
    )
    assert resp.status_code == 200, resp.text
    assert cache_store.cache_size() == 0, "笔记元数据改动**必须**全失效（拍板 #6 = A）"
    await _fresh_answer_is_served(client, model, first)


@pytest.mark.asyncio
async def test_a11b_leg3_note_soft_delete_then_restore_is_not_stale(
    client, db, cache_on, monkeypatch
):
    """**判据 ④**：软删 → 恢复 ⇒ 同题**不得**回旧答案（两个动作各清一次，逐次取证）。"""
    note, materials, text = await _note_materials(db)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    model = _install(monkeypatch)

    first = await _run_chat(client)
    await _flush_cache_writes()
    _only_entry()

    resp = await client.delete(f"/api/notes/{int(note.id)}")
    assert resp.status_code == 200, resp.text
    assert cache_store.cache_size() == 0, "笔记软删**必须**全失效"

    # U6 写侧拒绝已删除来源：不能把旧材料写成可复用条目。
    hits = cache_store.failure_counts()["hit"]
    before = model.count
    await _run_chat(client)
    await _flush_cache_writes()
    assert cache_store.cache_size() == 0
    assert cache_store.failure_counts()["hit"] == hits and model.count == before + 1

    # 用仍存活的第二来源建立真条目，检验恢复动作仍全失效。
    other, counter.materials, counter.text = await _note_materials(db, title="仍存活的笔记")
    await _run_chat(client)
    await _flush_cache_writes()
    entry = _only_entry()
    assert f"source:note:{int(other.id)}" in entry.fingerprints
    await _run_chat(client)
    assert cache_store.failure_counts()["hit"] == hits + 1

    resp = await client.post("/api/trash/restore", json={"type": "note", "id": int(note.id)})
    assert resp.status_code == 200, resp.text
    assert cache_store.cache_size() == 0, "回收站恢复**必须**全失效（否则软删期间的旧答案会被复用）"
    counter.materials, counter.text = materials, text
    await _fresh_answer_is_served(client, model, first)


@pytest.mark.asyncio
async def test_a11b_leg3_note_purge_is_not_stale(client, db, cache_on, monkeypatch):
    """**判据 ⑤**：软删 → 彻底删除 ⇒ 同题**不得**回旧答案（``_purge_one`` 是唯一登记处）。"""
    note, materials, text = await _note_materials(db)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    model = _install(monkeypatch)

    first = await _run_chat(client)
    await _flush_cache_writes()
    _only_entry()

    resp = await client.delete(f"/api/notes/{int(note.id)}")
    assert resp.status_code == 200, resp.text
    assert cache_store.cache_size() == 0, "笔记软删必须先清除旧条目"

    hits = cache_store.failure_counts()["hit"]
    before = model.count
    await _run_chat(client)
    await _flush_cache_writes()
    assert cache_store.cache_size() == 0, "已软删笔记不得写成可复用条目"
    assert cache_store.failure_counts()["hit"] == hits and model.count == before + 1

    other, counter.materials, counter.text = await _note_materials(db, title="仍存活的笔记")
    await _run_chat(client)
    await _flush_cache_writes()
    entry = _only_entry()
    assert f"source:note:{int(other.id)}" in entry.fingerprints
    await _run_chat(client)
    assert cache_store.failure_counts()["hit"] == hits + 1

    resp = await client.delete(f"/api/trash/note/{int(note.id)}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True, resp.text
    assert cache_store.cache_size() == 0, "彻底删除**必须**全失效（`_purge_one` 唯一登记处）"
    counter.materials, counter.text = materials, text
    await _fresh_answer_is_served(client, model, first)
    await _flush_cache_writes()
    assert cache_store.cache_size() == 0, "已硬删笔记不得写成可复用条目"
    assert cache_store.failure_counts()["hit"] == hits + 1


# ---------------------------------------------------------------------------
# 接线后的反向自查：``app/cache/**`` 不得再留「尚未接线」类**陈旧声明**
# ---------------------------------------------------------------------------


def test_no_stale_no_call_site_claim_in_cache_package():
    """**语义反转的自查**（原「红线 9」）：接线落地后，不许再留下**与落地矛盾**的陈旧声明。

    - 旧版禁语 = 「已接线 / 不会给旧答案 / B3 已消除 / 写完缓存不会」（**接线前**不许声称已接线）；
    - 现版禁语 = 「无生产调用点 / 接线移交阶段 5 / 只交付**能力 + 判据** / 不会给旧答案」
      —— **接线后**不许再说「还没接线」（陈旧声明正是读者会信的载体，注释与 docstring 同样算）。

    判别力仍在，只是**被禁的方向相反**：`157` 落地时如不同笔更新 ``app/cache/store.py`` 与
    ``app/cache/__init__.py`` 的旧 docstring，本腿当场变红。
    """
    from pathlib import Path

    cache_dir = Path(__file__).resolve().parents[2] / "app" / "cache"
    stale = ("无生产调用点", "接线移交阶段 5", "只交付**能力 + 判据**", "不会给旧答案")
    findings: list[str] = []
    for path in sorted(cache_dir.rglob("*.py")):
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for token in stale:
                if token in raw:
                    findings.append(f"{path.name}:{lineno}: {token} ⇒ {raw.strip()}")
    assert findings == [], "陈旧声明（与「接线已落地」矛盾）：\n" + "\n".join(findings)


@pytest.mark.asyncio
async def test_product_entry_is_not_registered_as_a_model_tool(client, cache_on):
    """**`D-回归检查` v1.2 / 红线 13**：清缓存**不得**成为模型可自主调用的工具。

    观察量 = ``for_model()`` 工具清单（**零新增**）且清单里**没有**取清缓存语义的名字。
    ⚠️ 本阶段 `回归检查` **只落能力**；把它接成工具 = 越过裁决。
    """
    # ⚠️ `for_model` 的真身在 `app/agent/registry.py:81`（`ToolRegistry` 的方法）；
    #    注册表由 `app/agent/bootstrap.py:62` 的 `build_registry()` **每轮**构造
    #    —— **不存在** `app.agent.tools.registry` 这个模块（那是猜的）。
    from app.agent.bootstrap import build_default_registry

    specs = build_default_registry().for_model()
    names = {getattr(item, "name", "") for item in specs}
    assert names, "工具清单为空 ⇒ 本用例的观察量失效（空靶）"
    assert not any("invalidate" in name or "cache" in name.lower() for name in names), (
        f"清缓存语义出现在模型工具清单里：{sorted(names)}"
    )
