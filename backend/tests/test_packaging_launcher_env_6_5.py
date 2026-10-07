# -*- coding: utf-8 -*-
"""阶段 6 · 条目 6.5：打包入口「冻结态环境默认值」的回归测试（裁决 3 的钉子）。

**为什么必须有这个文件**：`6.5` 规格 5 的判据是「**干净机器装完后不改任何配置文件即可启动**」，
而它整条押在五个 `MINERU_*` 真的到位上。这五个键分两条腿走，且**两条腿的边界很容易被后人踩平**：

- `.env` 只喂 pydantic，而 `Settings` 是 `extra="ignore"` ⇒ **只有声明过的键**才认。
  `MINERU_MODEL_SOURCE` / `HF_HOME` / `MODELSCOPE_CACHE` 三个是 MinerU 直接 `os.getenv` 读的
  **真进程环境变量**，写进 `.env` 会被**静默丢弃**（2026-09-20 隔离探针实测：`os.environ` 里是
  `None`，`settings` 上连同名属性都没有）；
- `MINERU_MODEL_DIR` / `MINERU_DEVICE` 是 pydantic 键（写 `.env` 有效），由
  `pipeline/mineru.py::_apply_mineru_env()` 映射成 `MINERU_HOME` / `MINERU_DEVICE_MODE`。

所以本文件钉三件事（对应开工包 A6/A7）：

1. **冻结态五键全中**，且路径锚点 = **exe 目录**（`Path(sys.executable).resolve().parent`）、
   **不是** `sys._MEIPASS`（临时解包目录，下次启动就没了）；
2. **开发态逐字不变**——`sys.frozen` 为假时**一个键都不许设**（这是 6.2 起的一贯纪律）；
3. **A7 的反例**——「只写 `.env` ⇒ 后三个不生效」必须有可判真假的用例，否则裁决 3 的必要性
   只是注释里的一句话，后人会顺手把那三个键挪回 `.env`。

纪律：不连库、不联网、不起服务；只加载打包入口源码 + 在**临时目录**里造隔离 `.env`，
**不碰仓库 `.env`**（它含真实密钥）。进程环境的改动一律走 `monkeypatch`（自动还原）。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]
LAUNCHER = REPO / "tools" / "packaging" / "zhilan_launcher.py"

#: 冻结态必须钉住的五项（与 `launcher.FROZEN_ENV_KEYS` 必须逐字一致）。
EXPECTED_KEYS = (
    "MINERU_MODEL_SOURCE",
    "MINERU_HOME",
    "HF_HOME",
    "MODELSCOPE_CACHE",
    "MINERU_DEVICE_MODE",
)
#: 「只写 `.env` 没用」的三个（真进程环境变量，不是 pydantic 键）。
ENV_ONLY_KEYS = ("MINERU_MODEL_SOURCE", "HF_HOME", "MODELSCOPE_CACHE")


@pytest.fixture()
def launcher():
    """按文件路径加载打包入口（`tools/packaging` 不是包，不能 import）。

    与 `zhilan.spec::_load_smoke_modules()` 同一手法：`exec_module` 只跑顶层定义，
    `if __name__ == "__main__"` 块不触发 ⇒ 既不开窗口也不跑自检。
    """
    spec = importlib.util.spec_from_file_location("_zl_launcher_6_5", LAUNCHER)
    assert spec is not None and spec.loader is not None, f"无法从 {LAUNCHER} 建立模块规格"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def clean_env(monkeypatch):
    """把五项键从进程环境里清掉（`monkeypatch` 负责用例结束后还原原值）。"""
    for key in EXPECTED_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.fixture()
def dev(clean_env):
    """显式回到开发态。"""
    clean_env.delattr(sys, "frozen", raising=False)
    return clean_env


@pytest.fixture()
def frozen(clean_env, tmp_path):
    """进入冻结态：exe 在 `<tmp>/Zhilan/zhilan.exe`，`_MEIPASS` 另指一处临时解包目录。

    造 `_MEIPASS` 是**刻意的**：锚点若被后人改成 `_MEIPASS`，用例必须能发现
    （那种写法在真机上把模型根指到进程退出即清理的目录）。
    """
    install = tmp_path / "Zhilan"
    install.mkdir()
    exe = install / "zhilan.exe"
    exe.write_bytes(b"MZ")  # 只要有实体，`Path.resolve()` 才不会被当成不存在的路径
    meipass = tmp_path / "unpack" / "_internal"
    meipass.mkdir(parents=True)
    clean_env.setattr(sys, "frozen", True, raising=False)
    clean_env.setattr(sys, "executable", str(exe))
    clean_env.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    return install


# ---------------------------------------------------------------------------
# A6：冻结态五键全中 / 开发态逐字不变
# ---------------------------------------------------------------------------
def test_frozen_env_keys_are_exactly_the_five(launcher):
    """钉住键集本身：多一个少一个都要有人显式改这里（它是 A6/A7 的判据面）。"""
    assert tuple(launcher.FROZEN_ENV_KEYS) == EXPECTED_KEYS


def test_frozen_mode_pins_all_five(launcher, frozen):
    """冻结态：五项**全部**按期望值到位（`setdefault` 真的落进 `os.environ`）。"""
    written = launcher._apply_frozen_env_defaults()

    assert sorted(written) == sorted(EXPECTED_KEYS), f"应五项全部写入，实际 {written}"
    assert os.environ["MINERU_MODEL_SOURCE"] == "local"  # 不钉 ⇒ 上游联网找模型
    assert os.environ["MINERU_DEVICE_MODE"] == "cpu"  # 目标机无 GPU，必须钉死
    # Settings import authorization: upstream MinerU itself appends "models" to HOME.
    assert Path(os.environ["MINERU_HOME"]) == frozen
    assert Path(os.environ["HF_HOME"]) == frozen / "models" / "hf"
    assert Path(os.environ["MODELSCOPE_CACHE"]) == frozen / "models" / "ms"


def test_frozen_paths_anchor_to_exe_dir_not_meipass(launcher, frozen):
    """路径锚点必须是**数据根**（exe 目录），不是只读、进程退出即清理的 `_MEIPASS`。"""
    launcher._apply_frozen_env_defaults()

    meipass = Path(sys._MEIPASS).resolve()  # type: ignore[attr-defined]
    exe_dir = Path(sys.executable).resolve().parent
    assert meipass != exe_dir, "夹具自证：两个锚点必须是不同目录，否则本用例证明不了什么"
    for key in ("MINERU_HOME", "HF_HOME", "MODELSCOPE_CACHE"):
        value = Path(os.environ[key])
        assert value == exe_dir or exe_dir in value.parents, f"{key} 没锚在 exe 目录下：{value}"
        assert meipass not in value.parents, f"{key} 锚到了 _MEIPASS（临时解包目录）：{value}"


def test_user_override_wins_over_frozen_defaults(launcher, frozen):
    """一律 `setdefault` 语义：用户显式设过的真环境变量**不被覆盖**。"""
    custom = str(frozen / "别处" / "自定义模型根")
    os.environ["MINERU_HOME"] = custom

    written = launcher._apply_frozen_env_defaults()

    assert os.environ["MINERU_HOME"] == custom
    assert "MINERU_HOME" not in written, "已存在的键不该出现在「真正写入」清单里"
    assert sorted(written) == sorted(set(EXPECTED_KEYS) - {"MINERU_HOME"})


def test_dev_mode_sets_nothing(launcher, dev):
    """**开发态逐字不变**：`sys.frozen` 为假 ⇒ 一个键都不设（6.2 起的一贯纪律）。"""
    assert launcher._apply_frozen_env_defaults() == []
    for key in EXPECTED_KEYS:
        assert key not in os.environ, f"开发态不该设 {key}"


def test_main_applies_defaults_before_dispatching(launcher, frozen, monkeypatch):
    """`main()` 必须先钉环境、再进 `--smoke` / `client.app`（顺序是硬要求）。

    `mineru/config.py::ModelConfig.base_dir` 的默认值是在 **import 期**由 `MINERU_HOME`
    算出来的常量 ⇒ 顺序反了就白设（本用例用桩替换 `_smoke`，在它被调用的**那一刻**取样）。
    """
    seen: dict[str, str | None] = {}
    # ⚠️ 070 适配：`main()` 现以 `_smoke(out_path)` 带参调用（`--smoke-out` 出口）；
    # 本桩用 `*_a` 接住它——被判定的事实（进 smoke 前环境已钉）一字未变。
    monkeypatch.setattr(
        launcher, "_smoke", lambda *_a: (seen.update({k: os.environ.get(k) for k in EXPECTED_KEYS}) or 0)
    )
    monkeypatch.setattr(sys, "argv", ["zhilan.exe", "--smoke"])

    assert launcher.main() == 0
    assert set(seen) == set(EXPECTED_KEYS)
    assert all(value is not None for value in seen.values()), f"进入 --smoke 时仍有键未钉：{seen}"


def test_main_dev_mode_touches_no_env(launcher, dev, monkeypatch):
    """开发态跑 `main()` 同样一个键都不设（双模式两边都钉）。"""
    monkeypatch.setattr(launcher, "_smoke", lambda *_a: 0)  # 070 适配：带参调用，桩接住
    monkeypatch.setattr(sys, "argv", ["zhilan.exe", "--smoke"])

    assert launcher.main() == 0
    for key in EXPECTED_KEYS:
        assert key not in os.environ


def test_smoke_module_count_stays_at_22(launcher):
    """`--smoke` 的模块数 = **22**，`zhilan.spec` 的 `hiddenimports` 直接从这份元组读。

    ⚠️ 耦合钉：增删 `_SMOKE_MODULES` 会**同时**改掉 `--smoke` 输出与 spec 的 `hiddenimports`
    （spec 直接从 launcher 读这份元组），两边必须一起动。
    🔴 070 口径更正（事实跟随）：`build.ps1` 的 L2 判据**主源**已从 stdout 正则
    （`'22\\s*.*?\\s*0\\s'`）改为 `--smoke-out` 结果 JSON 的 `modules_total` / `failures_count`；
    stdout 字样降为旁证。模块数 22 仍被 spec 动态读取，本钉继续有效。
    此处把耦合显式钉住。
    """
    # Settings import adds exactly one named module and validates its bundled manifest.
    assert launcher._SMOKE_MODULES.count("app.system.offline_models") == 1
    assert len(launcher._SMOKE_MODULES) == 22 + 1


# ---------------------------------------------------------------------------
# A7：五个键各自可证 —— 反例（只写 `.env` ⇒ 后三个不生效）+ 映射（前两个）
# ---------------------------------------------------------------------------
def test_env_file_alone_cannot_reach_process_env(tmp_path, monkeypatch):
    """**A7 的反例钉**：后三个键写进 `.env` ⇒ 既不进进程环境、也不进 settings。

    这条用例证明的是**裁决 3 的必要性**：如果哪天有人把 `MINERU_MODEL_SOURCE` 之类
    挪进 `.env` 就以为完事，本用例仍然会绿（因为它说的就是「这么写没用」），
    但 `test_frozen_mode_pins_all_five` 会红——两条合起来才是完整判据。
    """
    from app.core.config import Settings

    env_file = tmp_path / "desktop.env"
    env_file.write_text(
        "\n".join(
            [
                "DB_BACKEND=sqlite",
                "MINERU_MODEL_DIR=D:/Zhilan/models",
                "MINERU_DEVICE=cpu",
                "MINERU_MODEL_SOURCE=local",
                "HF_HOME=D:/Zhilan/models/hf",
                "MODELSCOPE_CACHE=D:/Zhilan/models/ms",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # 隔离环境：先清掉可能存在的同名真环境变量，避免它们「碰巧」让断言变绿
    for key in EXPECTED_KEYS:
        monkeypatch.delenv(key, raising=False)

    isolated = Settings(_env_file=str(env_file))

    # ① pydantic 键：读得到（这两个才是写 `.env` 该管的东西）
    assert isolated.DB_BACKEND == "sqlite"
    assert isolated.MINERU_MODEL_DIR == "D:/Zhilan/models"
    assert isolated.MINERU_DEVICE == "cpu"
    # ② 真环境变量：`extra="ignore"` 静默丢弃 ⇒ 连属性都没有
    for key in ENV_ONLY_KEYS:
        assert not hasattr(isolated, key), f"{key} 进了 settings —— 裁决 3 的前提变了"
    # ③ 也不许「偷偷」导出到进程环境（pydantic-settings 不会这么做，这里钉住这一事实）
    for key in ENV_ONLY_KEYS:
        assert key not in os.environ, f"{key} 竟被写进了进程环境"


def test_apply_mineru_env_maps_pydantic_keys_to_process_env(monkeypatch):
    """另两个键的**映射**通路：`MINERU_MODEL_DIR` → `MINERU_HOME`、`MINERU_DEVICE` → `MINERU_DEVICE_MODE`。

    ⚠️ 这里打的是 `app.pipeline.mineru` 的**模块属性 `settings`**；用 `monkeypatch.setattr`
    而不是手工赋值——本仓有「全局状态被用例改坏 ⇒ 漏给后面用例」的先例，还原必须是自动的。
    """
    from app.pipeline import mineru as mineru_mod

    class _Stub:  # 只带被测函数读的两个属性
        MINERU_DEVICE = "cpu"
        MINERU_MODEL_DIR = "D:/Zhilan/models"

    monkeypatch.setattr(mineru_mod, "settings", _Stub)
    monkeypatch.delenv("MINERU_HOME", raising=False)
    monkeypatch.delenv("MINERU_DEVICE_MODE", raising=False)

    mineru_mod._apply_mineru_env()

    assert os.environ["MINERU_HOME"] == "D:/Zhilan/models"
    assert os.environ["MINERU_DEVICE_MODE"] == "cpu"

    # 幂等 + 不覆盖：已经存在的值一律不动（launcher 的 setdefault 与它同向）
    os.environ["MINERU_HOME"] = "D:/别处"
    mineru_mod._apply_mineru_env()
    assert os.environ["MINERU_HOME"] == "D:/别处"


def test_apply_mineru_env_auto_device_is_not_pinned(monkeypatch):
    """`auto` / 空值**不设** `MINERU_DEVICE_MODE`：交给 MinerU 自己探测（改造前的口径）。"""
    from app.pipeline import mineru as mineru_mod

    class _Stub:
        MINERU_DEVICE = "auto"
        MINERU_MODEL_DIR = ""

    monkeypatch.setattr(mineru_mod, "settings", _Stub)
    monkeypatch.delenv("MINERU_HOME", raising=False)
    monkeypatch.delenv("MINERU_DEVICE_MODE", raising=False)

    mineru_mod._apply_mineru_env()

    assert "MINERU_DEVICE_MODE" not in os.environ
    assert "MINERU_HOME" not in os.environ
