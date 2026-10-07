# -*- coding: utf-8 -*-
"""前端许可收集的最小离线检查；仅在指定 --out 新目录创建测试材料，无产品导入/网络/删除。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from collect_licenses import collect_frontend


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    root = parser.parse_args().out
    root.mkdir(parents=True, exist_ok=False)
    source = root / "frontend"
    package = source / "node_modules" / "@example" / "runtime"
    package.mkdir(parents=True)
    lock = {"lockfileVersion": 3, "packages": {
        "": {"dependencies": {"@example/runtime": "1.0.0"}},
        "node_modules/@example/runtime": {"version": "1.0.0", "license": "Apache-2.0",
            "resolved": "https://registry.npmjs.org/@example/runtime/-/runtime-1.0.0.tgz",
            "integrity": "sha512-synthetic"},
        "node_modules/build-only": {"version": "1.0.0", "dev": True},
        "node_modules/dev-optional": {"version": "1.0.0", "devOptional": True},
    }}
    lock_file = source / "package-lock.json"
    lock_file.write_text(json.dumps(lock), encoding="utf-8")
    meta = {"name": "@example/runtime", "version": "1.0.0", "license": "Apache-2.0"}

    def reject(label: str, expected: str) -> None:
        dest = root / label
        try:
            collect_frontend(source, dest)
        except SystemExit as exc:
            assert expected in str(exc), str(exc)
            assert not dest.exists(), f"{label} 留下了部分输出"
        else:
            raise AssertionError(f"{label} 未拒绝")
        print(f"PASS {label}")

    lock["lockfileVersion"] = 1
    lock_file.write_text(json.dumps(lock), encoding="utf-8")
    reject("lock-without-packages-format", "前端锁必须")
    lock["lockfileVersion"] = 3
    lock["packages"][""]["dependencies"]["missing-root"] = "1.0.0"
    lock_file.write_text(json.dumps(lock), encoding="utf-8")
    reject("missing-runtime-lock-entry", "前端锁缺少根 runtime 依赖")
    del lock["packages"][""]["dependencies"]["missing-root"]
    lock_file.write_text(json.dumps(lock), encoding="utf-8")
    reject("missing-package", "缺包")
    (package / "package.json").write_text(json.dumps(meta), encoding="utf-8")
    (package / "NOTICE").write_text("Copyright synthetic test fixture\n", encoding="utf-8")
    reject("notice-without-license", "缺许可全文")
    (package / "LICENSE").write_text(" \n", encoding="utf-8")
    reject("blank-license", "缺许可全文")
    # 从已有仓库全文取真实字节作有效控制，生产收集没有任何模板回退。
    license_bytes = (Path(__file__).resolve().parents[2] / "LICENSE").read_bytes()
    (package / "LICENSE").write_bytes(license_bytes)
    meta["version"] = "2.0.0"
    (package / "package.json").write_text(json.dumps(meta), encoding="utf-8")
    reject("version-mismatch", "版本不符")
    meta["version"] = "1.0.0"
    meta["name"] = "@example/other"
    (package / "package.json").write_text(json.dumps(meta), encoding="utf-8")
    reject("name-mismatch", "名称/版本不符")
    meta["name"] = "@example/runtime"
    (package / "package.json").write_text(json.dumps(meta), encoding="utf-8")
    lock["packages"]["node_modules/../escape"] = {"version": "1.0.0"}
    lock_file.write_text(json.dumps(lock), encoding="utf-8")
    reject("path-traversal", "非法 npm 依赖路径")
    del lock["packages"]["node_modules/../escape"]
    lock_file.write_text(json.dumps(lock), encoding="utf-8")
    dest = root / "valid"
    (package / "licenses").mkdir()
    (package / "licenses" / "COPYING").write_bytes(license_bytes)
    rows = collect_frontend(source, dest)
    manifest = json.loads((dest / "npm" / "00-package-manifest.json").read_text(encoding="utf-8"))
    record, = manifest["packages"]
    assert record["name"] == "@example/runtime" and record["version"] == "1.0.0"
    assert record["resolved"] == lock["packages"]["node_modules/@example/runtime"]["resolved"]
    assert record["integrity"] == "sha512-synthetic"
    assert manifest["package_lock_sha256"] == hashlib.sha256(lock_file.read_bytes()).hexdigest()
    assert (dest / "npm" / "@example" / "runtime" / "LICENSE").read_bytes() == license_bytes
    assert len(rows) == 4  # LICENSE、NOTICE、licenses/COPYING、manifest；dev 包不存在也不影响。
    for material in record["files"]:
        assert material["sha256"] == hashlib.sha256((dest / material["path"]).read_bytes()).hexdigest()
        assert material["source"] == "frontend/node_modules/@example/runtime/" + str(
            Path(material["path"]).relative_to("npm/@example/runtime")).replace("\\", "/")
    print("PASS valid-full-text-source-hashes-and-dev-exclusion")
    print("9 checks PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
