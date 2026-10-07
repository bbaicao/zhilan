# -*- coding: utf-8 -*-
"""条目 3.0 交付物 ③「中文全文检索选型」的可复跑验收用例。

- 验证中文全文检索在内存 SQLite 数据库中的分词与命中行为。
- 本文件**自包含**：只用标准库 ``sqlite3`` + ``pytest`` 断言；
  不依赖产品代码（不 import ``app.*``）、不依赖容器 / 网络 / Qdrant / MySQL，全程内存库。
- 单跑：``cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_chinese_search_choice.py -q``
- 实测环境（2026-09-18）：Python 3.13.14 / SQLite 3.50.4 / Windows。

用例分四组：
1. 失败基线复现（C1 trigram、C2 unicode61）——锁住「为什么不能只用 trigram / 默认分词」；
2. 推荐方案（写侧 2-gram + 查询侧整段真短语）的正确性——含 2 字短词、长词、混合脚本；
3. 误召回与 1 字查询两条已知限制——把边界钉在用例里，避免以后被当成 bug 重新「发现」；
4. 索引形态可用性——BM25 打分、索引路径、行级删除/改写。
"""

from __future__ import annotations

import re
import sqlite3

import pytest

CJK_SEG = re.compile(r"[\u4e00-\u9fff]+|[^\u4e00-\u9fff]+")

# 语料：9 条，覆盖目标短词「向量 / 检索 / 分块」+ 诱饵词「图谱」+ ASCII 词
DOCS = (
    (1, "向量数据库是桌面版的检索基石，向量维度固定为一千零二十四。"),
    (2, "文档分块策略：按标题切小节并贪心合并，分块大小默认五百字。"),
    (3, "中文全文检索需要覆盖短词，检索层使用倒数排名融合两路召回。"),
    (4, "向量库使用 Qdrant 内嵌模式，向量化由上游接口完成。"),
    (5, "关键词检索与向量检索互补，短词检索最容易漏召回。"),
    (6, "分块过大或过小都会降低检索精度，分块与检索需一起调。"),
    (7, "Redis 在桌面版由进程内垫片顶替，不依赖任何外部服务。"),
    (8, "双路召回与融合：向量 Top-K 加上关键词路，按 RRF 融合排序。"),
    (9, "安装包体积敏感，尽量不新增第三方依赖；图谱能力留待后续阶段。"),
)

# 对抗样本：bigram 全部散落，但**没有**「数据检索」这个子串
ADVERSARIAL = (
    (1, "数据库处理与据检查询都很慢，检索接口待优化。"),
    (2, "数据检索链路与据检查询相互独立。"),
)

SUPPORTS_CONTENTLESS_DELETE = sqlite3.sqlite_version_info >= (3, 43)


# --------------------------------------------------------------------------
# 参考实现（= 决策记录「推荐方案」的最小可跑版，非产品代码）
# --------------------------------------------------------------------------
def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def bigram_expand(text: str) -> str:
    """写侧展开：中文连续段按 2-gram 切开，非中文段原样保留（unicode61 自带切分）。"""
    parts: list[str] = []
    for seg in CJK_SEG.findall(text):
        if not seg:
            continue
        if _is_cjk(seg[0]):
            if len(seg) == 1:
                parts.append(seg)
            else:
                parts.extend(seg[k : k + 2] for k in range(len(seg) - 1))
        elif seg.strip():
            parts.append(seg.strip())
    return " ".join(parts)


def match_query(query: str) -> str | None:
    """查询改写：返回 FTS5 MATCH 串；``None`` 表示本方案不接管（须走 LIKE 兜底）。

    规则（决策记录 §二）：1 字 → 兜底；2 字 → 单个 bigram 词项；
    ≥3 字 → 段内 bigram **真短语**（引号串内部空格 = 位置相邻），多段用 AND 连接。
    """
    q = query.strip()
    if len(q) < 2:
        return None
    clauses: list[str] = []
    for seg in (s for s in CJK_SEG.findall(q) if s.strip()):
        tokens = bigram_expand(seg).split()
        if not tokens:
            continue
        clause = " ".join(tokens).replace('"', '""')
        clauses.append('"%s"' % clause)
    return " AND ".join(clauses) if clauses else None


def make_db(docs=DOCS) -> sqlite3.Connection:
    """建库：chunks 为真相源；bigram 索引是**只存索引**的 contentless 虚表。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE chunks(id INTEGER PRIMARY KEY, content TEXT)")
    conn.executemany("INSERT INTO chunks(id, content) VALUES (?,?)", docs)
    conn.execute(
        "CREATE VIRTUAL TABLE chunks_fts USING fts5("
        "bi, content='', contentless_delete=1, tokenize='unicode61')"
    )
    conn.executemany(
        "INSERT INTO chunks_fts(rowid, bi) VALUES (?,?)",
        [(i, bigram_expand(t)) for i, t in docs],
    )
    conn.commit()
    return conn


def search(conn: sqlite3.Connection, query: str) -> list[int]:
    """按决策记录的口径检索，返回命中 chunk id（升序）。"""
    mq = match_query(query)
    if mq is None:
        rows = conn.execute(
            "SELECT id FROM chunks WHERE content LIKE ?", ("%" + query + "%",)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank", (mq,)
        ).fetchall()
    return sorted(r[0] for r in rows)


def make_fts(docs, tokenize: str) -> sqlite3.Connection:
    """按给定分词器建一张内嵌正文的 FTS5 表（用于 C1/C2 基线复现）。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(content, tokenize=%s)" % tokenize)
    conn.executemany("INSERT INTO t(rowid, content) VALUES (?,?)", docs)
    conn.commit()
    return conn


def fts_hits(conn: sqlite3.Connection, query: str) -> list[int]:
    rows = conn.execute("SELECT rowid FROM t WHERE t MATCH ? ORDER BY rowid", (query,)).fetchall()
    return [r[0] for r in rows]


def _fts5_available() -> bool:
    try:
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
        return True
    except sqlite3.Error:
        return False


def _trigram_available() -> bool:
    try:
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE VIRTUAL TABLE probe USING fts5(x, tokenize='trigram')")
        return True
    except sqlite3.Error:
        return False


pytestmark = pytest.mark.skipif(not _fts5_available(), reason="该 Python 自带的 SQLite 未编译 FTS5")


@pytest.fixture()
def db():
    if not SUPPORTS_CONTENTLESS_DELETE:
        pytest.skip("contentless_delete=1 需要 SQLite >= 3.43.0（本机 %s）" % sqlite3.sqlite_version)
    conn = make_db(DOCS)
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 1. 失败基线（C1 / C2）：这两条锁住「为什么不能只用 trigram / 默认分词器」
# --------------------------------------------------------------------------
@pytest.mark.skipif(not _trigram_available(), reason="FTS5 trigram 需要 SQLite >= 3.34.0")
def test_c1_trigram_two_char_words_return_nothing():
    """C1 基线（2026-09-18 实测复现）：trigram 对 2 字中文词恒 0 命中。

    这是环境行为特征，不是产品缺陷；若某天 SQLite 改了这条语义，
    这个用例会红，提示可以重新评估「短词兜底」是否还需要。
    """
    conn = make_fts(DOCS, "'trigram'")
    assert fts_hits(conn, "向量") == []          # 「向量」在 [1,4,5,8] 中出现，但 2 字 < 3-gram
    assert fts_hits(conn, "检索") == []
    assert fts_hits(conn, "分块") == []
    # 查询侧写法救不回来：前缀 / 拆词短语同样无效
    assert fts_hits(conn, "向量*") == []
    assert fts_hits(conn, '"向 量"') == []


@pytest.mark.skipif(not _trigram_available(), reason="FTS5 trigram 需要 SQLite >= 3.34.0")
def test_c1_trigram_three_chars_and_longer_are_exact():
    """C1：≥3 字时 trigram 是**精确子串**语义（零误召回）。"""
    conn = make_fts(DOCS, "'trigram'")
    assert fts_hits(conn, "向量库") == [4]
    assert fts_hits(conn, "向量检索") == [5]           # 只命中真含该子串的文档
    assert fts_hits(conn, "关键词检索与向量检索互补") == [5]
    assert fts_hits(conn, "图谱") == []                # 2 字诱饵同样漏


def test_c2_unicode61_treats_a_cjk_run_as_one_token():
    """C2 基线：unicode61 不做中文切分——整段连续中文 = 一个 token。"""
    conn = make_fts(DOCS, "'unicode61'")
    assert fts_hits(conn, "中文全文") == []            # 4 字词照样 0 命中
    assert fts_hits(conn, "中文全文检索需要覆盖短词") == [3]   # 整段原样才命中
    assert fts_hits(conn, "向量") == [8]               # 只有「向量」恰好成段（后接空格）才命中
    # 前缀只在段首生效 ⇒ 段中的「向量检索」漏召回
    assert fts_hits(conn, "向量*") == [1, 4, 8]


# --------------------------------------------------------------------------
# 2. 推荐方案的正确性：写侧 2-gram + 查询侧真短语
# --------------------------------------------------------------------------
def test_expand_is_bigram_and_keeps_ascii_whole():
    assert bigram_expand("分块策略") == "分块 块策 策略"
    assert bigram_expand("Redis") == "Redis"
    assert bigram_expand("库") == "库"


def test_match_query_rewriting_rules():
    """查询改写是 3.1 的直接接口，逐档钉死。"""
    assert match_query("库") is None                            # 1 字：兜底
    assert match_query("") is None
    assert match_query("   ") is None
    assert match_query("向量") == '"向量"'                       # 2 字：单个 bigram
    assert match_query("向量库") == '"向量 量库"'                 # 3 字：真短语
    assert match_query("向量检索") == '"向量 量检 检索"'           # 4 字：重叠 bigram 相邻
    assert match_query("Redis") == '"Redis"'
    assert match_query("向量 Top-K") == '"向量" AND "Top-K"'      # 跨脚本按段 AND


@pytest.mark.parametrize(
    "word,expect",
    [
        ("向量", [1, 4, 5, 8]),
        ("检索", [1, 3, 5, 6]),
        ("分块", [2, 6]),
        ("图谱", [9]),
    ],
)
def test_short_words_two_chars_are_fully_recalled(db, word, expect):
    """核心验收：2 字中文词全召回、零误召回（trigram 在这里是 0）。"""
    assert search(db, word) == expect


@pytest.mark.parametrize(
    "query,expect",
    [
        ("向量库", [4]),
        ("检索层", [3]),
        ("分块大", [2]),
        ("向量检索", [5]),
        ("分块策略", [2]),
        ("中文全文", [3]),
        ("倒数排名", [3]),
        ("Redis", [7]),
        ("Qdrant", [4]),
        ("关键词检索与向量检索互补", [5]),
    ],
)
def test_long_queries_are_substring_exact(db, query, expect):
    assert search(db, query) == expect


def test_no_hit_word_returns_empty(db):
    assert search(db, "牛顿力学") == []
    assert search(db, "不存在") == []


# --------------------------------------------------------------------------
# 3. 两条已知限制（写进用例，避免以后当成回归）
# --------------------------------------------------------------------------
@pytest.mark.parametrize("query,expect", [("数据检索", [2]), ("向量检索", [])])
def test_bridge_bigram_false_positive_is_killed_by_phrase(query, expect):
    """误召回压力测试：bigram 全部散落时，**真短语**必须不命中。

    对照：写成 AND（``"数据" "据检" "检索"`` 或不加引号）会把 [1] 也捞回来 ⇒
    推荐方案坚持真短语，就是因为这条。
    """
    conn = make_db(ADVERSARIAL)
    assert search(conn, query) == expect

    and_form = " ".join(bigram_expand(query).split())
    got = [r[0] for r in conn.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rowid", (and_form,)
    ).fetchall()]
    if query == "数据检索":
        assert got == [1, 2]        # AND 的误召回：文档 1 没有该子串却被命中
    else:
        assert got == []


def test_one_char_query_falls_back_to_like(db):
    """1 字查询：bigram 索引里没有单字词项，必须显式走 LIKE 兜底（本方案的边界）。"""
    assert match_query("库") is None
    assert search(db, "库") == [1, 4]
    assert search(db, "索") == [1, 3, 5, 6]
    indexed = [
        r[0]
        for r in db.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?", (bigram_expand("库"),)
        ).fetchall()
    ]
    assert indexed == []


# --------------------------------------------------------------------------
# 4. 索引形态可用性（3.1 要 BM25 打分、要能删行）
# --------------------------------------------------------------------------
def test_bm25_score_and_rank_are_available(db):
    """contentless 形态下 ``bm25()`` / ``ORDER BY rank`` 仍可用（3.1 的关键词路打分）。"""
    rows = db.execute(
        "SELECT rowid, bm25(chunks_fts) AS score FROM chunks_fts "
        "WHERE chunks_fts MATCH ? ORDER BY rank",
        (match_query("分块策略"),),
    ).fetchall()
    assert [r[0] for r in rows] == [2]
    assert isinstance(rows[0][1], float)
    assert rows[0][1] < 0            # FTS5 的 bm25() 返回越小越相关


def test_short_word_path_uses_the_index_not_a_scan(db):
    """2 字短词走的是 FTS 索引（不是 chunks 全表扫描）——EQP 断言。"""
    plan = " ".join(
        r[3]
        for r in db.execute(
            "EXPLAIN QUERY PLAN SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?",
            (match_query("向量"),),
        ).fetchall()
    )
    assert "chunks_fts" in plan


def test_index_rows_can_be_deleted_and_rewritten(db):
    """purge_document / 重传改写要走行级 DELETE / UPDATE（contentless_delete=1）。"""
    db.execute("DELETE FROM chunks_fts WHERE rowid = 9")
    assert search(db, "图谱") == []
    assert search(db, "向量") == [1, 4, 5, 8]        # 删一条不影响其余

    db.execute(
        "UPDATE chunks_fts SET bi = ? WHERE rowid = 7", (bigram_expand("这段文字不含目标词"),)
    )
    assert search(db, "Redis") == []
    assert search(db, "向量") == [1, 4, 5, 8]


def test_contentless_index_needs_no_extra_text_column():
    """推荐形态的物理事实：虚表只存索引，读不到正文列（正文在 chunks）。"""
    conn = make_db(DOCS)
    assert conn.execute("SELECT bi FROM chunks_fts WHERE rowid = 1").fetchone() == (None,)
    assert conn.execute("SELECT content FROM chunks WHERE id = 1").fetchone()[0].startswith("向量")
