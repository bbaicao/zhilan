"""向量范围元数据与 SQL 文档使用同一身份，旧点可只改 payload 回填。"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json


def scope_payload(*, kb_id, category, tags, created_at) -> dict:
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if isinstance(created_at, datetime):
        date = created_at.replace(tzinfo=timezone.utc) if created_at.tzinfo is None else created_at
        timestamp = date.timestamp()
    else:
        timestamp = None
    payload = {
        "kb_id": int(kb_id or 1),
        "category": category or "",
        "tags": sorted({str(tag).strip() for tag in (tags or []) if str(tag).strip()}),
        "created_ts": timestamp,
    }
    payload["scope_hash"] = sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return payload
