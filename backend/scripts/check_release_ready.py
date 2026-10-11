"""Run the named release checks on a disposable source copy without real services."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys

TESTS = [
    "api/test_bug101_vision_wiring.py",
    "api/test_bug118_vision_ready_gate.py",
    "api/test_chat_contract.py",
    "api/test_chat_retrieval_wiring.py",
    "api/test_installed_repair_s2_settings.py",
    "api/test_installed_repair_s3_images.py",
    "api/test_installed_repair_s3_r1_cancel.py",
    "api/test_installed_repair_s4.py",
    "api/test_installed_repair_s5_memory.py",
    "api/test_semantic_cache_http.py",
    "generation/test_bug101_vision_messages.py",
    "generation/test_direct_answer.py",
    "generation/test_installed_repair_s2_models.py",
    "session/test_compaction.py",
    "session/test_installed_repair_s5.py",
    "test_installed_repair_s1_r2_resources.py",
    "test_installed_repair_s1_resources.py",
    "test_reasoning.py",
    "api/test_release_ready_upload.py",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--mode", choices=("targeted", "upload", "restart-seed", "restart-read"),
                        default="targeted")
    args = parser.parse_args()
    source, state = args.source.resolve(), args.state.resolve()
    if any((source / name).exists() for name in (".env", "backend/.env")):
        parser.error("Use an exported disposable source copy without .env.")
    if state == source or state.is_relative_to(source):
        parser.error("--state must be outside the disposable source tree.")
    state.mkdir(parents=True, exist_ok=True)
    marker = state / ".release-ready-state"
    if marker.exists():
        if marker.read_text(encoding="utf-8") != str(source):
            parser.error("--state belongs to a different disposable source copy.")
    elif any(state.iterdir()):
        parser.error("--state must be empty on its first use.")
    else:
        marker.write_text(str(source), encoding="utf-8")
    for name in list(os.environ):
        if any(part in name.upper() for part in ("API_KEY", "APIKEY", "TOKEN", "PASSWORD")):
            os.environ.pop(name, None)
    os.environ.update({
        "DB_BACKEND": "sqlite", "SQLITE_PATH": str(state / "isolated.db"),
        "QDRANT_PATH": str(state / "qdrant"), "QDRANT_URL": "",
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "HF_HOME": str(state / "hf"), "XDG_CACHE_HOME": str(state / "cache"),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
        "MODEL_SERVICES": json.dumps([{
            "id": "offline-cache-control", "provider": "openai",
            "base_url": "http://127.0.0.1:9/v1", "api_key": "offline-placeholder",
            "models": ["offline-cache-control"],
        }]),
        "MODEL_ROLES": json.dumps({
            "chat": {"service_id": "offline-cache-control", "model": "offline-cache-control"},
        }),
        "MODEL_PROVIDER": "",
        "S5_RESTART_DB": str(state / "restart.db"), "S5_CAPTURE_DIR": str(state),
    })
    os.chdir(source / "backend")
    sys.path.insert(0, str(source / "backend"))
    blocked = []
    originals = (socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo)

    def local(host):
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except (ValueError, TypeError):
            return False

    def guard_connect(original):
        def guarded(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6) and not local(address[0]):
                blocked.append(str(address[0]))
                raise RuntimeError("RELEASE_CHECK_EXTERNAL_NETWORK_BLOCKED")
            return original(sock, address)
        return guarded

    def guarded_lookup(host, *positional, **keywords):
        if not local(host):
            blocked.append(str(host))
            raise RuntimeError("RELEASE_CHECK_EXTERNAL_DNS_BLOCKED")
        return originals[2](host, *positional, **keywords)

    socket.socket.connect = guard_connect(originals[0])
    socket.socket.connect_ex = guard_connect(originals[1])
    socket.getaddrinfo = guarded_lookup
    if args.mode == "targeted":
        selected = ["tests/" + name for name in TESTS]
    elif args.mode == "upload":
        selected = [
            "tests/api/test_release_ready_upload.py",
            "tests/test_installed_repair_s1_r2_resources.py",
            "tests/api/test_bug122_upload_size_precheck.py",
            "tests/pipeline/test_s5_bug168_bom_frontmatter.py",
        ]
    else:
        function = ("test_seed_persistent_memory_and_summary" if args.mode == "restart-seed"
                    else "test_read_persistent_memory_and_summary_after_restart")
        selected = ["tests/session/test_installed_repair_s5_restart.py::" + function]
    import pytest
    exit_code = 1
    try:
        exit_code = int(pytest.main([
            *selected, "-q", "-m", "not live", "--tb=short",
            "--basetemp", str(state / (args.mode + "-tmp")),
            "--junitxml", str(state / (args.mode + "-junit.xml")),
        ]))
        if blocked:
            exit_code = 1
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = originals
        result = {
            "mode": args.mode, "source": str(source), "pid": os.getpid(),
            "selected": selected, "exit_code": exit_code,
            "blocked_external_attempts": blocked, "real_network_sends": 0,
            "quality": "Unverified; isolated tests and synthetic controls",
        }
        (state / (args.mode + "-result.json")).write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
