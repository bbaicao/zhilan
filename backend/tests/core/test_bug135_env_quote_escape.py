"""回归修复 判据载体：env_store._env_value 的「加引号 + 转义」往返（本仓首次给 _env_value 本体上判据）。

## 缺陷本体（改前必红）

_env_value 旧实现 ="'" + value.replace("'", "") + "'"=：值**同时**「命中加引号条件
（首尾空白 / 含『空格 + #』/ 含换行）」且「含撇号」时，撇号被**静默删除**
（O'Brien #1 ⇒ 落盘 OBrien #1），而 settings_routes 是「落盘成功才 setattr」
⇒ **磁盘与内存当场分歧、无任何告警**，重启后读入损坏值。

## 修法（拍板单 C 生效档）

双引号包裹 + 转义（反斜杠、双引号逐字符转义；换行 / 回车转成两字符转义）——**零拒答面**。

## 判据前置（拍板单 C 明写：落地前先实测 dotenv 兼容）

本仓 dotenv 实现 = **python-dotenv 1.2.3**（pydantic-settings 的既有依赖；
config._read_env_file_key 也直接用它）。探针（走锁，scratch/probe135.py）实测
「旧实现 / 新实现」两种写法 × 21 个值形态的「persist_env 写 → dotenv 读回」往返：

- **旧实现 8 个形态失败**：撇号被删 3 个（O'Brien #1 / a'b"c #x / ' it's '）、
  引号形态被吃 3 个（"abc" / 'abc' / 以引号开头的 'abc 会**整行判非法 ⇒ 丢键**）、
  回车→换行 1 个、美元花括号插值 1 个；
- **新实现只剩 1 个形态失败**：美元花括号形态（resolve_variables 对**所有**值插值，
  与引号形态无关、**改前改后同值**）⇒ 属**既有**缺陷，登记不修（腿⑧）；
- 不含引号 / 空白 / 换行的值**逐字节原样返回**（腿⑤负控）。

## 腿构成（8 例）

| # | 腿 | 判别力 |
|---|---|---|
| ① | 含撇号 + 「空格 + #」的值往返 == 原值（缺陷本体） | 改前必红（撇号被删） |
| ② | 含双引号 / 引号形态的值往返 == 原值 | 改前 3 个形态必红（引号被吃） |
| ③ | 含换行 / 回车的值往返 == 原值 **且落盘单行** | 改前「回车→换行」必红 |
| ④ | 以引号开头的值不再丢键（旧实现整行判非法） | 改前键消失、必红 |
| ⑤ | **负控腿**：无引号值的输出逐字节不变 | 改前改后同值（防误伤既有输出） |
| ⑥ | 形态矩阵：dotenv 真解析 + config._read_env_file_key 双读法逐形态往返 | 逐形态断言 |
| ⑦ | 同键重写两次仍等值（设置页连存的常见路径） | 覆盖「命中同名键就地替换」那条路 |
| ⑧ | **登记腿**：美元花括号形态任何引号形态都过不了（既有缺陷、读侧 scope 外） | 钉住现状 |

## 边界（🔴 不得含糊）

- 腿⑧ 的形态**不在本笔修复面**：修它要动读侧（`dotenv_values(..., interpolate=False)` 在
  app/core/config.py）⇒ 已登记、未修（本条不改任何读侧代码）。
- 判据覆盖 _env_value + persist_env 的**落盘往返**；设置页端点面由既有 settings 契约用例覆盖。
- 本笔**有意扩展**两处触发条件（已登记，见交付）：① 值以引号开头 / 结尾时也加引号
  （旧实现会让 dotenv 静默丢键 —— 同族且后果更重）；② 换行 / 回车不再裸写，而是转成
  两字符转义（旧实现的裸换行会让 persist_env 的按行重写在**下次改写**时截断值）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dotenv import dotenv_values

from app.core import config, env_store

#: 缺陷本体：撇号 + 「空格 + #」同时命中 ⇒ 旧实现把撇号删掉
DEFECT_VALUE = "O'Brien #1"

#: 判据前置矩阵里**能往返**的形态（腿⑥）；美元花括号形态单列在腿⑧。
MATRIX_VALUES: dict[str, str] = {
    "缺陷本体": DEFECT_VALUE,
    "撇号+首尾空白": " it's ",
    "双引号+行内注释": 'he said "hi" # note',
    "两种引号+行内注释": "a'b\"c #x",
    "引号形态_双": '"abc"',
    "引号形态_单": "'abc'",
    "引号开头": "'abc",
    "引号结尾": "abc'",
    "换行": "line1\nline2",
    "回车": "line1\rline2",
    "回车换行": "line1\r\nline2",
    "首尾空白": "  padded  ",
    "反斜杠": "back\\slash",
    "反斜杠+n字面量": "a\\nb",
    "反斜杠结尾": "end\\",
    "反斜杠+双引号": "a\\\"b",
    "裸美元": "dollar$5",
    "井号无空格": "abc#def",
    "普通值": "plain-value",
    "空值": "",
}

#: 负控腿（腿⑤）的取值：**不含引号 / 空白 / 换行** ⇒ 输出必须逐字节原样。
NO_QUOTE_VALUES = [
    "plain-value",
    "abc#def",
    "it's",
    "dollar$5",
    "end\\",
    "a\\nb",
    "1.5",
    "",
]


@pytest.fixture
def fake_env(tmp_path, monkeypatch) -> Path:
    """把 .env 指到 tmp_path（**绝不碰仓库真实 .env**；照 tests/test_model_roles.py::fake_dotenv）。"""
    path = tmp_path / ".env"
    monkeypatch.setattr(env_store, "ENV_PATH", path)
    assert env_store.ENV_PATH == path
    assert env_store.ENV_PATH != env_store.ROOT_DIR / ".env", "假 .env 落到了仓库真实路径上"
    return path


def _emit(value: str) -> str:
    """_env_value 的输出（= persist_env 落盘时写的那一段）。"""
    return env_store._env_value(value)


def _back(path: Path, key: str = "PROBE_KEY") -> str | None:
    """dotenv **真解析**读回（与 Settings / config._read_env_file_key 同一口径）。"""
    return dotenv_values(path).get(key)


def _back_via_config(path: Path, key: str = "PROBE_KEY") -> str:
    """仓库自己的读法（config._read_env_file_key，用的就是 env_store.ENV_PATH）。"""
    assert env_store.ENV_PATH == path, "假 .env 没生效"
    return config._read_env_file_key(key)


# --------------------------------------------------------------------------
# ① 缺陷本体
# --------------------------------------------------------------------------


def test_含撇号与行内注释的值往返等于原值(fake_env):
    """腿①（主腿）：O'Brien #1 —— 改前落盘会变成 OBrien #1（撇号被静默删除）。"""
    env_store.persist_env({"PROBE_KEY": DEFECT_VALUE})

    assert _emit(DEFECT_VALUE) == '"O\'Brien #1"', "新写法应是双引号包裹、撇号原样"
    assert _back(fake_env) == DEFECT_VALUE, f"dotenv 读回：{_back(fake_env)!r}"
    assert _back_via_config(fake_env) == DEFECT_VALUE
    # 磁盘上**不得**出现被删撇号的形态（改前的坏值）
    assert "OBrien #1" not in fake_env.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# ② 双引号与引号形态
# --------------------------------------------------------------------------


def test_含双引号与引号形态的值往返等于原值(fake_env):
    """腿②：含双引号的值、以及本身就是引号形态的值（'"abc"' / "'abc'"）都要往返。"""
    cases = ['he said "hi" # note', "a'b\"c #x", '"abc"', "'abc'"]
    for index, value in enumerate(cases):
        key = f"PROBE_KEY_{index}"
        env_store.persist_env({key: value})
        assert _back(fake_env, key) == value, f"{value!r} 往返失败：{_back(fake_env, key)!r}"
        assert _back_via_config(fake_env, key) == value


# --------------------------------------------------------------------------
# ③ 换行 / 回车：往返 + 落盘单行
# --------------------------------------------------------------------------


def test_含换行与回车的值往返等于原值且落盘单行(tmp_path, monkeypatch):
    """腿③：换行 / 回车 / 回车换行三种都要往返，且值在磁盘上**保持单行**（转成两字符转义）。

    为什么单行重要：persist_env 是**按行**读改写（splitlines），裸换行会让值在**下次改写**
    时被截断；两字符转义没有这个问题。
    """
    for index, value in enumerate(["line1\nline2", "line1\rline2", "line1\r\nline2"]):
        path = tmp_path / f".env{index}"
        monkeypatch.setattr(env_store, "ENV_PATH", path)
        env_store.persist_env({"PROBE_KEY": value})

        assert _back(path) == value, f"{value!r} 往返失败：{_back(path)!r}"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2, f"{value!r} 落盘不是单行：{lines!r}"
        # 落盘形态必须是**转义形态**（两字符 \n / \r），而不是真换行
        assert lines[-1] == "PROBE_KEY=" + _emit(value), f"落盘形态不是转义形态：{lines[-1]!r}"


# --------------------------------------------------------------------------
# ④ 引号开头：旧实现整行判非法、静默丢键
# --------------------------------------------------------------------------


def test_以引号开头的值不再丢键(fake_env):
    """腿④：'abc / "abc 这两种「以引号开头但不成对」的值，改前会让 dotenv 判整行非法 ⇒ 丢键。"""
    for key, value in (("PROBE_SINGLE", "'abc"), ("PROBE_DOUBLE", '"abc')):
        env_store.persist_env({key: value})
        assert key in fake_env.read_text(encoding="utf-8"), f"{value!r} 没写进文件"
        assert _back(fake_env, key) == value, f"{value!r} 读回失败：{_back(fake_env, key)!r}"
        assert _back_via_config(fake_env, key) == value


# --------------------------------------------------------------------------
# ⑤ 负控腿：无引号值输出逐字节不变
# --------------------------------------------------------------------------


def test_无引号值的输出逐字节不变_负控(fake_env):
    """腿⑤（**负控**）：不含引号 / 空白 / 换行的值，_env_value 输出必须**一个字都不变**。

    这条钉的是「修 135 不许顺手改掉既有输出」：那些值今天就是原样落盘的（真 dotenv 也确实
    能原样读回）⇒ 本笔一个字都不该动它们。
    """
    for index, value in enumerate(NO_QUOTE_VALUES):
        assert _emit(value) == value, f"{value!r} 的输出被改动了：{_emit(value)!r}"
        key = f"PROBE_NQ_{index}"
        env_store.persist_env({key: value})
        assert _back(fake_env, key) == value, f"{value!r} 往返失败：{_back(fake_env, key)!r}"


# --------------------------------------------------------------------------
# ⑥ 形态矩阵：双读法往返
# --------------------------------------------------------------------------


def test_形态矩阵双读法往返(tmp_path, monkeypatch):
    """腿⑥：MATRIX_VALUES 全部形态 —— persist_env 写 → dotenv 真解析 + config 读法都等值。"""
    path = tmp_path / ".env"
    monkeypatch.setattr(env_store, "ENV_PATH", path)
    for index, (name, value) in enumerate(MATRIX_VALUES.items()):
        key = f"PROBE_M_{index}"
        env_store.persist_env({key: value})
        assert _back(path, key) == value, f"[{name}] dotenv 读回失败：{_back(path, key)!r}"
        assert _back_via_config(path, key) == value, f"[{name}] config 读法失败"


# --------------------------------------------------------------------------
# ⑦ 同键重写幂等
# --------------------------------------------------------------------------


def test_同键重写两次仍等值(fake_env):
    """腿⑦：设置页连存同一键（走「命中同名键就地替换」）后，磁盘值仍等于原值、不重复追加。"""
    env_store.persist_env({"PROBE_KEY": DEFECT_VALUE})
    env_store.persist_env({"PROBE_KEY": DEFECT_VALUE})

    text = fake_env.read_text(encoding="utf-8")
    assert text.count("PROBE_KEY=") == 1, f"同一个键被写了多行：{text!r}"
    assert _back(fake_env) == DEFECT_VALUE
    assert len(text.splitlines()) == 2, f"文件行数不对：{text!r}"


# --------------------------------------------------------------------------
# ⑧ 登记腿：美元花括号形态（既有缺陷、读侧 scope 外）
# --------------------------------------------------------------------------


def test_登记腿_美元花括号形态任何引号形态都过不了(tmp_path, monkeypatch):
    """腿⑧（**登记腿**）：含「美元花括号」的值**任何引号形态都过不了往返** —— 既有缺陷、本笔不修。

    机制：python-dotenv 的 resolve_variables 对**所有**值做插值（单引号形态也一样），
    未定义的变量名解析成空串。修它在**读侧**（dotenv_values(..., interpolate=False)，
    app/core/config.py），不在本模块 scope ⇒ 如实登记。
    """
    monkeypatch.delenv("ZHILAN_PROBE_UNDEFINED", raising=False)
    value = "home=${ZHILAN_PROBE_UNDEFINED}"

    # ① 本笔的写侧：该值不含引号 / 空白 / 换行 ⇒ 输出原样（写侧无从下手）
    assert _emit(value) == value

    # ② 裸值形态：插值把它吃成空
    path = tmp_path / ".env"
    monkeypatch.setattr(env_store, "ENV_PATH", path)
    env_store.persist_env({"PROBE_KEY": value})
    assert _back(path) == "home=", f"裸值形态实测：{_back(path)!r}"

    # ③ 双引号形态（本笔的修法形态）同样被插值 ⇒ 证明这不是「引号选错」，而是读侧插值
    path.write_text('PROBE_Q="home=${ZHILAN_PROBE_UNDEFINED}"\n', encoding="utf-8")
    assert dotenv_values(path)["PROBE_Q"] == "home=", "双引号形态竟然没被插值 ⇒ 本登记腿前提变了"

    # ④ 单引号形态同理
    path.write_text("PROBE_S='home=${ZHILAN_PROBE_UNDEFINED}'\n", encoding="utf-8")
    assert dotenv_values(path)["PROBE_S"] == "home=", "单引号形态竟然没被插值 ⇒ 本登记腿前提变了"
