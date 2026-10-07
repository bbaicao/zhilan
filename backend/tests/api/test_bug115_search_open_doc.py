"""回归修复（缺陷清单-2 · 阶段五「前端一致性」）：全局搜索「知识库」命中点击不跳对应文档。

根因：``SearchModal`` 的 ``onOpenDoc`` **无参**（``() => void``），chunk 命中的
``document_id`` 在点击路径上被整体丢弃 ⇒ 点哪个知识库命中都只落到 docs 页
**上一次**的状态，永远进不了命中的那篇文档。

修法（两处）：

- ``SearchModal.tsx``：prop 签名改 ``onOpenDoc(documentId: number | null): void``；
  ``FlatHit`` 的 chunk 变体带上 ``document_id: number | null``；flat 映射 chunk 分支带
  ``document_id: hit.document_id ?? null``（缺 id 的命中显式传 null）；两条点击路径
  （键盘回车走 ``openHit`` 的 else 分支 / 鼠标点 chunk 命中行）都把 id 传出去；
- ``App.tsx`` 接线：消费参数——``documentId === null`` 时兜底回旧行为（落 docs 页
  上一次状态），否则 ``openDocument(documentId)`` 跳对应文档。

判据面（本仓无 JS 测试框架 ⇒ 界面面只有**静态判别力**，与 099 / 回归检查 先例同口径）：
源码一律**剔除整行 ``//`` 注释**后再断言（「注释掉」与「删掉」在判据面上等价）。

- 腿①（静态）：props 类型块内出现**带参签名** ``onOpenDoc(documentId: number | null): void;``；
- 腿②（静态）：``FlatHit`` chunk 变体带 ``document_id: number | null``，且 flat 映射
  chunk 分支带 ``document_id: hit.document_id ?? null``；
- 腿③（静态）：两条点击路径都传 id——``openHit`` 内 ``onOpenDoc(hit.document_id)``；
  chunk 命中行 ``onClick={() => onOpenDoc(hit.document_id ?? null)}``；
- 腿④（静态）：App 接线消费参数——``onOpenDoc={(documentId)`` 的处理体内出现
  ``openDocument(documentId)`` 与 null 兜底分支 ``if (documentId === null)``（兜底在前）；
- 腿⑤（**负控**）：笔记命中行**原样**——``onClick={() => onOpenNote(hit.id)}`` 仍在、
  App 接线 ``onOpenNote={jumpNote}`` 未动（笔记路径与本次修复无关）。

回退口径：**反向应用 115 的 diff** 后，腿①–④必须红、腿⑤不受影响。
``SearchHit.document_id`` 是 **02 契约已登记字段**（``frontend/src/api.ts`` 契约镜像
``document_id?: number | null``）⇒ 本条**零 02 改动**。

⛔ **口径**：静态判别力 ≠ 运行期渲染已验证（本仓无 JS 运行时测试设施）。
"""

from __future__ import annotations

from pathlib import Path

FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend" / "src"
SEARCH_MODAL = FRONTEND_DIR / "components" / "SearchModal.tsx"
FRONTEND_APP = FRONTEND_DIR / "App.tsx"


def _strip_line_comments(path: Path) -> str:
    """读源码全文，**剔除整行注释**后再返回（099 判别力纪律同款）。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("//"))


def _search_source() -> str:
    return _strip_line_comments(SEARCH_MODAL)


def _app_source() -> str:
    return _strip_line_comments(FRONTEND_APP)


def _balanced_span(source: str, start: int, opener: str, closer: str) -> str:
    """从 ``start``（指向 ``opener``）起，按**括号配平**截出到配对 ``closer`` 的一段源码。

    ⚠️ 非贪婪正则会在这类嵌套对象字面量处**提前收口**（先例 回归检查 ``_balanced_span``）——
    纯配对扫描不受嵌套影响。找不到 opener / 括号未配平 ⇒ 直接断言失败（**不**返回空串）。
    """
    assert start >= 0, f"起始锚点不存在：{opener}"
    assert start < len(source) and source[start] == opener, (
        f"起始锚点没落在 {opener!r} 上：{source[start:start + 40]!r}"
    )
    depth = 0
    for index in range(start, len(source)):
        char = source[index]
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"括号未配平（源码被截断？）：{source[start:start + 80]!r}")


def _enclosing_object(source: str, inner: str) -> str:
    """截出包含 ``inner``（首次出现）的**最近一层花括号对象**（配平截取）。"""
    inner_at = source.index(inner)
    open_at = source.rindex("{", 0, inner_at)
    return _balanced_span(source, open_at, "{", "}")


# --- 腿①：props 类型块内出现带参签名 -------------------------------------------


def test_leg1_props_signature_takes_document_id():
    """``SearchModalProps`` 里 ``onOpenDoc`` 必须带 ``documentId`` 参数（无参 = id 被丢弃的旧形态）。"""
    source = _search_source()
    props_open = source.index("type SearchModalProps = {")
    props_block = _balanced_span(source, source.index("{", props_open), "{", "}")
    assert "onOpenDoc(documentId: number | null): void;" in props_block


# --- 腿②：FlatHit chunk 变体与 flat 映射分支都带 document_id ---------------------


def test_leg2_flathit_chunk_variant_and_flat_map_carry_document_id():
    """chunk 变体类型面带 ``document_id: number | null``；flat 映射分支带 ``?? null`` 归一。"""
    source = _search_source()
    # 首个 kind: "chunk" 出现在 FlatHit 类型定义里（chunk 变体）
    variant = _enclosing_object(source, 'kind: "chunk"')
    assert "document_id: number | null" in variant
    # flat 映射的 chunk 分支（kind: "chunk" as const 的对象字面量）
    flat_chunk = _enclosing_object(source, 'kind: "chunk" as const')
    assert "document_id: hit.document_id ?? null" in flat_chunk


# --- 腿③：两条点击路径都把 id 传出去 --------------------------------------------


def test_leg3_both_click_paths_pass_document_id():
    """键盘回车（openHit else 分支）与鼠标点行（chunk 命中行）两条路径都必须带 id。"""
    source = _search_source()
    openhit_open = source.index("function openHit(hit: FlatHit) {")
    openhit = _balanced_span(source, source.index("{", openhit_open), "{", "}")
    assert 'if (hit.kind === "note") onOpenNote(hit.id);' in openhit
    assert "else onOpenDoc(hit.document_id);" in openhit
    assert "onClick={() => onOpenDoc(hit.document_id ?? null)}" in source


# --- 腿④：App 接线消费参数（null 兜底在前，openDocument 在后）---------------------


def test_leg4_app_wiring_consumes_document_id():
    """``onOpenDoc={(documentId)`` 的处理体必须消费参数：null 兜底分支在前、跳文档在后。"""
    source = _app_source()
    wire_at = source.index("onOpenDoc={(documentId)")
    body_open = source.index("=> {", wire_at)
    handler = _balanced_span(source, source.index("{", body_open), "{", "}")
    assert "setSearchOpen(false);" in handler
    assert "if (documentId === null)" in handler
    assert "openDocument(documentId);" in handler
    assert handler.index("if (documentId === null)") < handler.index("openDocument(documentId);")


# --- 腿⑤（负控）：笔记命中路径原样 ------------------------------------------------


def test_leg5_note_path_untouched():
    """笔记路径与本次修复无关：命中行 onClick 与 App 接线都必须原样（回退 115 不该动它）。"""
    assert "onClick={() => onOpenNote(hit.id)}" in _search_source()
    assert "onOpenNote={jumpNote}" in _app_source()
