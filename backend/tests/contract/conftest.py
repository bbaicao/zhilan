"""契约测试集 conftest：双目标录制/回放 + 被测服务**自动探测**。

用法（**不指定目标即可跑**）：
    pytest tests/contract -q

    # 阳性对照（真起服务 · 真跑，不是 skip）—— 推荐用一条命令起停：
    pwsh -File backend/scripts/run_contract_live.ps1
    # 等价的裸命令形态：
    python -m uvicorn main:app --port 8021
    CONTRACT_TARGET=v2 BASE_URL=http://127.0.0.1:8021 pytest tests/contract -q -rfsE

    # 对老系统录制基线（显式 v1）
    CONTRACT_TARGET=v1 pytest tests/contract -q

环境变量：
    CONTRACT_TARGET: "v1"（老系统）/ "v2"（新系统）/ 未设 = **自动探测**
        显式给出时**原样尊重**，不因探测结果改写；未设或写成别的值时走自动探测。
    BASE_URL: 覆盖默认服务地址（给了就**优先探测它**；显式给出时不改目标）
        v1 未给 BASE_URL ⇒ 试 http://127.0.0.1:8010
        v2 未给 BASE_URL ⇒ 依次试 http://127.0.0.1:8021 → http://127.0.0.1:8000

🔴 自动探测口径（回归检查 · 拍板 #1-C）：
    · **有**知澜服务 ⇒ 按端口约定判目标（8021/8000 = v2；8010 = v1），live 腿**真跑**；
    · **无**服务 / 服务不是知澜（`GET /openapi.json` 非 200、非 JSON、或缺指纹路径）
      ⇒ 依赖服务的 fixture **显式 `pytest.skip(原因)`** —— 不再以连接错误的形式产出
      55 errors + 12 failed；原因行经 `-rs` 出现在汇总里，另有一行
      `CONTRACT-PROBE: …` 打在报告头与汇总区（**汇总可见**）。
    · ⛔ `skip` 只说明「本机没起被测服务」，**不说明契约已通过**：阳性对照必须真起服务。
    · 探测维度 = 端口约定 + 知澜指纹路径子集。⚠️ **指纹只能证明「这是知澜服务」**，
      不能证明它属于哪个仓库（v1/v2 两树路由集实测逐条相同）⇒「是哪一版」仍依端口约定。

静态源码路径（v1 录制时用于源码级核对，不依赖跑服务）：
    OLD_REPO_BACKEND: 老仓库 backend 根目录——**仓库外部的可选依赖**，
        按「环境变量 `OLD_REPO_BACKEND` → `.env` 的 `OLD_REPO_BACKEND` → 空」解析
        （`.env` 永不入库，故仓库里不写死任何本机绝对路径）。
        未配置时 `old_repo_backend` fixture 直接 skip，而不是让用例报 FileNotFoundError。
    V2_REPO_BACKEND: 本仓库 backend 根目录。默认由本文件位置推导（`tests/contract/conftest.py`
        → `parents[2]`），可用环境变量 `V2_REPO_BACKEND` 覆盖。
"""

import os
from pathlib import Path

import pytest

from app.core.config import settings

# --- 服务自动探测（回归检查）---------------------------------------------------
# 端口约定：v2 = 8021（本仓验收约定口）/ 8000（.env 的 PORT）；v1 老仓 = 8010。
_PORT_TARGETS = ((8021, "v2"), (8000, "v2"), (8010, "v1"))
_TARGET_PORTS = {"v1": (8010,), "v2": (8021, 8000)}
_DEFAULT_BASE_URL = {"v1": "http://127.0.0.1:8010", "v2": "http://127.0.0.1:8000"}
# 知澜指纹：v1/v2 共有的核心路径（两树路由集实测逐条相同 ⇒ 只能证明「是知澜」）。
_FINGERPRINT_PATHS = (
    "/api/health",
    "/api/notes",
    "/api/documents",
    "/api/chats",
    "/api/settings",
    "/api/agent/audit",
    "/api/search",
    "/api/graph",
    "/api/trash",
    "/api/knowledge-bases",
    "/api/chat/stream",
    "/api/translate",
    "/api/chat-groups",
    "/api/wiki/backlinks",
    "/api/wiki/related",
)
_PROBE_TIMEOUT = 2.0


def _probe_service(base_url: str) -> tuple[str, str]:
    """探测一个地址：返回 `(status, reason)`，status ∈ {"UP", "ABSENT"}。

    判据 = `GET /openapi.json` 200 + JSON + 含知澜指纹路径子集。
    `trust_env=False` 同 `http_client`（回归修复：本机代理会拦 127.0.0.1）。
    """
    import httpx

    try:
        with httpx.Client(
            base_url=base_url, timeout=_PROBE_TIMEOUT, trust_env=False
        ) as client:
            resp = client.get("/openapi.json")
    except Exception as exc:  # 连接被拒 / 超时 / DNS 等 —— 一律视为「没有可用服务」
        return "ABSENT", f"{type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        return "ABSENT", f"GET /openapi.json → HTTP {resp.status_code}（不是知澜后端）"
    try:
        spec = resp.json()
    except Exception as exc:
        return "ABSENT", f"/openapi.json 不是 JSON（{type(exc).__name__}: {exc}）"
    paths = set((spec.get("paths") or {}).keys())
    missing = [p for p in _FINGERPRINT_PATHS if p not in paths]
    if missing:
        return "ABSENT", (
            "OpenAPI 缺知澜指纹路径 "
            f"{len(missing)}/{len(_FINGERPRINT_PATHS)}（首个缺 {missing[0]}）"
        )
    return "UP", f"命中知澜指纹路径 {len(_FINGERPRINT_PATHS)}/{len(_FINGERPRINT_PATHS)}"


class _Resolved:
    """探测结论（session 级，只解析一次）。"""

    def __init__(
        self,
        target: str,
        base_url: str,
        status: str,
        reason: str,
        probes: list[str],
        mode: str,
    ) -> None:
        self.target = target
        self.base_url = base_url
        self.status = status
        self.reason = reason
        self.probes = probes
        self.mode = mode

    @property
    def is_up(self) -> bool:
        return self.status == "UP"

    @property
    def skip_reason(self) -> str:
        detail = "；".join(self.probes) if self.probes else "（未做探测）"
        return (
            f"未探测到可用的知澜被测服务（{self.mode} · target={self.target}）"
            f"⇒ 依赖服务的契约腿**显式 skip**（⛔ 不代表契约已过）。探测明细：{detail}。"
            " 起真服务后重跑：pwsh -File backend/scripts/run_contract_live.ps1"
            "（或 CONTRACT_TARGET=v2 BASE_URL=http://127.0.0.1:8021 "
            "pytest tests/contract -q -rfsE）"
        )

    def header_lines(self) -> list[str]:
        head = (
            f"CONTRACT-PROBE: mode={self.mode} target={self.target} "
            f"base_url={self.base_url or '-'} service={self.status} reason={self.reason}"
        )
        return [head] + [f"  probe: {line}" for line in self.probes]


def _resolve_target() -> _Resolved:
    """解析被测目标：显式环境变量优先，否则按端口约定自动探测。"""
    raw_target = (os.environ.get("CONTRACT_TARGET") or "").strip()
    explicit = raw_target.lower()
    if explicit and explicit not in _TARGET_PORTS:
        explicit = ""  # 未知取值 ⇒ 按自动探测处理（不静默打到一个猜出来的端口）
    base_env = (os.environ.get("BASE_URL") or "").strip()

    if explicit:
        mode = f"显式 CONTRACT_TARGET={explicit}"
        target_guess = explicit
        urls = ([base_env] if base_env else []) + [
            f"http://127.0.0.1:{port}" for port in _TARGET_PORTS[explicit]
        ]
        fallback_url = base_env or _DEFAULT_BASE_URL[explicit]
        fallback_target = explicit
    else:
        mode = "自动探测（CONTRACT_TARGET 未设）"
        urls = []
        if base_env:
            urls.append(base_env)
        urls += [f"http://127.0.0.1:{port}" for port, _ in _PORT_TARGETS]
        fallback_url = base_env or _DEFAULT_BASE_URL["v2"]
        fallback_target = "v2"

    probes: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        status, reason = _probe_service(url)
        probes.append(f"{url} → {status}（{reason}）")
        if status == "UP":
            if explicit:
                target = target_guess
            else:
                port = url.rsplit(":", 1)[-1].split("/")[0].strip()
                target = next(
                    (t for p, t in _PORT_TARGETS if str(p) == port), "v2"
                )
            return _Resolved(target, url, "UP", reason, probes, mode)
    return _Resolved(
        fallback_target,
        fallback_url,
        "ABSENT",
        "以上候选地址均未探到知澜服务",
        probes,
        mode,
    )


_RESOLVED = _resolve_target()


def _emit_probe_lines() -> None:
    """把探测结论**直接**打到终端（导入期 = 收集期，保证汇总可见）。

    为什么不止靠钩子：只跑 `pytest tests` 时子目录 conftest 的 `pytest_report_header`
    可能赶不上首屏，`pytest_terminal_summary` 又只在收尾打印 ⇒ 这里补一次，
    再配上那两个钩子，**跑法怎么变都能在日志里看到 probe 行**。
    """
    lines = _RESOLVED.header_lines()
    reporter = None
    try:
        from _pytest.config import get_config

        reporter = get_config().pluginmanager.get_plugin("terminalreporter")
    except Exception:  # 非 pytest 环境（直接 import 本模块）时退化为普通打印
        reporter = None
    if reporter is not None:
        for line in lines:
            reporter.write_line(line)
    else:
        for line in lines:
            print(line)


_emit_probe_lines()

# 兼容旧名（本模块重构前后都对外暴露这两个常量）。
CONTRACT_TARGET = _RESOLVED.target
BASE_URL = _RESOLVED.base_url

# 老仓库在**仓库之外**，属可选依赖：环境变量优先（跨机临时覆盖），其次 .env（本机常驻配置）。
_OLD_REPO_BACKEND = (
    os.environ.get("OLD_REPO_BACKEND") or settings.OLD_REPO_BACKEND or ""
).strip()

OLD_REPO_BACKEND = Path(_OLD_REPO_BACKEND)
# v2 仓库根由本文件位置推导，机器无关；留环境变量覆盖口子便于临时指向别处。
V2_REPO_BACKEND = Path(
    os.environ.get("V2_REPO_BACKEND") or Path(__file__).resolve().parents[2]
)


def pytest_report_header(config):
    """把探测结论打在报告头（**汇总可见**）。"""
    return _RESOLVED.header_lines()


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """再打一份到汇总区 —— 只跑 `pytest tests` 时子目录 conftest 的头钩子可能赶不上。"""
    for line in _RESOLVED.header_lines():
        terminalreporter.write_line(line)


@pytest.fixture(scope="session")
def contract_service() -> dict:
    """探测结论（供复核者/用例读取；键：mode/target/base_url/status/reason/probes）。"""
    return {
        "mode": _RESOLVED.mode,
        "target": _RESOLVED.target,
        "base_url": _RESOLVED.base_url,
        "status": _RESOLVED.status,
        "reason": _RESOLVED.reason,
        "probes": list(_RESOLVED.probes),
    }


@pytest.fixture(scope="session")
def contract_target() -> str:
    return _RESOLVED.target


@pytest.fixture(scope="session")
def base_url() -> str:
    return _RESOLVED.base_url


@pytest.fixture(scope="session")
def old_repo_backend() -> Path:
    """老仓库 backend 根目录（**可选依赖**）。

    未配置时 skip：源码级核对用例在没有老仓库的机器上本就没法跑，
    报 FileNotFoundError 只会让人误以为仓库坏了。
    """
    if not _OLD_REPO_BACKEND:
        pytest.skip(
            "未配置老仓库路径（可选依赖）：请在未入库的 .env 里设 "
            "OLD_REPO_BACKEND=<老仓库 backend 根目录>，或设同名环境变量后重跑。"
        )
    return OLD_REPO_BACKEND


@pytest.fixture(scope="session")
def v2_repo_backend() -> Path:
    return V2_REPO_BACKEND


@pytest.fixture(scope="session")
def backend_root(contract_target: str, old_repo_backend: Path, v2_repo_backend: Path) -> Path:
    """源码级核对时按目标选根目录：v1 老仓库 / v2 新仓库。"""
    return old_repo_backend if contract_target == "v1" else v2_repo_backend


@pytest.fixture(scope="session")
def http_client():
    """httpx 同步客户端（session 级，复用连接）。

    ``trust_env=False`` 是**必需**的（回归修复）：httpx 默认读环境/系统代理
    （Windows 上 ``urllib.getproxies()`` 会读注册表里的 WinINET 设置），本机开着
    代理软件时，打 ``127.0.0.1`` 的请求会被代理拦下并返回 **502 Bad Gateway**——
    表现为「契约测试全红」，而真相是请求压根没到被测服务，排障方向完全被带偏。
    契约测试的目标永远是**本机被测服务**，因此显式绕开代理，不依赖环境变量。

    🔴 回归检查：本机**没有**可用知澜服务时**显式 skip**（原因见 `_RESOLVED.skip_reason`），
    不再让 12 条 live 腿报连接错误、也不让 `openapi_schema` 抛 55 个 error。
    需要真跑请先起服务（`backend/scripts/run_contract_live.ps1`）。
    """
    if not _RESOLVED.is_up:
        pytest.skip(_RESOLVED.skip_reason)
    import httpx

    with httpx.Client(base_url=_RESOLVED.base_url, timeout=30.0, trust_env=False) as client:
        yield client


@pytest.fixture(scope="session")
def openapi_schema(http_client):
    """拉取 /openapi.json，供端点表驱动比对。"""
    resp = http_client.get("/openapi.json")
    resp.raise_for_status()
    return resp.json()
