#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""知澜 · 离线模型包完整性清单工具（阶段 6 · 条目 6.4 交付物 3）。

一句话：**给「官方 ONNX 模型件」发身份**——`gen` 算清单，`verify` 按清单校验。

为什么自建而不是用 MinerU 的 `models verify`
-----------------------------------------
MinerU 自带的 `models verify` 查的是**完成标记**（`.mineru_complete`），不是 sha256
（上游 `model/download.py` 的 `verify` 一节）。标记只能说明「下载过程跑完了」，
说明不了「手上的字节和发布时一致」。用户从网盘/镜像拿到模型包后要能自己核对，
就必须有**逐文件 sha256**。故本清单自建，MinerU 的输出只当旁证。

范围（Q2-a / 阶段 6 §九 Q4 定案）
--------------------------------
**只覆盖官方 ONNX 件**（`OpenDataLab/MinerU-4_models_onnx`，13 件 / 858,204,914 字节）。
**不含**第三方 GGUF、**不含** `mineru_llama_cpp` 的任何文件——那两样按定案不进交付包，
自然也不该有身份。所以 `gen` 一旦在模型根下看到它们，**直接拒绝生成**（退出码 2），
而不是悄悄跳过：悄悄跳过会让清单看起来「干净」，而根目录其实脏。

纪律
----
- **只 stdlib、零出网、零子进程**：连 `socket` / `urllib` 都不 import（有回归用例钉住）。
- **不 import 任何产品模块**：本脚本属**分发侧工具**，不进产品包、也不依赖 `backend/`。
- 退出码与 `6.2c` 的闸门同口径：**0 = 通过 / 1 = 校验失败 / 2 = 输入或读取错误**。

用法
----
    python tools/packaging/models_manifest.py gen    --root <模型根> --out tools/packaging/models-manifest.txt
    python tools/packaging/models_manifest.py verify --root <模型根> --manifest tools/packaging/models-manifest.txt

`<模型根>` = 官方 ONNX 件所在目录（形如 `<安装目录>\\models\\MinerU-4_models_onnx`），
清单里的相对路径**都相对它**——这样同一份清单在构建机与用户机上都可直接校验。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from datetime import datetime, timezone, timedelta

# --------------------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------------------
EXIT_OK = 0
EXIT_VERIFY_FAILED = 1
EXIT_INPUT_ERROR = 2

SOURCE_REPO = "OpenDataLab/MinerU-4_models_onnx"

#: 清单行：`<sha256><两空格><相对路径><两空格><字节数>`。
#: 用正则而不是 `split()`——相对路径里**可能含空格**（文件名不做保证），
#: 而 sha256 恰好 64 位十六进制、字节数是纯数字，两端锚死就无歧义。
_LINE_RE = re.compile(r"^(?P<sha>[0-9a-fA-F]{64}) {2}(?P<path>.+?) {2}(?P<size>\d+)$")

#: 绝对不该进交付包的件（命中即拒绝生成清单，见模块 docstring「范围」）。
_FORBIDDEN_SUFFIXES = (".gguf",)
_FORBIDDEN_NAMES = (".mineru_complete", ".gitattributes")
_FORBIDDEN_SUBSTR = "mineru_llama_cpp"

_CN_TZ = timezone(timedelta(hours=8))


def _die(message: str) -> int:
    """输入/读取类错误：打印中文原因后退 2。"""
    print(f"[错误] {message}", file=sys.stderr)
    return EXIT_INPUT_ERROR


def _sha256_of(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _forbidden_reason(rel_posix: str) -> str | None:
    """相对路径命中禁进包件时返回中文原因，否则 None。"""
    name = rel_posix.rsplit("/", 1)[-1].lower()
    if _FORBIDDEN_SUBSTR in rel_posix.lower():
        return f"路径含 `{_FORBIDDEN_SUBSTR}`"
    if name.endswith(_FORBIDDEN_SUFFIXES):
        return "扩展名命中禁进包类型（.gguf）"
    if name in _FORBIDDEN_NAMES:
        return f"文件名 `{name}` 属清单外杂项"
    return None


def _walk_files(root: str):
    """列出 `root` 下的全部文件（含隐藏），返回 [(相对路径 posix, 绝对路径, 字节数)]，按相对路径排序。

    用 `os.walk` 而不是 `Path.rglob`：junction / 目录符号链接在 Windows 上对 `os.walk`
    是透明的（`6.4` 的「有模型包」侧正是用 junction 造 ONNX-only 树），语义可控；
    同时 `followlinks=False` 保证**嵌套**的链接不会绕出环来。
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for filename in sorted(filenames):
            abspath = os.path.join(dirpath, filename)
            rel = os.path.relpath(abspath, root).replace(os.sep, "/")
            try:
                size = os.path.getsize(abspath)
            except OSError as exc:
                raise OSError(f"读取不到文件大小：{rel}（{exc}）") from exc
            out.append((rel, abspath, size))
    out.sort(key=lambda item: item[0])
    return out


# --------------------------------------------------------------------------------------
# gen
# --------------------------------------------------------------------------------------
def cmd_gen(args) -> int:
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        return _die(f"模型根不是一个目录：{root}")

    files = _walk_files(root)

    forbidden = [(rel, reason) for rel, _abs, _size in files if (reason := _forbidden_reason(rel))]
    if forbidden:
        print("[错误] 模型根里出现了**不得进交付包**的件，拒绝生成清单：", file=sys.stderr)
        for rel, reason in forbidden[:20]:
            print(f"        - {rel}（{reason}）", file=sys.stderr)
        if len(forbidden) > 20:
            print(f"        …（共 {len(forbidden)} 项，只列前 20）", file=sys.stderr)
        print(
            "        说明：本清单只覆盖官方 ONNX 件（Q2-a / §九 Q4）。"
            "请把 --root 指向官方 ONNX 件所在目录（形如 <模型根>/MinerU-4_models_onnx）。",
            file=sys.stderr,
        )
        return EXIT_INPUT_ERROR

    if not files:
        return _die(f"模型根下没有任何文件：{root}（清单会是空的，拒绝生成）")

    lines = []
    for rel, abspath, size in files:
        try:
            digest = _sha256_of(abspath)
        except OSError as exc:
            return _die(f"读取不到文件：{rel}（{exc}）")
        lines.append(f"{digest}  {rel}  {size}")

    total = sum(size for _rel, _abs, size in files)
    generated_at = args.generated_at or datetime.now(_CN_TZ).isoformat(timespec="seconds")

    # ⚠️ 文件头**不写本机绝对路径**（仓库纪律：本机路径零容忍）——`--root` 一律写成占位符。
    header = [
        "# 知澜 · 离线模型包完整性清单（阶段 6 · 条目 6.4 交付物 2）",
        f"# 来源仓库：{SOURCE_REPO}（官方件）",
        f"# 生成时间：{generated_at}",
        "# 生成命令：python tools/packaging/models_manifest.py gen --root <模型根>"
        " --out tools/packaging/models-manifest.txt",
        "# 校验命令：python tools/packaging/models_manifest.py verify --root <模型根>"
        " --manifest tools/packaging/models-manifest.txt",
        "#",
        "# 声明：本清单**只覆盖官方 ONNX 件**，不含任何 .gguf、不含 mineru_llama_cpp、",
        "#       不含第三方再分发件（依据 Q2-a 与 阶段6/设计说明 §九 Q4 定案）。",
        f"# 件数：{len(files)} 件；总字节：{total}",
        "# 格式：<sha256><两个空格><相对路径><两个空格><字节数>（相对路径相对于模型根）",
    ]
    text = "\n".join(header + lines) + "\n"

    out_path = os.path.abspath(args.out)
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)

    print("知澜 · 离线模型包完整性清单（生成）")
    print(f"模型根   ：{root}")
    print(f"清单     ：{out_path}")
    print(f"件数     ：{len(files)} 件")
    print(f"总字节   ：{total:,}")
    print(f"来源仓库 ：{SOURCE_REPO}")
    for line in lines:
        print(f"  {line}")
    print(f"结论：已生成（退出码 {EXIT_OK}）")
    return EXIT_OK


# --------------------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------------------
def _parse_manifest(manifest_path: str):
    """解析清单，返回 (entries, problems)。entries = [(sha, rel, size)]。

    只跳过空行与 `#` 注释行；**任何**其它形态的行都判为清单损坏（退 2）——
    校验器的第一职责是「别拿一份坏清单给出绿结论」。
    """
    entries = []
    problems = []
    with open(manifest_path, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.rstrip("\n").rstrip("\r")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            m = _LINE_RE.match(line)
            if m is None:
                problems.append(f"第 {lineno} 行不是合法清单行（应为 `<sha256>  <相对路径>  <字节数>`）：{line!r}")
                continue
            entries.append((m.group("sha").lower(), m.group("path"), int(m.group("size"))))
    return entries, problems


def cmd_verify(args) -> int:
    root = os.path.abspath(args.root)
    manifest_path = os.path.abspath(args.manifest)

    if not os.path.isdir(root):
        return _die(f"模型根不是一个目录：{root}")
    if not os.path.isfile(manifest_path):
        return _die(f"清单文件不存在：{manifest_path}")

    try:
        entries, parse_problems = _parse_manifest(manifest_path)
    except OSError as exc:
        return _die(f"读不到清单：{manifest_path}（{exc}）")
    if parse_problems:
        print("[错误] 清单本身不合法：", file=sys.stderr)
        for item in parse_problems[:20]:
            print(f"        - {item}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    if not entries:
        return _die(f"清单里没有任何条目：{manifest_path}（空清单不能作为通过依据）")

    dup = sorted({rel for _sha, rel, _size in entries if [e[1] for e in entries].count(rel) > 1})
    if dup:
        print("[错误] 清单里有重复路径：" + ", ".join(dup[:10]), file=sys.stderr)
        return EXIT_INPUT_ERROR

    by_rel = {rel: (sha, size) for sha, rel, size in entries}

    try:
        on_disk = _walk_files(root)
    except OSError as exc:
        return _die(f"遍历模型根失败：{root}（{exc}）")
    disk_map = {rel: (abspath, size) for rel, abspath, size in on_disk}

    missing, extra, size_bad, hash_bad = [], [], [], []
    for rel in sorted(set(by_rel) | set(disk_map)):
        if rel not in disk_map:
            missing.append(rel)
            continue
        if rel not in by_rel:
            extra.append(rel)
            continue
        exp_sha, exp_size = by_rel[rel]
        abspath, got_size = disk_map[rel]
        if got_size != exp_size:
            size_bad.append(f"{rel}（清单 {exp_size:,} 字节 / 实测 {got_size:,} 字节）")
            continue
        try:
            got_sha = _sha256_of(abspath)
        except OSError as exc:
            return _die(f"读取不到文件：{rel}（{exc}）")
        if got_sha != exp_sha:
            hash_bad.append(f"{rel}（清单 {exp_sha[:16]}… / 实测 {got_sha[:16]}…）")

    total_expected = sum(size for _sha, _rel, size in entries)
    total_disk = sum(size for _rel, _abs, size in on_disk)

    print("知澜 · 离线模型包完整性校验（阶段 6 · 条目 6.4）")
    print(f"模型根   ：{root}")
    print(f"清单     ：{manifest_path}")
    print(f"清单条目 ：{len(entries)} 条（总字节 {total_expected:,}）")
    print(f"实测文件 ：{len(on_disk)} 个（总字节 {total_disk:,}）")
    print("")

    def _report(title, items, hint):
        if not items:
            print(f"[ OK ] {title}：0")
            return
        print(f"[FAIL] {title}：{len(items)}")
        for item in items[:20]:
            print(f"        - {item}（{hint}）")
        if len(items) > 20:
            print(f"        …（共 {len(items)} 项，只列前 20）")

    _report("缺件（清单里有、盘上没有）", missing, "模型包不完整，请重新下载/解压")
    _report("多件（盘上有、清单里没有）", extra, "非官方件或多余文件，请核实后再用")
    _report("哈希不符", hash_bad, "文件内容与发布时不一致，请重新下载")
    _report("字节数不符", size_bad, "文件不完整，请重新下载")

    failed = bool(missing or extra or hash_bad or size_bad)
    if failed:
        print("")
        print(f"结论：FAIL（退出码 {EXIT_VERIFY_FAILED}）——模型包与清单不一致")
        return EXIT_VERIFY_FAILED
    print("")
    print(f"结论：PASS（退出码 {EXIT_OK}）——{len(entries)} 件逐文件 sha256 与清单逐字一致")
    return EXIT_OK


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="models_manifest.py",
        description="知澜离线模型包完整性清单工具（阶段 6 · 条目 6.4；只 stdlib、零出网）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("gen", help="对已有模型目录计算 sha256 清单")
    p_gen.add_argument("--root", required=True, help="官方 ONNX 件所在目录")
    p_gen.add_argument("--out", required=True, help="清单输出路径")
    p_gen.add_argument(
        "--generated-at",
        default=None,
        help="覆盖「生成时间」字段（ISO8601）；只为可复现生成，正常不必传",
    )
    p_gen.set_defaults(func=cmd_gen)

    p_verify = sub.add_parser("verify", help="按清单校验模型目录（缺件/多件/哈希不符/字节数不符）")
    p_verify.add_argument("--root", required=True, help="官方 ONNX 件所在目录")
    p_verify.add_argument("--manifest", required=True, help="清单文件路径")
    p_verify.set_defaults(func=cmd_verify)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("[错误] 被中断", file=sys.stderr)
        return EXIT_INPUT_ERROR


if __name__ == "__main__":
    sys.exit(main())
