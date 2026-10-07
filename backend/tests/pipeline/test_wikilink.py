"""双向链接提取规则（老仓库 test_wikilink.py 离线用例对拍）。

需容器的建边/维护/扩链用例已随阶段 4 · 4.5 落到 `tests/api/test_notes_4_5_2.py`（那里有真库夹具）；
本文件只对拍 extract_links 纯函数行为。⚠️ 老的「检索扩展」半段（`expand_chunks` / `_link_fragment`）
零调用点，已于 4.5 收尾删除（一跳扩展改由 `app/retrieval/link.py` 以 `Material` 原生实现）。
"""

from app.pipeline import wikilink


def test_extract_links_dedupes_and_keeps_order():
    text = "见 [[笔记A]] 与 [[笔记B]]，再提一次 [[笔记A]]。"
    assert wikilink.extract_links(text) == ["笔记A", "笔记B"]


def test_extract_links_skips_embed_and_takes_title_part():
    """嵌入 ``![[x]]`` 不建链；块级引用只取 ``#`` 前段（本轮不做块级语义）。"""
    text = "![[嵌入图片.png]] 不算；[[目标笔记#第三章]] 取标题；[[另一个]]"
    assert wikilink.extract_links(text) == ["目标笔记", "另一个"]


def test_extract_links_ignores_malformed():
    assert wikilink.extract_links("[[]] [[ ]] [[a][b]] 正常 [[好的]]") == ["好的"]


def test_extract_links_no_multiline_titles():
    """标题不跨行（Obsidian 语义）：换行让链接失效，不会造出永远无法解析的边。"""
    assert wikilink.extract_links("[[跨行\n标题]] [[正常]]") == ["正常"]
