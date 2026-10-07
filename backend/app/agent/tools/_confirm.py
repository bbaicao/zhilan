"""写通道的**宿主侧可校验凭证**（专项工具升级-3 · 阶段 5 · 条目 `回归检查`）。

## 这个模块解决什么（红线 13 的落地）

红线 13 逐字：「**「用户已确认」的唯一判据必须是宿主侧可校验凭证**，
**不得存在只有模型能读到的确认通道**」。

⇒ 本模块是**从零设计**（实测：`backend/app` 内 `hmac` / `compare_digest` **均 0 命中**，全仓零先例）。
它提供**两个**函数：

- :func:`issue_write_confirm` —— **签发**（宿主侧调用）；
- :func:`verify_write_confirm` —— **校验**（写工具 handler 调用）。

另有一组 **per-token 临界区锁**（:func:`token_lock` / :func:`release_token_lock`，
`回归修复`）：由写工具 handler 把「verify → mark_used」包成**互斥临界区**，
堵「同一张凭证并行两次调用、两次 verify 都落在对方 mark_used 之前 ⇒ 都过闸都落盘」
的并发窗口（不变量见 :func:`token_lock` 的 docstring）。

## 🔴 红线 13 的三条结构约束（**改代码前先读**）

1. **`issue_write_confirm` 绝不得注册成工具、绝不得进 `for_model()`** ——
   本模块**不提供** `build_spec` / `TOOL_NAME` / `Args`。它不是工具模块，是**宿主侧函数库**。
   若模型能自己调 `issue_*` ⇒ 它就能自签自证 ⇒ **违约**。
2. **模型【不】自己拿凭证** —— 由**宿主侧**在「模型决定要写之后、写调用真正执行之前」
   注入到 `args.confirm_token`。本阶段的签发入口是**进程内可调用函数**，
   **不是** HTTP 面（界面确认交互归阶段 6 的 `回归检查`；⚠️ 本阶段**不得**称「审批门」—— 红线 11）。
3. **落痕只落结果值** —— `verify_*` 的五值结果落 `agent_steps.meta` 的 `write_confirm` 键，
   ⛔ **绝不落 token 原文 / 密钥 / 任何派生物**（否则「模型能看 meta」=「模型能伪造」）。

## 算法与绑定三要素（`D-回归检查`）

| 项 | 值 |
|---|---|
| 算法 | `hmac`（**stdlib，零新依赖**）· HMAC-SHA256 |
| 比对 | `hmac.compare_digest` —— ⛔ **不得用 `==`**（时序侧信道） |
| 绑定① | **目标路径**（归一后的**相对根形态**，posix） |
| 绑定② | **内容 hash** = `sha256(写入后内容字节).hexdigest()` |
| 绑定③ | **时间戳**（签发时刻，UTC epoch 秒） |
| 密钥 | `settings.WORKSPACE_CONFIRM_SECRET`（落 `.env`；**空 ⇒ 一律 `missing`** ⇒ 写通道整体不可用） |
| 过期窗口 | `CONFIRM_WINDOW_SECONDS = 300`（**≤ 300 秒**，写死） |
| 一次性 | **进程内已用集合** + `threading.Lock`（**不建表、零迁移**）+ **per-token `asyncio.Lock` 临界区**（`回归修复`，压缩 verify→mark 窗口） |

## 🔴 一条支撑本机制安全的**隐式不变量**（必须写明，否则未来改动会悄悄破坏它）

**凭证有效性依赖「模型不可读密钥」**：密钥落 `.env`，而 `.env` 属
`_paths.SENSITIVE_SEGMENTS`（`_paths.py:123`）⇒ 读工具拒之。
⇒ 🔴 **本阶段不得改动该清单**（动它 = 在无人察觉下使凭证机制失效）。
⚠️ 写侧若要额外拒绝某类路径，**必须新建写侧常量**（见 `_write_paths.py`），
**不得**把新项塞进 `_paths.SENSITIVE_SEGMENTS`（那会改读侧行为）。

## 已知限制（**如实登记，不得粉饰**）

- **重启窗口内的重放防护不存在**：已用集合在**进程内存**，进程重启即清空。
  取内存而非落库的理由 = **本阶段零迁移**是硬约束。
- **密钥键无设置页可见面**（`D-回归检查` 定档「不上设置页」）⇒ 用户不可见、不可调；
  与 `回归修复`（联网两键无用户可写/可见面）**同型**，须在阶段小结件与缺陷账本如实登记。

## 分包边界

只允许标准库 + `app.core`（`A17` 同口径）。**不 import** `app.api` / `app.agent.runner`。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import ntpath
import threading
import time
from dataclasses import dataclass
from typing import Any, Final, Literal

# ── 常量（**冻结**：文档化 + 用例引用）────────────────────────────────────────

#: token 编码格式的版本号（进 payload；将来换算法时可并存两种校验）。
CONFIRM_VERSION: Final[int] = 1

#: **过期窗口**（秒）。`D-回归检查` 要求「写死一个短窗口（建议 ≤ 300 秒）」⇒ 取 300。
CONFIRM_WINDOW_SECONDS: Final[int] = 300

#: 允许的**未来时钟容差**（秒）：`ts` 比当前时刻晚超过该值 ⇒ 视为 `expired`。
#: 给少量容差，是为了让机器时钟微调不至于误拒一份刚刚签发的合法凭证。
CONFIRM_FUTURE_SKEW_SECONDS: Final[int] = 5

#: 五值判定结果的**封闭取值域**（判据 `A22` 断的就是这五个）。
ConfirmResult = Literal["ok", "missing", "replayed", "expired", "mismatch"]

#: ⚠️ **判定次序写死**（`D-回归检查`）：missing -> replayed -> expired -> mismatch -> ok。
#: 之所以 `replayed` 排在 `expired` 之前：一个**用过的** token 即使也已过期，
#: 也应报「重放」——那才是它被拒的**真原因**（报 expired 会掩盖重放尝试）。
JUDGE_ORDER: Final[tuple[str, ...]] = ("missing", "replayed", "expired", "mismatch", "ok")

#: HMAC 输入串的**字段分隔符**（NUL，`"\\x00"`）。
#: 🔴 **为什么必须是 NUL 而不是 `|` / `\\n`**：路径与 hash 都是内容可控的字符串，
#: 若用可打印分隔符，构造 ("a|b", "c") 与 ("a", "b|c") 会得到**同一个输入串**
#: ⇒ 两个不同的 (path, hash) 组合签发/校验同一份 MAC = **签名歧义**（经典拼接漏洞）。
#: NUL 在 Windows 路径与 hex 摘要里都**不可能出现** ⇒ 无歧义。
_SEP: Final[str] = "\x00"
#: 域分离前缀（防「拿本仓别处的 HMAC 当本凭证」）。
_MAC_PREFIX: Final[str] = "zhilan/workspace-write/confirm"

#: **进程内已用 token 集合**（一次性消费）。键 = token 串本身。
#: ⚠️ 只用 set + Lock，**不落库、零迁移**；进程重启即清空（**已知限制**）。
_USED: Final[set[str]] = set()
_USED_LOCK: Final[threading.Lock] = threading.Lock()

#: **per-token 临界区锁注册表**（`回归修复`）。键 = token 串。
#: 缺陷现场：verify 与 mark_used **分离非原子** —— 同一张有效凭证并行两次调用，
#: 两次 verify 可以都落在对方 `mark_used` 之前 ⇒ 都过闸都落盘（双写）。
#: ⇒ 写工具 handler 把「verify → mark_used」包进**同一把** `asyncio.Lock`（同 token 同锁）：
#: **临界区互斥 ⇒ 后来者 verify 必见 `replayed`**（不变量见 :func:`token_lock`）。
#: ⚠️ 用 `asyncio.Lock` 而**非** `threading.Lock`：handler 的全部调用方都是
#: **事件循环里的协程**，threading.Lock 的阻塞等待会把整个事件循环卡死。
#: ⚠️ dict 的读 / 写都是**单条语句**（取锁与 setdefault 之间**无 await 夹缝**）
#: ⇒ 在单事件循环内是原子的，两个并发取锁者不会各造各的锁。
_TOKEN_LOCKS: Final[dict[str, asyncio.Lock]] = {}


@dataclass(frozen=True)
class ConfirmToken:
    """解出来的 token 载荷（内部用）。"""

    version: int
    path: str
    sha256: str
    ts: int
    mac: str


# ── 密钥读取（**全模块唯一读点**）────────────────────────────────────────────


def _secret() -> bytes:
    """读配置里的凭证密钥；**空 / 未配置 ⇒ `b""`** ⇒ 调用方一律返 `missing`。

    ⚠️ **本函数是全模块唯一的密钥读点**（与 `_paths._read_configured_root` 同型：单一读点，
    便于用例 monkeypatch 模块属性而不必碰 settings）。

    🔴 **不做归一**（不 strip 引号 / 不 expanduser）：密钥是**不透明字节串**，
    任何「智能处理」都会让「签发时读到的」与「校验时读到的」不一致。
    仅把**纯空白**视为未配置（那是 .env 里 `KEY=` 的常见形态）。
    """
    from app.core.config import settings  # 局部 import：避免模块级 app.* 依赖

    raw = getattr(settings, "WORKSPACE_CONFIRM_SECRET", "") or ""
    if not str(raw).strip():
        return b""
    return str(raw).encode("utf-8")


# ── 归一（**签发与校验必须用同一把尺**）──────────────────────────────────────


def canonical_rel_path(root: Any, rel_path: str) -> str:
    """把「目标路径」归一成 **posix 相对根形态**（绑定要素①）。**零 IO**。

    🔴 **为什么必须归一**：签发方与校验方若能拿到「同一个文件」的两种**不同字符串**
    （`sub\\\\a.txt` vs `sub/a.txt` vs `./sub/a.txt`），MAC 就对不上
    ⇒ 合法凭证被误判 `mismatch`。

    归一只做**纯字符串**处理（`ntpath.normpath` + 反斜杠转正斜杠），**零 IO** ——
    真正「是不是同一个文件 / 有没有越界」由 `_write_paths.resolve_write_target` 判，
    不在本函数职责内。⚠️ `root` 形参只参与「同尺」约定，本函数**不读**它（保持零 IO）。
    """
    raw = str(rel_path or "").strip().replace("/", ntpath.sep).replace(ntpath.sep * 2, ntpath.sep)
    norm = ntpath.normpath(raw)
    if norm in (".", ""):
        return ""
    return norm.replace(ntpath.sep, "/").lstrip("/")


def content_digest(content: bytes) -> str:
    """内容 hash（绑定要素②）：`sha256(写入后内容字节).hexdigest()`。

    ⚠️ 口径写死 = **写入后的完整内容**（不是 diff、不是旧内容）——「确认的就是这份内容」。
    ⚠️ 用 `bytes` 入参而不是 `str`：编码保真（`A8`）要求按**原编码**写回
    ⇒ hash 必须落在**最终要落盘的字节**上，而不是解码后的文本上（两者对 GBK 内容不同）。
    """
    return hashlib.sha256(bytes(content)).hexdigest()


# ── MAC（**输入串逐字构造**）─────────────────────────────────────────────────


def _mac(*, secret: bytes, version: int, path: str, digest: str, ts: int) -> str:
    """算 HMAC-SHA256（十六进制小写）。

    输入串**逐字构造**（防歧义，`D-回归检查`）：

        输入 = "zhilan/workspace-write/confirm" + NUL
             + str(version)                      + NUL
             + path                              + NUL
             + digest                            + NUL
             + str(ts)

    三条不能省：① **带前缀常量**（域分离，防拿别处 HMAC 冒充）；
    ② **带版本号**（防将来加字段时新旧输入串串通）；
    ③ **NUL 分隔 + 字段顺序固定**（防拼接歧义，见 `_SEP`）。
    """
    message = _SEP.join((_MAC_PREFIX, str(int(version)), path, digest, str(int(ts))))
    return hmac.new(secret, message.encode("utf-8"), hashlib.sha256).hexdigest()


# ── token 编解码 ─────────────────────────────────────────────────────────────


def _encode(token: ConfirmToken) -> str:
    """把载荷编成 **base64url 的紧凑 JSON**（去填充）。

    为什么 base64url + JSON：① 单行、无空白，可直接当 tool args 的字符串传；
    ② 去填充后仍可解（`_decode` 补回）；③ JSON 便于将来加字段（`v` 控版本）。
    """
    payload = {
        "v": int(token.version),
        "path": token.path,
        "sha256": token.sha256,
        "ts": int(token.ts),
        "mac": token.mac,
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(token: str) -> ConfirmToken | None:
    """解 token；**任何**畸形（坏 base64 / 坏 JSON / 缺键 / 类型错）⇒ None（**不抛**，I5）。

    ⚠️ 解不开 ⇒ 调用方按 `mismatch` 处理（**不是** `missing`）：
    `missing` 的语义**专指**「压根没给 token / 密钥未配置」，而「给了一个烂 token」
    是内容不符 —— 两者是**不同**的事实，混为一谈会让 `A22` 腿① 与腿③ 观察同一量。
    """
    try:
        text = str(token or "").strip()
        if not text:
            return None
        padded = text + "=" * (-len(text) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            return None
        version = data["v"]
        path = data["path"]
        digest = data["sha256"]
        ts = data["ts"]
        mac = data["mac"]
        if not isinstance(version, int) or not isinstance(ts, int):
            return None
        if not all(isinstance(item, str) for item in (path, digest, mac)):
            return None
        return ConfirmToken(version=version, path=path, sha256=digest, ts=ts, mac=mac)
    except Exception:  # noqa: BLE001 —— 畸形 token 一律 None（I5：不抛穿写通道）
        return None


# ── 签发（**宿主侧**；绝不得注册成工具）──────────────────────────────────────


def issue_write_confirm(*, root: Any = None, rel_path: str, content: bytes) -> str:
    """**宿主侧签发**一份一次性写凭证；返回 token 串。

    ## 🔴 红线 13：本函数**不得**注册成工具、**不得**进 for_model()

    它只允许被**宿主侧**（设置/工具装配层、进程内、将来的界面确认）调用；
    模型**永不**直接调用它 ——「模型自签自证」= 确认通道失效。

    ## 参数

    - root：工作区根（仅用于与 rel_path 一起算归一形态；本函数**不做**越界判定，
      那是 `_write_paths.resolve_write_target` 的职责）；
    - rel_path：**相对根**路径（与写调用 `args.path` 同一字符串最省事，
      不同写法也可 —— 归一会消掉分隔符差异）；
    - content：**将写入的完整新内容字节**（与 `args.content` 编码成字节后须**逐字节相同**）。

    ## 密钥为空时

    返回**空串**（`""`）—— 调用方把空串塞进 `args.confirm_token` 时，
    :func:`verify_write_confirm` 会按 `missing` 拒。**不抛异常**（I5）：
    签发失败不该炸穿宿主侧的任何链路。
    """
    secret = _secret()
    if not secret:
        return ""
    path = canonical_rel_path(root, rel_path)
    digest = content_digest(content)
    ts = int(time.time())
    mac = _mac(secret=secret, version=CONFIRM_VERSION, path=path, digest=digest, ts=ts)
    return _encode(ConfirmToken(version=CONFIRM_VERSION, path=path, sha256=digest, ts=ts, mac=mac))


# ── 一次性消费（**进程内**，不建表）──────────────────────────────────────────


def _seen_replay(token: str) -> bool:
    """该 token 是否**已经用过**（只读查询，不改集合）。

    ⚠️ 读与「登记已用」**必须分开**：判定次序要求 `replayed` 排在任何消费动作**之前**，
    否则「本次消费」会把「本次重放」误判成「首次使用」。
    """
    with _USED_LOCK:
        return token in _USED


def mark_used(token: str) -> None:
    """把 token 登记进**进程内已用集合**（一次性消费的**唯一**落点）。

    调用时机：**校验通过（`ok`）之后**由写工具 handler 调。
    ⚠️ 只有 `ok` 之后才登记 —— 校验失败的 token **不消费**，
    否则一次重放尝试会「顺手把合法 token 作废」（拒绝路径不该有副作用）。
    """
    if not token:
        return
    with _USED_LOCK:
        _USED.add(token)


def token_lock(token: str | None) -> asyncio.Lock:
    """取**该 token 专属**的临界区锁（**同 token 同锁**；`回归修复`）。

    ## 🔴 不变量（**必须写明**，否则未来改动会悄悄破坏它）

    **同一 token 的 `verify_write_confirm` → `mark_used` 临界区互斥**：
    并发的第二个调用者必须等第一个**完整走完**（含 `mark_used`）才能进闸
    ⇒ 它的 verify 一律查到 `_USED` 已命中 ⇒ 判 `replayed` ⇒ **恰好一次落盘**。
    若没有这把锁，两次 verify 可以都落在对方 `mark_used` 之前 ⇒ 都过闸都落盘（双写）。

    ## 实现口径

    - 同 token ⇒ 同一把锁（dict 命中；取不到就 `setdefault` —— 单事件循环内原子，
      见 `_TOKEN_LOCKS` 的注释）；
    - `token` 为 `None` / 空串 ⇒ 归一到空串键（handler 在 verify **之前**取锁，
      而「无 token」本来就会被 verify 判 `missing`，空键锁是无害的占位）；
    - ⚠️ 只对**事件循环内的协程**有效（handler 的全部调用方都是协程）；
    - **权威状态是 `_USED` 集合本身**，锁只负责压缩 verify→mark 窗口，
      不承担消费记账（见 :func:`release_token_lock` 对 pop 竞态的登记）。
    """
    key = str(token or "")
    lock = _TOKEN_LOCKS.get(key)
    if lock is None:
        lock = _TOKEN_LOCKS.setdefault(key, asyncio.Lock())
    return lock


def release_token_lock(token: str | None) -> None:
    """**用后即清**该 token 的锁（防 `_TOKEN_LOCKS` 随 token 数无界上涨）。

    ⚠️ 调用时机：**临界区外**（handler 的外层 `finally`，`async with` 已退出之后）。

    ⚠️ **pop 竞态是良性的（如实登记）**：pop 之后到来的第三个调用者会拿到**新**锁对象，
    与仍握着旧锁对象的等待者之间不再互斥 —— 但 pop 只发生在**上一个临界区退出之后**：
    成功路径上 `mark_used` 已先于锁释放执行，权威状态 `_USED` 已登记该 token，
    后来者**无论拿哪把锁**都判 `replayed` ⇒ 不会双写；失败路径本就**不消费**
    （「失败不消费」语义不变，token 仍可重试）。
    锁的职责边界 = **只**压缩「verify→mark_used」窗口，**不**承担消费记账。
    """
    _TOKEN_LOCKS.pop(str(token or ""), None)


def reset_used_for_tests() -> None:
    """清空已用集合**与** per-token 锁注册表（**测试工具**；照 `cache.reset_cache` 同类先例）。

    ⚠️ 用它的是**测试**，不是产品路径 —— 进程级集合会跨用例泄漏（本仓踩过同类坑）；
    `_TOKEN_LOCKS` 同理（`asyncio.Lock` 对象跨用例残留）⇒ 同步清空。
    """
    with _USED_LOCK:
        _USED.clear()
    _TOKEN_LOCKS.clear()


# ── 校验（**写工具 handler 侧**）────────────────────────────────────────────


def verify_write_confirm(
    token: str | None, *, root: Any = None, rel_path: str, content: bytes
) -> ConfirmResult:
    """校验凭证，返回**五值之一**（:data:`ConfirmResult`）。

    ## 判定次序（**写死，不可调换** —— `D-回归检查`）

        missing -> replayed -> expired -> mismatch -> ok

    逐条给**为什么必须在此位**：

    1. **missing 最先**：token 为空 / 密钥未配置 ⇒ 写通道**整体不可用**（fail-closed）。
       这一条必须在任何解密之前 —— 没密钥时连 MAC 都算不出来，先判它才不会把
       「未配置」误报成「内容不符」（两者**修复动作相反**：前者去配密钥，后者改内容）。
    2. **replayed 第二**：重放是**安全事件**，且「用过的 token」的 MAC 可能仍然正确 ⇒
       必须先于 mismatch/ok 判，否则一个被重放的合法 token 会走到 ok。
    3. **expired 第三**：时间窗是**独立于内容**的维度；先判时间可以避免
       「token 已过期但内容也改了」时报 mismatch（掩盖过期这一真原因）。
    4. **mismatch 第四**：三要素逐字段比对 + MAC 比对。⛔ 用 `hmac.compare_digest`，
       **不得用 `==`**（时序侧信道）。
    5. **ok 最后**：只有前四条全过才是放行。

    ## 密钥为空 ⇒ 一律 missing

    `_secret()` 返 `b""` 时**直接返回 missing**，不进入任何后续判定
    —— 与 `D-回归检查` 的「空 ⇒ 写通道 fail-closed 关闭」逐字一致。

    ## 本函数**零 IO**

    它只做字符串 / 字节 / 时间运算 + 一次集合查询 —— **不碰文件系统**。
    这是 `A22` 腿①（无凭证 ⇒ 拒 + **零 IO** + 目标文件未变）成立的前提：
    「凭证闸」必须排在「写」之前，且自身不产生 IO。

    ## 调用方契约（**写工具 handler 必须照此调**）

        result = verify_write_confirm(args.confirm_token, root=ws, rel_path=args.path,
                                      content=args.content.encode(编码))
        if result != "ok":
            return ToolObservation(text=<中文可读拒绝文案>, ok=False)   # 零 IO、不写
        mark_used(args.confirm_token)   # ⚠️ **只在 ok 之后**才消费
        # 然后才落 WriteOutcome 与 meta["write_confirm"] = result
    """
    # ① missing —— 密钥缺失或未带 token（**fail-closed**）
    secret = _secret()
    if not secret:
        return "missing"
    text = str(token or "").strip()
    if not text:
        return "missing"

    # ② replayed —— 已用集合命中（**读**，不消费）
    if _seen_replay(text):
        return "replayed"

    # 解 token（畸形 ⇒ mismatch，不是 missing —— 见 _decode 的说明）
    parsed = _decode(text)
    if parsed is None:
        return "mismatch"

    # ③ expired —— 时间窗（含少量未来容差，防时钟微调误拒）
    now = int(time.time())
    age = now - int(parsed.ts)
    if age > CONFIRM_WINDOW_SECONDS or age < -CONFIRM_FUTURE_SKEW_SECONDS:
        return "expired"

    # ④ mismatch —— 三要素逐字段 + MAC（**先字段后 MAC**：字段不符时不必算 MAC）
    expected_path = canonical_rel_path(root, rel_path)
    expected_digest = content_digest(content)
    if parsed.path != expected_path:
        return "mismatch"
    if parsed.sha256 != expected_digest:
        return "mismatch"
    # ⚠️ 重算时用**解出来的** v/path/digest/ts（而不是当前输入）—— 这样「改了 payload 任一字段
    #    但没改 mac」必然对不上；反之若用当前输入重算，就退化成「只验 mac 是不是本进程算的」。
    expected_mac = _mac(
        secret=secret,
        version=int(parsed.version),
        path=parsed.path,
        digest=parsed.sha256,
        ts=int(parsed.ts),
    )
    # 🔴 必须 compare_digest（**不得** ==）：防时序侧信道。
    if not hmac.compare_digest(expected_mac, parsed.mac):
        return "mismatch"

    # ⑤ ok
    return "ok"


__all__ = [
    "CONFIRM_FUTURE_SKEW_SECONDS",
    "CONFIRM_VERSION",
    "CONFIRM_WINDOW_SECONDS",
    "ConfirmResult",
    "ConfirmToken",
    "JUDGE_ORDER",
    "canonical_rel_path",
    "content_digest",
    "issue_write_confirm",
    "mark_used",
    "release_token_lock",
    "reset_used_for_tests",
    "verify_write_confirm",
    "token_lock",
]
