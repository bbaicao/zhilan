r"""v2 ORM 模型（阶段 0 · 0.2 新 schema；历史骨架继承自阶段 2 · 2.1.1）。

v2 不要求与老库同构；对外只经 API。下列模型声明定义各表的字段与职责。

表清单：notes / documents / document_builds / chunks / chats / session_groups / messages /
wiki_links / knowledge_bases / usage_events / session_events /
message_attributions / agent_runs / agent_steps / memories / lexical_materials / lexical_terms。

- 会话分组：``session_groups`` 对应统筹 chat_groups 职责（侧栏分组，前端/契约
  已用该名，**不改名**）。
- 回收站无独立表：notes / documents / chats 以 ``deleted_at`` 软删。
- 设置项无独立表：读写走 ``.env`` / ``app/core/env_store.py``。
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    and_,
    false,
    func,
    or_,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates
from sqlalchemy.types import TypeEngine

from app.core.kb_identity import kb_path_key


def _long_text() -> TypeEngine:
    """长文本列：**MySQL 侧仍是 ``LONGTEXT``**（既有库结构不变），其它方言（SQLite）退化为 ``TEXT``。

    阶段 3 · 条目 3.0（B5 方言可切换）：改造前直接写 ``mysql.LONGTEXT``，
    在 SQLite 建表会直接编译失败。用 ``with_variant`` 保留 MySQL 侧类型——
    **不能**简单换成 ``Text``：``TEXT`` 在 MySQL 只有 64KB，会静默改变列的容量语义。
    """
    return Text().with_variant(mysql.LONGTEXT(), "mysql")


def _json() -> TypeEngine:
    """JSON 列：MySQL 侧显式 ``mysql.JSON``，其它方言用通用 ``JSON``（SQLite 存 TEXT）。"""
    return JSON().with_variant(mysql.JSON(), "mysql")


def _big_int_pk() -> TypeEngine:
    """``session_events.id`` 主键：MySQL 侧 ``BIGINT``，**SQLite 侧必须退化为 ``INTEGER``**。

    实测（2026-09-18，SQLite 3.50.4）：SQLite 只把 ``INTEGER PRIMARY KEY`` 当 **rowid 别名**
    并自动赋值；``BIGINT PRIMARY KEY`` 是普通列，插入时不会自动填值 ⇒ ``NOT NULL`` 违约，
    **每轮问答写 ``session_events`` 直接失败**（当时 13 张表里唯一「迁移后写入即失败」的点；
    现为 14 张表 —— 加表时别只改数字，见 :data:`app.db.verify.TABLE_NAMES` 那份单一真相源）。
    SQLite 的 ``INTEGER`` 本身就是 64 位，容量语义与 ``BIGINT`` 等价。
    """
    return BigInteger().with_variant(Integer(), "sqlite")


class Base(DeclarativeBase):
    pass


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(_long_text(), nullable=False)
    category: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # 知识库归属（本机库 = 1）；S2 落库，M6 多库检索的基础
    kb_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 自定义标签：版本、部门等，过滤下推（S3）时用
    tags: Mapped[list | dict | None] = mapped_column(_json(), nullable=True)
    # 任务清单 29：AI 生成笔记的来源文档（溯源），手工笔记为 NULL
    source_document_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 任务清单 29：生成元数据（模型、完整性核对结果等），草稿保存时随笔记落库
    generation_meta: Mapped[dict | None] = mapped_column(_json(), nullable=True)
    # 条目 25 回收站：NULL = 在库；非空 = 已删（UTC naive）。彻底删除才真正清行
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_notes_category", "category"),
        Index("ix_notes_kb_id", "kb_id"),
        Index("ix_notes_source_document_id", "source_document_id"),
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # 历史状态列：与 index_status 并存兼容；新写路径两者同步写
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # 阶段 0.2 / 统筹 §2.4：索引状态机 pending → processing → ready|failed
    index_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    # 上传留档：本条目的「失败可重试」前提。解析/切块失败后能直接读留档重跑，
    # 不必让用户重新上传同一个文件；同时是「禁止整文件搬运」（统筹 §3.6）下的
    # 唯一物理副本——v2 不把整个源文件复制进知识库，只在 data/uploads 留一份。
    # 文件夹导入不复制文件，只记原文件绝对路径（upload_file 路径同样存这里）。
    # 内部列，绝不进任何响应体（DocumentOut 不含此字段）。
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # 入库失败的可读中文原因：落列而非只打日志，是为了「GET /documents 列表里
    # 失败的文档也能在徽标 tooltip 显示为什么失败」（前端 StatusBadge 用 message
    # 字段，而 message 在列表路径就地取 index_error）。internal column，不进响应体。
    index_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 向量是否真的写成功：status=ready 只代表分块入库，向量可能缺
    vectorized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    vector_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 清洗后文本的内容指纹，用于重复上传识别（NULL 允许，兼容历史数据）
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 解析得到的原始文本：清洗只作用在入库文本上，原文永远可查看、可回退（M3.2）
    raw_content: Mapped[str | None] = mapped_column(_long_text(), nullable=True)
    # 每步清洗统计 + 乱码标记 + 规则指纹（M3.2 / 回归检查）
    clean_report: Mapped[dict | None] = mapped_column(_json(), nullable=True)
    # 工具升级-3 · 回归检查（迁移 0006）：入库时探测到的编解码名（utf-8 / utf-8-sig / gb18030 / latin-1）。
    # 文档级属性（一个文件一种编码）⇒ 挂 Document 不挂 Chunk。列**只做「存下来」**：
    # 按原编码写回在阶段 5；本阶段判据只到「读得到探测名」（A8 前置腿）。internal column，不进响应体。
    source_encoding: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # U2：启用的索引构建与正文/分块版本；NULL 保留旧库正文和分块的身份。
    # 指针不建外键，避免 document_builds.document_id 与 documents 形成循环依赖。
    active_build_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    content_build_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 知识库归属（本机库 = 1）
    kb_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 分类（任务清单 12）：frontmatter 的 category，缺省回退文件夹名；S3 的
    # categories 过滤据此对文档生效（此前文档没有分类维度，指定分类即整体排除）
    category: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    tags: Mapped[list | dict | None] = mapped_column(_json(), nullable=True)
    # 条目 25 回收站：NULL = 在库；非空 = 已删（UTC naive）。指纹唯一约束仍在，
    # 回收站里的同内容文档会挡住重复上传（提示可恢复或彻底删除后再传）
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_documents_status", "status"),
        Index("ix_documents_index_status", "index_status"),
        Index("ix_documents_filename", "filename"),
        Index("ix_documents_kb_id", "kb_id"),
        Index("ix_documents_category", "category"),
        UniqueConstraint("content_hash", name="uk_documents_content_hash"),
    )


class IndexBuild(Base):
    """一轮索引构建：先准备和校验，再发布；旧版本继续供引用和回滚读取。"""

    __tablename__ = "document_builds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    base_build_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    content_build_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="preparing", server_default="preparing"
    )
    fingerprint: Mapped[dict] = mapped_column(_json(), nullable=False)
    space_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    point_ids: Mapped[list] = mapped_column(_json(), nullable=False, default=list)
    snapshot: Mapped[dict] = mapped_column(_json(), nullable=False, default=dict)
    vectorized: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_document_builds_document_id", "document_id"),)


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    build_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    content: Mapped[str] = mapped_column(_long_text(), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    # 阶段 0.2：页码 / 字符偏移元数据（解析器能给出才写；历史或纯文本切块为 NULL）
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 工具升级-3 · 回归检查（迁移 0006）：代码块回溯到**源文件行号** + 顶层符号名。
    # 三列只有 chunk_code() 填写（1 起闭区间）；chunk_document 的块一律 NULL。
    # ⚠️ 与 start_offset 不是同一坐标系（那是归一化全文字符偏移），不得互相挪用。
    line_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol_path: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index("ix_chunks_document_id", "document_id"),
        Index("uk_chunks_document_build_ordinal", "document_id", "build_id", "ordinal", unique=True),
    )


def active_chunks():
    """关联 Document 后使用的当前分块过滤条件；历史 NULL 版本仍可正常读取。"""
    return or_(
        and_(Document.content_build_id.is_(None), Chunk.build_id.is_(None)),
        Chunk.build_id == Document.content_build_id,
    )


class LexicalMaterial(Base):
    """U3 派生词项索引；文档沿用真实分块，笔记片段保留笔记身份及正文坐标。"""

    __tablename__ = "lexical_materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chunk_id: Mapped[int | None] = mapped_column(
        ForeignKey("chunks.id", ondelete="CASCADE"), nullable=True
    )
    note_id: Mapped[int | None] = mapped_column(
        ForeignKey("notes.id", ondelete="CASCADE"), nullable=True
    )
    fragment_ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # 正文始终只在源行；笔记片段由坐标回取，不复制长笔记或修改对外引用 ID。
    span_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    span_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    index_version: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    __table_args__ = (
        UniqueConstraint("chunk_id", name="uk_lexical_materials_chunk"),
        UniqueConstraint("note_id", "fragment_ordinal", name="uk_lexical_materials_note_fragment"),
    )


class LexicalTerm(Base):
    """文档版本/笔记来源桶分组 posting；锚仅负责存储，资格按每个成员核对。"""

    __tablename__ = "lexical_terms"

    term_hash: Mapped[bytes] = mapped_column(
        LargeBinary(33).with_variant(mysql.VARBINARY(33), "mysql"), primary_key=True
    )
    owner_material_id: Mapped[int] = mapped_column(
        ForeignKey("lexical_materials.id", ondelete="CASCADE"), primary_key=True
    )
    term: Mapped[str | None] = mapped_column(_long_text(), nullable=True)
    postings: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    __table_args__ = (
        Index("ix_lexical_terms_owner_material_id", "owner_material_id"),
        {"sqlite_with_rowid": False},
    )


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    clarify_rounds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # 条目 25 回收站：软删只置时间戳，消息行保留（恢复后原样回来）
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 条目 24 记忆压缩：当前摘要全文与已压缩覆盖到的最大 Message.id（NULL = 从未压缩）。
    # 只压「发给模型的历史」，messages 行与 refs 不动，摘要可随时在这里查看
    context_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    compressed_up_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 条目 40 会话分组：NULL = 未分组（「最近」平铺）；删组时置 NULL，不删会话。
    # 应用层维护（无外键约束）：分组是纯侧栏组织，删除分组只是把组内会话放回「最近」
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 条目 40 归档：NULL = 在「最近」；非空 = 已归档（UTC naive，与 deleted_at 同口径）。
    # 归档 ≠ 回收站：归档只是从「最近」隐藏、可恢复；删除仍走 deleted_at 软删进回收站
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SessionGroup(Base):
    """会话分组（条目 40）：侧栏「最近」里给会话建的可折叠分组。

    纯组织维度：不参与提问与检索范围（不是 Project 模式）。会话通过
    ``Chat.group_id`` 归入分组，删除分组时组内会话置回 NULL（回「最近」平铺）。
    """

    __tablename__ = "session_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    sort: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(_long_text(), nullable=False)
    refs: Mapped[dict | list | None] = mapped_column(_json(), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="normal", server_default="normal")
    # 该次提问用的元数据过滤范围（回归检查 可复现）：回看历史时能还原当时的检索范围
    filters: Mapped[dict | list | None] = mapped_column(_json(), nullable=True)
    # 条目 19 深度思考：厂商在响应里给的思考内容原文（我们不改写；厂商不给就是 NULL）
    reasoning: Mapped[str | None] = mapped_column(_long_text(), nullable=True)
    # 思考元数据：用的哪个参数哪一档、是不是自动加深、为什么（复杂判定理由）等
    thinking: Mapped[dict | None] = mapped_column(_json(), nullable=True)
    # 任务清单 35：本条消息启用的技能 id 列表（如 ["local:deep-research"]）；历史消息为 NULL
    skill_ids: Mapped[list | None] = mapped_column(_json(), nullable=True)
    # 条目 18：Agent 编排步骤留档 {mode, steps[]}；非 Agent 消息为 NULL
    agent_trace: Mapped[dict | None] = mapped_column(_json(), nullable=True)
    # 条目 53：本条**用户消息**随消息发出的图片（有序 base64 data URL）；助理消息与
    # 0017 之前的存量消息均为 NULL。只给界面回读，不参与发给模型的历史上下文。
    images: Mapped[list | None] = mapped_column(_json(), nullable=True)
    # 阶段 3 条目 3.5：**答案级来源标记**（角落标签的承载字段，迁移 0019）。
    # 取值由分路结果决定（不是模型说、也不是前端猜）：kb / general / empty_scope /
    # clarify 四值之一；历史存量消息为 NULL（界面不渲染标签），停止轮
    # （kind="stopped"）不是一次完整回答，同样保持 NULL。
    answer_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 统筹 §2.4：来源类型 internal_db / external（库内命中 vs 库外直答）
    source_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_messages_chat_id", "chat_id"),
        Index("ix_messages_source_type", "source_type"),
    )


class WikiLink(Base):
    """双向链接边（任务清单 13）：正文 ``[[标题]]`` 在保存/入库时解析出的引用关系。

    source 是含有 ``[[标题]]`` 的一方（笔记或文档）；target 是解析命中的一方——
    同 kb 内笔记标题优先，其次文档文件名（去扩展名）。两个 target 列都为空
    （``resolved=False``）表示链到了尚不存在的内容，前端标「未创建」。
    """

    __tablename__ = "wiki_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    kb_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    target_title: Mapped[str] = mapped_column(String(255), nullable=False)
    target_note_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_document_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("source_type", "source_id", "target_title", name="uk_wiki_links_source_target"),
        Index("ix_wiki_links_target_note_id", "target_note_id"),
        Index("ix_wiki_links_target_document_id", "target_document_id"),
        Index("ix_wiki_links_source", "source_type", "source_id"),
    )


class KnowledgeBase(Base):
    """知识库登记行。

    ``path_or_url_key`` 是 ``path_or_url`` **strip 之后**的 sha256 十六进制摘要
    （见 ``app.core.kb_identity``），两方言都对**它**建唯一索引 —— 等价于
    「strip 后的完整路径逐字唯一」。空路径映射为 ``NULL``，而 ``NULL`` 在 MySQL/SQLite
    下都允许多行，所以本机种子行（``id=1``、``path_or_url=""``）天然不在唯一域内。

    为什么不直接对 ``path_or_url`` 建唯一索引：``VARCHAR(1024)`` + ``utf8mb4`` = 4096
    字节，超过 InnoDB 的 3072 字节索引键长上限（MySQL 8.0.46 实测）。见 S6 · 186。
    """

    __tablename__ = "knowledge_bases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    path_or_url: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    #: 唯一身份摘要；空 path ⇒ NULL（不进唯一域）
    path_or_url_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    collection: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 用 Index(..., unique=True) 而不是 UniqueConstraint：SQLite 不支持
    # ALTER TABLE ADD CONSTRAINT，而 CREATE UNIQUE INDEX 两方言都原生支持（迁移侧同样用
    # op.create_index，从而不必进 batch 模式）。
    __table_args__ = (
        Index("uk_knowledge_bases_path_or_url_key", "path_or_url_key", unique=True),
    )

    @validates("path_or_url")
    def _sync_path_or_url_key(self, _key: str, value: str | None) -> str | None:
        """``path_or_url`` 被赋值时同步刷新摘要列（两列永远同源）。

        ⛔ 这不是「第二套 canonical 逻辑」：取值一律经
        ``app.core.kb_identity.kb_path_key``。
        注意 Python 侧列默认值（不传 ``path_or_url``、flush 时补空串）**不会**触发本钩子，
        结果是 ``key=None`` —— 与「空 path 不进唯一域」完全一致。
        """
        self.path_or_url_key = kb_path_key(value)
        return value


class UsageEvent(Base):
    """任务清单 27：一次真实模型调用的用量记录（成功才记）。

    tokens 三个列厂商响应里给了才写；拿不到就是 NULL——界面按「未知」展示
    （典型场景：流式对话不加 stream_options，拿不到 usage）。
    """

    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # naive 时间戳，与全库 server_default=func.now() 同口径
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    # chat / vision / embed / rerank
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="", server_default="")
    model: Mapped[str] = mapped_column(String(100), nullable=False, default="", server_default="")
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 这一行折合的调用次数（默认 1）
    calls: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # 预留杂项（批次、触发来源等）
    meta: Mapped[dict | None] = mapped_column(_json(), nullable=True)

    __table_args__ = (Index("ix_usage_events_created_at", "created_at"),)


class SessionEvent(Base):
    """第三期·对话机制 阶段 2（条目 2.1）：会话事件的**只追加**事实日志。

    DSH 的口径是「Session 是类型化事件的只追加日志，其它一切（模型历史、标题、
    界面窗口）都从这条流派生」；本表就是那条流的落库形态。三条不变量：

    1. 同一 ``(chat_id, seq)`` **至多一行**（唯一约束 ``uk_session_events_chat_seq``）；
    2. 同一行**永不 UPDATE**（没有 updated_at，业务层也不提供任何更新入口）；
    3. 事件**只追加**——删除只随会话级联（``chat_id`` 的 ON DELETE CASCADE）发生，
       会话软删（``Chat.deleted_at``）不触发级联。

    写入的唯一入口是 ``app/session/events.py`` 的 ``append_event``：
    禁止在业务代码里直接 ``db.add(SessionEvent(...))``。
    """

    __tablename__ = "session_events"

    id: Mapped[int] = mapped_column(_big_int_pk(), primary_key=True, autoincrement=True)
    # 外键显式命名（与老仓库迁移 0018、v2 迁移 0002 的 fk_session_events_chat_id 同一份口径，不依赖 MySQL 自动名）
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # 会话内序号：从 0 起单调递增、不断号（seq = 同会话 MAX(seq) + 1）
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # 轮次号（用户一次提问 = 一轮）/ 轮内步骤；仅日志事件也可为 NULL
    turn: Mapped[int | None] = mapped_column(Integer, nullable=True)
    step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 事件类型：词汇表以 app/session/events.py 的 SURFACE_EVENT_TYPES / LOG_ONLY_EVENT_TYPES 为准
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    # 载荷：必须是无损 JSON（校验规则见 append_event）
    data: Mapped[dict] = mapped_column(_json(), nullable=False)
    # 关联系（rpcId / retrieval_id / attempt_id）：把多个事件串成一条链
    trace: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 两个「谁和谁是一对」的关联 id：列由 0002 迁移建成，故意物化成列以便能走索引。
    #   tool_call_id：一次工具调用的稳定 id，tool/call 与配对结果事件同值；
    #   attempt_id ：一次**模型尝试**（阶段 4 · 条目 4.4 · D6 的口径，老基线 attempt.py 原样）
    #                的 id，assistant/attempt、stream/interrupted、assistant/message 三处同值。
    #                ⚠️ 原文写「流分片」是**错**的（核验代理 2026-09-19 打回）：老基线
    #                `attempt.py` 里的 "chunk" 指**前端 SSE 帧**、不是 session_events 行，
    #                而 v2 的 23 类词表里根本没有「流分片」这类事件 ⇒ 那一层是空气。
    # ✅ **两列都已有生产写入点**（2026-09-18 更正 `回归修复`；2026-09-19 条目 4.4 补齐后者）：
    #   `app.session.events.append_event()` 有 `tool_call_id` / `attempt_id` 形参（条目 4.1 落地）；
    #   `app/agent/runner.py` 的 `tool/call` ↔ `tool/result` 传**同一个** `tool_call_id`（I4）；
    #   `app/api/chat_routes.py` 的 `start_attempt()` + `log_attempt()` 为**两条回答链路**
    #   （流式 `/chat/stream` 与同步 `/chat`）铸并透传 `attempt_id`，且真的写进本列——
    #   回归钉 `tests/api/test_chat_detail_4_4.py`。
    #   ⚠️ 旧仓库那套「按 data 同名键自动镜像」的 `_link_from_payload` 在 v2 **不存在**：
    #   要落这两列**必须显式传形参**，不会自动从 `data` 里认出来。
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 幂等键：同一 (chat_id, request_id) 重复 append 的净效果为零
    # （规格扩展；老仓库在 0018、v2 由 0002 建成，见 0002 的 docstring）
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # naive 时间戳，与全库 server_default=func.now() 同口径
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("chat_id", "seq", name="uk_session_events_chat_seq"),
        Index("uk_session_events_chat_request_id", "chat_id", "request_id", unique=True),
        Index("ix_session_events_chat_id_seq", "chat_id", "seq"),
        Index("ix_session_events_chat_type", "chat_id", "type"),
        Index("ix_session_events_trace", "trace"),
        # 阶段 4 条目 4.4（恢复/投影）：恢复按缺口查（同会话 + tool_call_id）、流式对账按尝试查
        Index("ix_session_events_tool_call", "chat_id", "tool_call_id"),
        Index("ix_session_events_attempt", "chat_id", "attempt_id"),
        ForeignKeyConstraint(
            ["chat_id"], ["chats.id"], name="fk_session_events_chat_id", ondelete="CASCADE"
        ),
    )


class MessageAttribution(Base):
    """消息↔chunk 归因（统筹 §2.4）：引用卡片落库，支撑 I3 编号一致与溯源对账。

    ordinal 为该消息内引用编号（从 1 起）；chunk_id 指向本地 chunks 行，
    与 Qdrant 点 payload 中的 chunk_id 对应。
    """

    __tablename__ = "message_attributions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    chunk_id: Mapped[int] = mapped_column(
        ForeignKey("chunks.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    # 展示用快照（可冗余，避免引用时再 join）：标题/片段/分数
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    meta: Mapped[dict | None] = mapped_column(_json(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_message_attributions_message_id", "message_id"),
        Index("ix_message_attributions_message_ordinal", "message_id", "ordinal"),
        Index("ix_message_attributions_chunk_id", "chunk_id"),
        UniqueConstraint("message_id", "ordinal", name="uk_message_attributions_message_ordinal"),
    )


class AgentRun(Base):
    """Agent 运行（统筹 §2.4）：一次 Agent 编排的可观测主记录。"""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int | None] = mapped_column(
        ForeignKey("chats.id", ondelete="SET NULL"), nullable=True
    )
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    # react / plan_execute / off
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="off", server_default="off")
    # running / done / failed / stopped / error（error = 决策模型调用失败过，回归修复）
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running", server_default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    meta: Mapped[dict | None] = mapped_column(_json(), nullable=True)

    __table_args__ = (
        Index("ix_agent_runs_chat_id", "chat_id"),
        Index("ix_agent_runs_status", "status"),
    )


class AgentStep(Base):
    """Agent 步骤：与 agent_runs 配对，阶段 4 器官可观测与恢复用。"""

    __tablename__ = "agent_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # thought / tool_call / tool_result / message / plan ...
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict | None] = mapped_column(_json(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_agent_steps_run_id", "run_id"),
        Index("ix_agent_steps_run_step", "run_id", "step_no"),
        Index("ix_agent_steps_tool_call", "tool_call_id"),
    )


class Memory(Base):
    """条目级长期事实；U6 保留修订链、有效时间与来源快照。

    同一 scope/fact_key 的不同陈述先标冲突，明确修订才替代旧版；旧行保留。
    读取与写入均只经 session/memory.py。text_hash 仍是规范化正文 SHA256。
    """

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # global（用户偏好）/ chat:<id>（会话事实）/ kb:<id>（知识库事实）
    scope: Mapped[str] = mapped_column(
        String(32), nullable=False, default="global", server_default="global"
    )
    # 记忆类型：knowledge（关于世界的稳定事实）/ interaction（关于用户与用法的事实）
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(_long_text(), nullable=False)
    # 规范化文本的 sha256（64 位十六进制）：去重的唯一依据
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 来源：哪一轮抽出来的（会话删了不牵连记忆，故无外键、只记 id）
    source_chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fact_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    valid_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    supersedes_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    # 最后一次被召回进提示词的时间（NULL = 从未被用过）
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("scope", "text_hash", "version", name="uk_memories_scope_hash"),
        UniqueConstraint("scope", "fact_key", "version", name="uk_memories_scope_fact_version"),
        Index("ix_memories_created_at", "created_at"),
    )
