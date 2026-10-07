"""可留档的 embedding 身份、隔离空间与构建点编号；不含凭证。"""

from __future__ import annotations

import hashlib
import json
import uuid


def embedding_identity(client) -> dict[str, str | int]:
    """生产客户端提供固定身份；简易测试桩按类型和维度区分。"""
    identity = getattr(client, "identity", None)
    if callable(identity):
        raw = identity()
    else:
        # ponytail: 只用于注入的测试桩；生产客户端必须实现 identity()。
        raw = {
            "provider": "test",
            "model": f"{type(client).__module__}.{type(client).__qualname__}",
            "dim": getattr(client, "dim", 0),
        }
    if not isinstance(raw, dict) or type(raw.get("dim")) is not int or raw["dim"] <= 0:
        raise ValueError("embedding 身份必须包含正整数维度")
    return {
        "provider": str(raw.get("provider", "") or ""),
        "service_id": str(raw.get("service_id", "") or ""),
        "base_url": str(raw.get("base_url", "") or ""),
        "model": str(raw.get("model", "") or ""),
        "dim": raw["dim"],
    }


def space_id(identity: dict) -> str:
    """同一服务、模型和维度得到同一空间；同维不同模型仍隔离。"""
    raw = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def point_id(build_id: str, chunk_id: int) -> str:
    """构建与分块共同确定 UUID；重试幂等，历史版本互不覆盖。"""
    if not build_id or type(chunk_id) is not int or chunk_id <= 0:
        raise ValueError("向量点需要有效构建编号与分块编号")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"zhilan:{build_id}:{chunk_id}"))
