"""离线管道：解析 → 清洗 → 分块 → frontmatter / 双链 / 文件夹扫描 → 入库。

七模块（parsing / cleaning / chunking / frontmatter / wikilink / folder_scan / mineru）
整文件自老仓库 `app/rag/` 移植（阶段 2 · 2.2），行为一字不改；import 面按 v2 目录调整
（errors → core.errors，包内相对路径）。`ingest` / `storage` 是 v2 新增（条目 2.1）：
老仓库上传即解析、不留档、无状态机，v2 把「接入 + 状态机 + 可重试」独立成服务层。
"""

from app.pipeline.chunking import ChunkMeta, chunk_code, chunk_document, chunk_text, normalize
from app.pipeline.cleaning import clean, rule_fingerprint
from app.pipeline.folder_scan import FolderScan, FolderScanError, scan_folder
from app.pipeline.frontmatter import date_from_name, parse_frontmatter
from app.pipeline.ingest import (
    IngestResult,
    Prepared,
    ingest_document,
    list_documents,
    prepare_document,
    purge_document,
    retry_document,
)
from app.pipeline.mineru import available as mineru_available
from app.pipeline.parsing import (
    CODE_SUFFIXES,
    SUPPORTED_FORMATS_HINT,
    SUPPORTED_SUFFIXES,
    detect_encoding,
    parse_document,
    parse_document_pages,
    parse_path,
)
from app.pipeline.storage import resolve_stored, save_upload
from app.pipeline.wikilink import extract_links

__all__ = [
    "CODE_SUFFIXES",
    "SUPPORTED_FORMATS_HINT",
    "SUPPORTED_SUFFIXES",
    "ChunkMeta",
    "FolderScan",
    "FolderScanError",
    "IngestResult",
    "Prepared",
    "chunk_code",
    "chunk_document",
    "chunk_text",
    "clean",
    "date_from_name",
    "detect_encoding",
    "extract_links",
    "ingest_document",
    "list_documents",
    "mineru_available",
    "normalize",
    "parse_document",
    "parse_document_pages",
    "parse_frontmatter",
    "parse_path",
    "prepare_document",
    "purge_document",
    "resolve_stored",
    "retry_document",
    "rule_fingerprint",
    "save_upload",
    "scan_folder",
]
