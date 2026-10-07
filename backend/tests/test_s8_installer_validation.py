"""S8 · 条目 181：静默安装（/SILENT、/VERYSILENT）下受保护目录必须 fail-closed，且中止原因必须精确。

缺陷（修前 tools/packaging/zhilan.iss）：
* ValidateInstallDir 对「受保护目录」只弹 Yes/No，且弹框被 Interactive 参数把门；PrepareToInstall 传
  Interactive=False ⇒ /SILENT、/VERYSILENT 一路放行，可以把产品装进 Program Files / Windows / System
  目录，全程没有任何提示。
* PrepareToInstall 失败时另写一份硬编码拒因，原文只讲「末级目录名必须是纯 ASCII」；于是受保护目录
  被拒时，用户看到的原因却是「含中文」——原因与事实不符（条目 181 的核心症状）。

本文件的判据（修后必须满足）：
1. 静默判据是真实安装模式 WizardSilent，不是 Interactive 参数。交互安装里 PrepareToInstall 同样传
   False；拿参数当静默判据，会把「用户已在目录页确认过 Yes」的安装拦死在最后一步 —— 那是未经授权
   改变既有产品行为。
2. 静默 + 受保护目录 ⇒ 必须给出非空的中文拒绝原因并中止（fail-closed）。
3. 受保护目录的拒因不得说「含中文 / 非 ASCII」；末级非 ASCII 的拒因不得说「受保护」。
4. 双违例时优先级固定：先判末级 ASCII，再判受保护目录。
5. 交互态的 Yes/No 与原末级 ASCII 政策保持不变。
6. 末级 ASCII 判据仍只针对最后一级目录名（ExtractFileName），不得扩大成整条路径。

判别力边界：
* 本文件判的是源码文本结构 + 内存副本负对照（定点变异必须命中对应判据，而不是「随便改坏就红」）。
* 它不证明真机安装行为。行为判据 = 用本机真实 Inno Setup 6.7.3 编译后，再用 /SILENT、/VERYSILENT 对
  受保护目录检查真实非零退出及产品文件未落下，才能证明真实安装行为。
* 源仓零写入：变异只在内存里做。要判字节副本时用环境变量指过去：
    ZHILAN_ISS              —— 静态判据的被判对象（默认仓库源码）
    ZHILAN_ISS_PREFIX_REF   —— 修前字节副本（给「修前红」腿用）
    ZHILAN_ISS_MUTANT_BASE  —— 负对照的基准文本（默认仓库源码；可指向修后副本）
* 不写「零回归」；与基线的差异按双向差集记录。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ISS = REPO / "tools" / "packaging" / "zhilan.iss"
BOM = bytes([0xEF, 0xBB, 0xBF])

FN_VALIDATE = "function ValidateInstallDir("
FN_NEXT = "function NextButtonClick("
FN_PREPARE = "function PrepareToInstall("
FN_ISPROT = "function IsProtectedDir("
FN_ISUNDER = "function IsUnder("

PROTECTED_IDS = ("{commonpf}", "{commonpf32}", "{commonpf64}", "{win}", "{sys}")

PREFIX_HARDCODED_REASON = (
    "Result := '安装目录的末级目录名必须是纯 ASCII 目录名，不能含中文或其它非 ASCII 字符。"
    "当前是：' + Dir + '（程序路径含非 ASCII 字符会让内置原生库间歇性加载失败）。';"
)


def _iss_path() -> Path:
    override = os.environ.get("ZHILAN_ISS", "").strip()
    return Path(override) if override else ISS


def _read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig")


def _compact(text: str) -> str:
    return "".join(text.split())


def _clean(text: str) -> str:
    """去掉 Pascal 注释：整行 ; 注释与 { } 块注释（字符串字面量内部原样保留）。

    必须先去掉注释再判结构：否则「只改注释」或「注释里恰好写着 WizardSilent」就会骗过判据。
    """
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith(";"):
            continue
        lines.append(line)
    out = []
    in_string = False
    i = 0
    body = "\n".join(lines)
    while i < len(body):
        ch = body[i]
        if ch == "'":
            if in_string and i + 1 < len(body) and body[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            in_string = not in_string
            out.append(ch)
            i += 1
            continue
        if not in_string and ch == "{":
            j = body.find("}", i)
            if j < 0:
                break
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _block(code: str, header: str) -> str:
    """截取一条顶层声明的正文：从 header 行到下一条顶层声明（function/procedure/[）为止。"""
    lines = code.splitlines()
    for idx, line in enumerate(lines):
        if not line.startswith(header):
            continue
        for j in range(idx + 1, len(lines)):
            nxt = lines[j]
            if nxt.startswith("function ") or nxt.startswith("procedure ") or nxt.startswith("["):
                return "\n".join(lines[idx:j])
        return "\n".join(lines[idx:])
    raise LookupError("判据锚点缺失：" + header)


def _quoted_text(chunk: str) -> str:
    out = []
    in_string = False
    i = 0
    while i < len(chunk):
        ch = chunk[i]
        if ch == "'":
            if in_string and i + 1 < len(chunk) and chunk[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            in_string = not in_string
            i += 1
            continue
        if in_string:
            out.append(ch)
        i += 1
    return "".join(out)


def _reason_literals(region: str) -> str:
    """把区域里所有 Reason := ...; 赋值里出现的中文/文本字面量拼起来。"""
    out = []
    start = 0
    while True:
        i = region.find("Reason :=", start)
        if i < 0:
            break
        j = region.find(";", i)
        if j < 0:
            j = len(region)
        out.append(_quoted_text(region[i:j]))
        start = j
    return "".join(out)


def _judge(raw: str) -> list[str]:
    """返回违约判据标签（空列表 = 通过）。标签名稳定，供负对照点名。"""
    bad = set()
    try:
        validate = _block(raw, FN_VALIDATE)
        nextbutton = _block(raw, FN_NEXT)
        prepare = _block(raw, FN_PREPARE)
        isprot = _block(raw, FN_ISPROT)
        isunder = _block(raw, FN_ISUNDER)
    except LookupError as exc:
        return ["structure:" + str(exc)]

    validate_c = _clean(validate)
    prepare_c = _clean(prepare)

    if "var Reason: String" not in validate.splitlines()[0]:
        bad.add("sig_reason_out_param")
    if "Reason := ''" not in validate_c:
        bad.add("reason_reset")

    i_ascii = validate_c.find("if not IsAsciiOnly(")
    i_prot = validate_c.find("if IsProtectedDir(")
    if i_ascii < 0 or i_prot < 0:
        bad.add("branches_missing")
        return sorted(bad)
    if i_ascii > i_prot:
        bad.add("priority_ascii_first")
    ascii_region = validate_c[i_ascii:i_prot]
    prot_region = validate_c[i_prot:]

    if "ExtractFileName(" not in validate_c[:i_ascii] or "IsAsciiOnly(Name)" not in ascii_region:
        bad.add("ascii_scope")
    all_c = _compact(_clean(raw))
    if "IsAsciiOnly(Dir)" in all_c or "IsAsciiOnly(TrimTrailingSlashes(Dir))" in all_c:
        bad.add("ascii_scope_widened")
    if "Result := False" not in ascii_region:
        bad.add("ascii_enforced")
    if "必须是纯 ASCII" not in ascii_region:
        bad.add("ascii_prompt_wording")
    if "not WizardSilent" not in ascii_region:
        bad.add("ascii_dialog_not_gated")
    ascii_reason = _reason_literals(ascii_region)
    if len(ascii_reason) < 10:
        bad.add("ascii_reason_missing")
    elif "ASCII" not in ascii_reason or "受保护" in ascii_reason:
        bad.add("ascii_reason_wrong_class")

    i_ref = prot_region.find("Result := False")
    if i_ref < 0:
        bad.add("silent_refusal_missing")
        before_ref = prot_region
    else:
        before_ref = prot_region[:i_ref]
        after_ref = prot_region[i_ref:]
        if "WizardSilent" not in before_ref:
            bad.add("silent_judge_not_wizardsilent")
        if "not Interactive" in before_ref:
            bad.add("silent_judge_uses_param")
        if "MsgBox" in before_ref:
            bad.add("silent_dialog_before_refusal")
        if "MsgBox(ProtectedMsg" not in after_ref:
            bad.add("silent_dialog_order")
    prot_reason = _reason_literals(before_ref)
    if len(prot_reason) < 10:
        bad.add("protected_reason_missing")
    elif "受保护" not in prot_reason or "中文" in prot_reason or "ASCII" in prot_reason:
        bad.add("protected_reason_wrong_class")
    if "受保护的位置" not in prot_region:
        bad.add("protected_prompt_wording")

    if "ValidateInstallDir(" not in prepare_c or "Reason" not in prepare_c:
        bad.add("prepare_calls_validator")
    if "Result := Reason" not in prepare_c:
        bad.add("prepare_returns_shared_reason")
    if "纯 ASCII" in prepare_c:
        bad.add("prepare_hardcoded_reason")

    nb_c = _compact(_clean(nextbutton))
    if "ValidateInstallDir(" not in nb_c or "Reason" not in nb_c:
        bad.add("nextbutton_shares_reason")
    if ",True," not in nb_c or "WizardDirValue()" not in nb_c:
        bad.add("nextbutton_interactive")

    prot_c = _compact(_clean(isprot))
    for pid in PROTECTED_IDS:
        if pid not in prot_c:
            bad.add("protected_ids:" + pid)

    iu_c = _compact(_clean(isunder))
    if "Lowercase(" not in iu_c or "TrimTrailingSlashes(" not in iu_c:
        bad.add("isunder_case_slash")
    if "(D=P)" not in iu_c or "Length(P)+1" not in iu_c:
        bad.add("isunder_equality")

    for needle in ("GetFileAttributes", "GetFileSecurity", "CreateFileW"):
        if needle in raw:
            bad.add("new_framework")
            break

    return sorted(bad)


def _mutate(text: str, edits) -> str:
    for old, new in edits:
        if old not in text:
            raise AssertionError("负对照锚点缺失：" + old)
        text = text.replace(old, new, 1)
    return text


def test_source_passes_all_static_judgments() -> None:
    path = _iss_path()
    violations = _judge(_read_text(path))
    assert violations == [], (
        str(path) + " 违反 181 静态判据：" + ", ".join(violations)
        + "（条目 181：静默安装下受保护目录必须 fail-closed，且中止原因必须与事实同类）"
    )


def test_iss_is_utf8_with_bom() -> None:
    raw = ISS.read_bytes()
    assert raw.startswith(BOM), (
        "tools/packaging/zhilan.iss 必须以 UTF-8 with BOM 保存（文件头既有契约）。"
        "注：修前原件是 UTF-8 无 BOM（首 3 字节 3b 20 3d），本条目按要求改为带 BOM；"
        "这条编码差异已登记在 证据/_s8-20261003-stage/installer/181 状态与证据.md"
        "（本机 Inno Setup 引擎 6.7.3 其实也支持无 BOM，故这是口径差而非必需）。"
    )
    raw[len(BOM):].decode("utf-8")


def test_prefix_source_is_red() -> None:
    override = os.environ.get("ZHILAN_ISS_PREFIX_REF", "").strip()
    if override:
        text = _read_text(Path(override))
        origin = "修前字节副本 " + override
    else:
        text = _mutate(
            _read_text(ISS),
            [
                ("if WizardSilent then", "if False then"),
                ("Result := Reason;", PREFIX_HARDCODED_REASON),
            ],
        )
        origin = "内存重建（去掉静默拒绝可达性 + 恢复硬编码 ASCII 拒因）"
    violations = _judge(text)
    for tag in ("silent_judge_not_wizardsilent", "prepare_returns_shared_reason"):
        assert tag in violations, (
            origin + " 未命中判据 " + tag + "；实际：" + ", ".join(violations)
        )


MUTANTS = [
    (
        "M1-remove-silent-refusal",
        "去掉静默拒绝：受保护目录在静默安装下重新放行（修前症状）",
        "if WizardSilent then",
        "if False then",
        ["silent_judge_not_wizardsilent"],
    ),
    (
        "M2-restore-hardcoded-reason",
        "中止原因回到 PrepareToInstall 里另写硬编码 ASCII 文案（受保护目录被报成含中文）",
        "Result := Reason;",
        PREFIX_HARDCODED_REASON,
        ["prepare_returns_shared_reason", "prepare_hardcoded_reason"],
    ),
    (
        "M3-param-as-silent-judge",
        "拿 Interactive 参数当静默判据（交互安装里 PrepareToInstall 也传 False ⇒ 用户在目录页确认 Yes 后仍被拦死）",
        "if WizardSilent then",
        "if not Interactive then",
        ["silent_judge_not_wizardsilent", "silent_judge_uses_param"],
    ),
    (
        "M4-dialog-loses-silent-gate",
        "末级 ASCII 提示弹框失去静默门（/SILENT 下弹框 = 无人可答）",
        "if Interactive and not WizardSilent then",
        "if Interactive then",
        ["ascii_dialog_not_gated"],
    ),
    (
        "M5-ascii-check-widens-to-full-path",
        "末级 ASCII 判据扩大到整条路径（越权收紧既有政策）",
        "if not IsAsciiOnly(Name) then",
        "if not IsAsciiOnly(Dir) then",
        ["ascii_scope", "ascii_scope_widened"],
    ),
]


@pytest.mark.parametrize(
    "label,desc,old,new,expected", MUTANTS, ids=[m[0] for m in MUTANTS]
)
def test_negative_control_mutant_is_red(label, desc, old, new, expected) -> None:
    base = os.environ.get("ZHILAN_ISS_MUTANT_BASE", "").strip()
    text = _read_text(Path(base)) if base else _read_text(ISS)
    violations = _judge(_mutate(text, [(old, new)]))
    missing = [tag for tag in expected if tag not in violations]
    assert not missing, (
        label + "（" + desc + "）未命中判据 " + ", ".join(missing)
        + "；实际：" + ", ".join(violations)
    )
