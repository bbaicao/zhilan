"""源码结构及内存负对照；不证明编译、实际安装或断网运行通过。"""

from pathlib import Path
import re

import pytest


ISS = Path(__file__).resolve().parents[2] / "tools/packaging/zhilan.iss"
RUNTIME = "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"


def _check(source: str) -> None:
    source = source.replace("\r\n", "\n")
    files = source.split("\n[Files]\n", 1)[1].split("\n[Dirs]\n", 1)[0]
    run = source.split("\n[Run]\n", 1)[1].split("\n[", 1)[0]
    prepare = re.search(
        r"^function PrepareToInstall\(.*?(?=^function |^procedure |\Z)",
        source, re.MULTILINE | re.DOTALL,
    ).group()
    assert "MicrosoftEdgeWebview2Setup.exe" not in source
    assert f'#if !FileExists(MyVendorDir + "\\{RUNTIME}")' in source
    assert files.count(f'Source: "{{#MyVendorDir}}\\{RUNTIME}"') == 1
    assert "Flags: dontcopy noencryption" in files
    assert files.index(RUNTIME) < files.index("MyPayloadDir")
    assert RUNTIME not in run
    assert prepare.index("ValidateInstallDir(") < prepare.index("NeedsWebView2()")
    assert re.search(
        r"if not ValidateInstallDir\(Dir, False, Reason\) then\s+begin\s+"
        r"Result := Reason;\s+Exit;", prepare,
    )
    assert re.search(r"if not NeedsWebView2\(\) then\s+begin.*?Exit;", prepare, re.DOTALL)
    assert f"ExtractTemporaryFile('{RUNTIME}')" in prepare
    assert "except\n    Result := '无法解出" in prepare
    assert f"if not Exec(ExpandConstant('{{tmp}}\\{RUNTIME}')," in prepare
    assert "'/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode)" in prepare
    assert "SysErrorMessage(ResultCode)" in prepare
    assert re.search(
        r"if ResultCode = 3010 then\s+begin\s+NeedsRestart := True;\s+"
        r"Result := '[^']+';\s+Log\(Result\);\s+Exit;", prepare,
    )
    assert re.search(
        r"if ResultCode <> 0 then\s+begin\s+Result := '[^']+' \+ IntToStr\(ResultCode\);"
        r"\s+Log\(Result\);\s+Exit;", prepare,
    )
    assert re.search(
        r"if NeedsWebView2\(\) then\s+begin\s+Result := '[^']+';"
        r"\s+Log\(Result\);\s+Exit;", prepare,
    )
    assert prepare.index("if ResultCode <> 0") < prepare.index("if NeedsWebView2() then")
    assert "if Result = '0.0.0.0' then Result := '';" in source
    assert "#ifdef MutateSkipWebView2Check" in source


def test_offline_runtime_precedes_application_install() -> None:
    raw = ISS.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    _check(raw.decode("utf-8-sig"))


@pytest.mark.parametrize("before,after", [
    ("SW_HIDE, ewWaitUntilTerminated", "SW_HIDE, ewNoWait"),
    ("if ResultCode <> 0 then", "if False then"),
    ("if NeedsWebView2() then", "if False then"),
    ("NeedsRestart := True;", "NeedsRestart := False;"),
    ("Result := Reason;\n    Exit;", "Result := Reason;"),
])
def test_offline_runtime_negative_controls(before: str, after: str) -> None:
    source = ISS.read_text(encoding="utf-8-sig")
    assert source.count(before) == 1
    with pytest.raises(AssertionError):
        _check(source.replace(before, after, 1))
