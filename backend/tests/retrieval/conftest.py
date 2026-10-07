"""``tests/retrieval`` 的公共助手（阶段 3 · 条目 3.1）。

只放一件事：往测试库里造「可检索语料」（``documents`` + ``chunks``）。
DB 夹具（``db`` / ``engine`` / ``fresh_db``）在 ``tests/conftest.py``，不重复定义。
"""

from __future__ import annotations

from datetime import datetime

import pytest

# 选型记录（证据 ``S3-中文检索选型.md`` §二）用的那 9 条语料——检索用例沿用同一批文本，
# 便于「算法语义」与「选型实测数字」逐条对照。
SELECTION_CORPUS = (
    "向量数据库是桌面版的检索基石，向量维度固定为一千零二十四。",
    "文档分块策略：按标题切小节并贪心合并，分块大小默认五百字。",
    "中文全文检索需要覆盖短词，检索层使用倒数排名融合两路召回。",
    "向量库使用 Qdrant 内嵌模式，向量化由上游接口完成。",
    "关键词检索与向量检索互补，短词检索最容易漏召回。",
    "分块过大或过小都会降低检索精度，分块与检索需一起调。",
    "Redis 在桌面版由进程内垫片顶替，不依赖任何外部服务。",
    "双路召回与融合：向量 Top-K 加上关键词路，按 RRF 融合排序。",
    "安装包体积敏感，尽量不新增第三方依赖；图谱能力留待后续阶段。",
)

# 真短语命中（含「数据检索」子串）与对抗样本（含 数据 / 据检 / 检索 但**不含**该子串）
PHRASE_DOC = "数据检索必须覆盖短词，双路召回与融合是基础。"
ADVERSARIAL_DOC = "数据库处理与据检查询都很慢，检索接口待优化。"


async def seed_document(
    db,
    chunks: list[str] | tuple[str, ...],
    *,
    filename: str = "检索说明.md",
    kb_id: int = 1,
    category: str = "",
    tags: list[str] | None = None,
    index_status: str = "ready",
    deleted: bool = False,
    start_ordinal: int = 1,
):
    """建一条文档 + 若干分块并 **commit**（用例会话结束时会回滚清理）。

    这里**必须 commit 而不是只 flush**：走 HTTP 的接线用例里，请求用的是**另一个会话**
    （``tests/api/test_chat_contract.py::client`` 的 ``get_db`` 覆盖），未提交的行它看不见
    ——只 ``flush`` 的话「有资料」那一态会静默退化成「无资料」，用例看起来是绿的，其实没测到东西。

    ``deleted=True`` 造软删（``deleted_at`` 非空）；``index_status`` 默认 ``ready``
    ——这两条正是检索必须排除的两种行（软删不删向量、未就绪不可检索）。
    """
    from app.models import Chunk, Document

    document = Document(
        filename=filename,
        file_type="md",
        status="ready",
        index_status=index_status,
        kb_id=kb_id,
        category=category,
        tags=tags,
        deleted_at=datetime(2026, 9, 18, 12, 0, 0) if deleted else None,
    )
    db.add(document)
    await db.flush()
    for offset, text in enumerate(chunks):
        db.add(
            Chunk(
                document_id=document.id,
                content=text,
                ordinal=start_ordinal + offset,
                page=None,
            )
        )
    await db.commit()
    return document


async def seed_corpus(db, texts: list[str] | tuple[str, ...], **kwargs):
    """一条文本一份文档（每条一个分块）——选型语料那种「一文档一段」的形态。"""
    documents = []
    for index, text in enumerate(texts, start=1):
        documents.append(
            await seed_document(
                db, [text], filename=f"语料{index}.md", **kwargs
            )
        )
    return documents


@pytest.fixture
def seed():
    """用例内的异步造行助手（``await seed(db, [...])``）。"""
    return seed_document


@pytest.fixture
def seed_many():
    """一次造多份文档（``await seed_many(db, [...])``，返回文档列表）。"""
    return seed_corpus
