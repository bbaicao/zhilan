"""模型角色解析 / rerank 绑定保留（回归修复 · 配置层）。

背景（本条落地前已核实的三件事）：

1. ``ROLE_KEYS`` 原本只有 chat/embed/vision，``Settings.roles`` 按它遍历 ⇒
   ``MODEL_ROLES.rerank`` **永远读不到、也不会被写回**（`PUT /settings` 一保存就丢）；
2. ``dump_registry(services, roles)`` 把传入的 roles **整串**序列化写进 ``MODEL_ROLES`` ⇒
   只传三角色的调用方会把没解析到的 rerank 绑定整串覆盖掉；
3. 原 ``repair_missing_role_bindings`` 正是上面这条的扳机（``roles = dict(target.roles)``
   只三角色 + ``dump_registry`` 整串重写），且是死代码 ⇒ 本条已整函数删除。

本文件是**纯配置层**用例：不连库、不联网、不读也不写仓库真实 ``.env``
（所有落盘用例一律把 ``env_store.ENV_PATH`` 指向 ``tmp_path`` 下的假文件）。

另有一组**对抗性复核实测**后的栅栏（P2-1 / P2-2 / P2-3 / P2-4 / P2-5，见最后一节）：
迁移只写 ``MODEL_ROLES``、悬挂 embed 绑定不落盘、遗留配置指向自定义服务时不迁、
``_read_env_file_key`` 与 ``dotenv_values`` 逐条对照、``ensure_service_registry`` 不丢未知角色键。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from dotenv import dotenv_values

from app.core import config as config_mod
from app.core import env_store
from app.core.config import RERANK_ROLE, ROLE_KEYS, dump_registry, settings

SVC_CHAT = "svc_chat"
SVC_EMBED = "svc_embed"
SVC_EMBED_DIRTY = "svc_embed_dirty"
# 只在 `MODEL_ROLES` 里出现、`MODEL_SERVICES` 里**不存在**的服务 id（P2-2 的悬挂绑定现场）
SVC_GONE = "svc_gone"

CHAT_BINDING = {"service_id": SVC_CHAT, "model": "m-chat"}
EMBED_BINDING = {"service_id": SVC_EMBED, "model": "m-embed"}
# 遗留扁平键里的型号：取一个够独特的名字，便于断言「迁的就是它」
LEGACY_RERANK_MODEL = "m-rerank-legacy"
RERANK_BINDING = {"service_id": SVC_EMBED, "model": LEGACY_RERANK_MODEL}


def _services() -> list[dict]:
    """两条最小可解析的服务（id/name/provider/base_url/api_key/models/model_roles）。"""
    return [
        {
            "id": SVC_CHAT,
            "name": "对话服务",
            "provider": "dashscope",
            "base_url": "https://example.invalid/v1",
            "api_key": "sk-test",
            "models": ["m-chat"],
            "model_roles": {"m-chat": ["chat"]},
        },
        {
            "id": SVC_EMBED,
            "name": "向量服务",
            "provider": "dashscope",
            "base_url": "https://example.invalid/v1",
            "api_key": "sk-test",
            "models": ["m-embed", "m-rerank-legacy"],
            "model_roles": {"m-embed": ["embed"]},
        },
    ]


def _dirty_services() -> list[dict]:
    """故意做「脏」的注册表：未知字段 + 未登记在 ``models`` 里的型号 + 带空格的 api_key + 带尾斜杠的 url。

    这四项正是 `Settings.services` / `dump_registry` 会「顺手规范化」掉的东西（丢未知字段、
    api_key 去空格、base_url 去尾斜杠、model_roles 按 models 裁剪）——一旦迁移把 MODEL_SERVICES
    也一起落盘，它们就会从 .env 里消失（回归修复 对抗性复核 P2-1 的实测现场）。
    """
    return [
        {
            "id": SVC_EMBED_DIRTY,
            "name": "向量服务",
            "provider": "dashscope",
            "base_url": "https://example.invalid/v1/",  # 尾斜杠：dump 会去掉
            "api_key": "  sk-test  ",  # 前后空格：dump 会去掉
            "models": ["m-embed"],  # m-unlisted 故意不登记
            "model_roles": {"m-embed": ["embed"], "m-unlisted": ["embed"]},
            "note": "手写的未知字段，不得被迁移顺手删掉",
        }
    ]


@pytest.fixture
def fake_dotenv(tmp_path, monkeypatch) -> Path:
    """把 ``.env`` 指到 ``tmp_path`` 下的假文件（**绝不许碰仓库真实 .env**）。

    ``config`` 侧读遗留键（``env_store.ENV_PATH``）与 ``persist_env`` 落盘走的是**同一个**
    模块属性，所以改这一处就够；改完先自证确实落在 ``tmp_path`` 里，再让用例往下走。
    """
    path = tmp_path / ".env"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(env_store, "ENV_PATH", path)
    assert env_store.ENV_PATH == path
    assert env_store.ENV_PATH.parent == tmp_path
    assert env_store.ENV_PATH != env_store.ROOT_DIR / ".env", "假 .env 落到了仓库真实路径上"
    return path


def _write_legacy_env(
    path: Path,
    *,
    rerank_model: str | None,
    roles: dict,
    services: list[dict] | None = None,
    rerank_provider: str = "dashscope",
    rerank_base_url: str = "https://example.invalid/v1",
) -> None:
    """写一份「存量 .env」：三行遗留 RERANK_* + 已存在的注册表。

    ``services`` 给需要「脏注册表」的用例（默认仍是 `_services()`）；
    ``rerank_provider`` / ``rerank_base_url`` 传空串即写「键在、值为空」的形态
    （dotenv 读出来是空串 ⇒ 迁移不做那一条准入判断）。
    """
    lines = [
        f"RERANK_PROVIDER={rerank_provider}",
        f"RERANK_BASE_URL={rerank_base_url}",
    ]
    if rerank_model is not None:
        lines.append(f"RERANK_MODEL={rerank_model}")
    lines.append("MODEL_SERVICES=" + _services_json(services if services is not None else _services()))
    lines.append("MODEL_ROLES=" + json.dumps(roles, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _services_json(services: list[dict]) -> str:
    """注册表在 .env 里那一行的**原始值**（与 `_write_legacy_env` 写进去的逐字一致）。"""
    return json.dumps(services, ensure_ascii=False)


def _env_line(path: Path, key: str) -> str:
    """取假 .env 里某个键的**整行原文**：做「逐字节未变」断言用，不做任何解析。"""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line
    raise AssertionError(f".env 里没有 {key} 行")


def _dotenv_ref(path: Path, key: str) -> str:
    """**真实解析器**的读法（python-dotenv；`Settings` 读同一个文件走的就是它）。

    用作 `_read_env_file_key` 的对照基准——不是拿实现自己写的规则自证。
    """
    return dotenv_values(path).get(key, "") or ""


def _env_model_roles(path: Path) -> dict:
    """从假 .env 里取 MODEL_ROLES 的值（顺手证明它没被写成多行）。"""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("MODEL_ROLES="):
            return json.loads(line.split("=", 1)[1])
    raise AssertionError(".env 里没有 MODEL_ROLES 行")


# --------------------------------------------------------------------------
# ① 解析层：rerank 能被解析出来（回归修复 的地基）
# --------------------------------------------------------------------------


def test_roles_parse_rerank(monkeypatch):
    """``MODEL_ROLES`` 里带 rerank 时，``settings.roles`` / ``settings.rerank_binding`` 都取得到。

    挡住「ROLE_KEYS 只有三角色 ⇒ rerank 永远解析不到」这条回归。
    """
    monkeypatch.setattr(
        settings,
        "MODEL_ROLES",
        json.dumps({"chat": CHAT_BINDING, "rerank": RERANK_BINDING}, ensure_ascii=False),
    )
    assert settings.roles["rerank"] == RERANK_BINDING
    assert settings.rerank_binding == RERANK_BINDING


def test_rerank_binding_none_when_absent_or_half(monkeypatch):
    """没绑定 / 半个绑定（缺 service_id 或缺 model）一律 ``None``，与三角色同一口径。"""
    monkeypatch.setattr(settings, "MODEL_ROLES", json.dumps({"chat": CHAT_BINDING}))
    assert settings.rerank_binding is None

    monkeypatch.setattr(settings, "MODEL_ROLES", json.dumps({"rerank": {"service_id": SVC_EMBED}}))
    assert settings.rerank_binding is None

    monkeypatch.setattr(settings, "MODEL_ROLES", json.dumps({"rerank": {"model": "m"}}))
    assert settings.rerank_binding is None


def test_min_rerank_score_key_declared():
    """阈值键就地声明（02 §7.6 的 ``GET.rerank.threshold`` 语义，默认 0.20）。

    只钉「键在、**声明**的默认值是 0.20、运行时是浮点」；阈值过滤本身由
    ``app/retrieval/rerank.py`` 消费。断言钉的是 ``model_fields`` 里的默认值，
    不是当前生效值——本机 ``.env`` 日后覆盖它不该把这条用例弄红。
    """
    default = config_mod.Settings.model_fields["RAG_MIN_RERANK_SCORE"].default
    assert default == pytest.approx(0.20)
    assert isinstance(settings.RAG_MIN_RERANK_SCORE, float)


# --------------------------------------------------------------------------
# ② 往返不丢（P5 的配置层钉子）
# --------------------------------------------------------------------------


def test_dump_registry_round_trip_keeps_rerank(monkeypatch):
    """``dump_registry(services, settings.roles)`` 序列化出的 MODEL_ROLES **仍含 rerank**。

    为什么是钉子：``PUT /settings`` 的角色表是**整表替换**（``dump_registry`` 直接
    ``json.dumps(roles)`` 覆盖 MODEL_ROLES），所以调用方唯一安全的取数口就是
    ``settings.roles``——它现在含 rerank，回写才不会抹掉绑定。
    """
    monkeypatch.setattr(settings, "MODEL_SERVICES", json.dumps(_services(), ensure_ascii=False))
    monkeypatch.setattr(
        settings,
        "MODEL_ROLES",
        json.dumps(
            {"chat": CHAT_BINDING, "embed": EMBED_BINDING, "rerank": RERANK_BINDING},
            ensure_ascii=False,
        ),
    )

    roles = settings.roles
    roles["chat"] = {"service_id": SVC_CHAT, "model": "m-chat-new"}  # 设置页改对话模型
    dumped = json.loads(dump_registry(settings.services, roles)["MODEL_ROLES"])

    assert dumped["rerank"] == RERANK_BINDING
    assert dumped["chat"]["model"] == "m-chat-new"
    assert dumped["embed"] == EMBED_BINDING

    # 反面对照（写明口径，不是冗余断言）：只回传三角色的旧口径**必然**丢 rerank，
    # 因为 dump_registry 是整串替换而非合并——这正是本条目要消灭的丢绑定路径。
    three_only = {key: value for key, value in roles.items() if key in ROLE_KEYS}
    assert "rerank" not in json.loads(dump_registry(settings.services, three_only)["MODEL_ROLES"])


# --------------------------------------------------------------------------
# ③④ 存量一次性迁移：迁移 / 幂等 / 静默跳过
# --------------------------------------------------------------------------


def test_migrate_legacy_rerank_once_then_idempotent(fake_dotenv, monkeypatch):
    """迁移：RERANK_MODEL + 已绑定 embed ⇒ 写入 rerank 绑定；第二次调用什么都不写。"""
    _write_legacy_env(fake_dotenv, rerank_model=LEGACY_RERANK_MODEL, roles={"embed": EMBED_BINDING})
    monkeypatch.setattr(settings, "MODEL_SERVICES", _services_json(_services()))
    monkeypatch.setattr(settings, "MODEL_ROLES", json.dumps({"embed": EMBED_BINDING}, ensure_ascii=False))
    services_line = _env_line(fake_dotenv, "MODEL_SERVICES")

    updates = config_mod.migrate_legacy_rerank()

    assert list(updates) == ["MODEL_ROLES"], "迁移只许写 MODEL_ROLES 一个键（P2-1）"
    assert updates["MODEL_ROLES"], "第一次迁移应返回落盘内容"

    # 磁盘内容与内存内容**各自独立**读一遍再互证：`updates["MODEL_ROLES"] == settings.MODEL_ROLES`
    # 是自指恒真的弱断言（实现刚把同一个字符串赋给内存），钉不住「到底落盘了什么」。
    on_disk = _env_model_roles(fake_dotenv)
    in_memory = json.loads(settings.MODEL_ROLES)
    assert on_disk["rerank"] == RERANK_BINDING                # 绑定落在 embed 那条服务上
    assert in_memory["rerank"] == RERANK_BINDING
    assert on_disk == in_memory                               # 两条独立读取，磁盘 = 内存
    assert settings.roles["rerank"] == RERANK_BINDING
    assert settings.rerank_binding == RERANK_BINDING

    # 修 1 的栅栏：迁移只写 MODEL_ROLES —— 磁盘那行 MODEL_SERVICES 与内存里的都不许动
    assert _env_line(fake_dotenv, "MODEL_SERVICES") == services_line
    assert settings.MODEL_SERVICES == _services_json(_services())

    after_first = fake_dotenv.read_text(encoding="utf-8")
    assert f"RERANK_MODEL={LEGACY_RERANK_MODEL}" in after_first     # 遗留键保留现场，只是不再被读

    # 幂等：第二次必须什么都不写（「已有 rerank 绑定 ⇒ 返回 {}」这条短路）
    assert config_mod.migrate_legacy_rerank() == {}
    assert fake_dotenv.read_text(encoding="utf-8") == after_first


def test_migrate_legacy_rerank_skips_without_embed_binding(fake_dotenv, monkeypatch):
    """只有 chat 绑定（没有 embed）⇒ 返回 ``{}`` 且**一个字节都不写**。"""
    _write_legacy_env(fake_dotenv, rerank_model=LEGACY_RERANK_MODEL, roles={"chat": CHAT_BINDING})
    monkeypatch.setattr(settings, "MODEL_SERVICES", json.dumps(_services(), ensure_ascii=False))
    monkeypatch.setattr(settings, "MODEL_ROLES", json.dumps({"chat": CHAT_BINDING}, ensure_ascii=False))

    before = fake_dotenv.read_text(encoding="utf-8")
    assert config_mod.migrate_legacy_rerank(settings) == {}   # 顺带覆盖 cfg 显式入参这条路径
    assert fake_dotenv.read_text(encoding="utf-8") == before
    assert settings.rerank_binding is None


def test_migrate_legacy_rerank_skips_without_legacy_key(fake_dotenv, monkeypatch):
    """没有遗留 ``RERANK_MODEL``（新装机器就是这样）⇒ 返回 ``{}`` 且不写文件。"""
    _write_legacy_env(fake_dotenv, rerank_model=None, roles={"embed": EMBED_BINDING})
    monkeypatch.setattr(settings, "MODEL_SERVICES", json.dumps(_services(), ensure_ascii=False))
    monkeypatch.setattr(settings, "MODEL_ROLES", json.dumps({"embed": EMBED_BINDING}, ensure_ascii=False))

    before = fake_dotenv.read_text(encoding="utf-8")
    assert config_mod.migrate_legacy_rerank() == {}
    assert fake_dotenv.read_text(encoding="utf-8") == before
    assert settings.rerank_binding is None


def test_migrate_legacy_rerank_skips_without_service_registry(fake_dotenv, monkeypatch):
    """**没有服务注册表就不迁**：否则会写出一条指向不存在服务的绑定，且**永久堵死重试**。

    （对抗性复核点名的悬挂边界：`roles["rerank"]` 一旦写进去，「已有绑定 ⇒ 返回 {}」这条幂等
    短路就会让后续每次启动都不再尝试 ⇒ rerank 永远配不上。）
    """
    _write_legacy_env(fake_dotenv, rerank_model=LEGACY_RERANK_MODEL, roles={"embed": EMBED_BINDING})
    monkeypatch.setattr(settings, "MODEL_SERVICES", "")          # 无注册表
    monkeypatch.setattr(settings, "MODEL_ROLES", json.dumps({"embed": EMBED_BINDING}, ensure_ascii=False))

    before = fake_dotenv.read_text(encoding="utf-8")
    assert settings.has_service_registry is False
    assert config_mod.migrate_legacy_rerank() == {}
    assert fake_dotenv.read_text(encoding="utf-8") == before, "不该动文件"
    assert settings.rerank_binding is None, "不该凭空造一条指向不存在服务的绑定"


def test_migrate_legacy_rerank_skips_when_embed_service_gone(fake_dotenv, monkeypatch, capsys):
    """embed 绑定指向的服务**已不存在** ⇒ ``{}`` + 一行中文说明 + 文件零改动（P2-2）。

    为什么：照迁的话写出的是一条**永远取不到服务**的 rerank 绑定，而「已有绑定 ⇒ 返回 ``{}``」
    这条幂等短路会让后续每次启动都不再重试——等于把「rerank 配不上」永久钉死。
    现场很常见：设置页删掉了服务，`MODEL_ROLES` 里那条绑定还留着。
    """
    gone = {"service_id": SVC_GONE, "model": "m-embed"}
    _write_legacy_env(fake_dotenv, rerank_model=LEGACY_RERANK_MODEL, roles={"embed": gone})
    monkeypatch.setattr(settings, "MODEL_SERVICES", _services_json(_services()))  # 里面没有 svc_gone
    monkeypatch.setattr(settings, "MODEL_ROLES", json.dumps({"embed": gone}, ensure_ascii=False))

    before = fake_dotenv.read_text(encoding="utf-8")
    assert settings.service_by_id(SVC_GONE) is None          # 前提：那条服务确实不在注册表里
    assert config_mod.migrate_legacy_rerank() == {}
    assert fake_dotenv.read_text(encoding="utf-8") == before, "不该动文件"
    assert settings.rerank_binding is None, "不该写出指向不存在服务的绑定"
    out = capsys.readouterr().out
    assert SVC_GONE in out and "跳过" in out                 # 一行中文说明；**不抛**（I5）


def test_migrate_legacy_rerank_never_rewrites_model_services(fake_dotenv, monkeypatch):
    """迁移**只写 MODEL_ROLES**：磁盘 ``MODEL_SERVICES`` 那一行逐字节不变，内存也不变（P2-1）。

    为什么是钉子：`dump_registry` 会**同时**产出重新序列化的 ``MODEL_SERVICES``（丢未知字段、
    api_key 去空格、base_url 去尾斜杠、model_roles 按 models 裁剪）；把它一起落盘 = 迁移顺手改写
    用户的注册表，而内存里还是原值 ⇒ 磁盘/内存当场不一致（复核探针实测到的就是这个）。
    栅栏数据故意做脏：只要实现再往 updates 里塞 ``MODEL_SERVICES``，本用例必红。
    """
    services = _dirty_services()
    embed = {"service_id": SVC_EMBED_DIRTY, "model": "m-embed"}
    _write_legacy_env(
        fake_dotenv, rerank_model=LEGACY_RERANK_MODEL, roles={"embed": embed}, services=services
    )
    monkeypatch.setattr(settings, "MODEL_SERVICES", _services_json(services))
    monkeypatch.setattr(settings, "MODEL_ROLES", json.dumps({"embed": embed}, ensure_ascii=False))

    services_line = _env_line(fake_dotenv, "MODEL_SERVICES")
    memory_before = settings.MODEL_SERVICES
    assert settings.service_by_id(SVC_EMBED_DIRTY) is not None          # 前提：embed 那条服务在
    assert "note" in services_line and "m-unlisted" in services_line    # 前提：.env 里确实带着「脏」字段

    updates = config_mod.migrate_legacy_rerank()

    assert list(updates) == ["MODEL_ROLES"], "迁移只许写 MODEL_ROLES 一个键"
    assert updates["MODEL_ROLES"], "本用例的前提是「真的迁了」，不是「什么都没做」"
    assert _env_line(fake_dotenv, "MODEL_SERVICES") == services_line, "磁盘上的 MODEL_SERVICES 被改写了"
    assert settings.MODEL_SERVICES == memory_before, "内存里的 MODEL_SERVICES 被改写了"
    # 迁移本身仍要成功：钉住「只写一个键」不是靠「整条迁移都没生效」蒙对的
    assert _env_model_roles(fake_dotenv)["rerank"] == {"service_id": SVC_EMBED_DIRTY, "model": LEGACY_RERANK_MODEL}


@pytest.mark.parametrize(
    ("rerank_provider", "rerank_base_url"),
    [
        ("bge", "https://example.invalid/v1"),      # 本地 CrossEncoder 路（v2 无实现）
        ("dashscope", "https://api.jina.ai/v1"),    # 自定义地址：与 embed 那条服务的 base_url 不同
    ],
)
def test_migrate_legacy_rerank_skips_custom_legacy_target(
    fake_dotenv, monkeypatch, capsys, rerank_provider, rerank_base_url
):
    """遗留配置**指向的不是 embed 那条服务** ⇒ 不迁 + 一行显式中文提示 + 文件零改动（P2-5）。

    为什么：照迁的话请求会打到 embed 的 host（本地 CrossEncoder 路与自定义地址都不是 embed
    那条服务）⇒ 404/400 ⇒ 每次静默回落；而 `.env` 那三行又「长得像已配置」，用户查不出来。
    """
    _write_legacy_env(
        fake_dotenv,
        rerank_model=LEGACY_RERANK_MODEL,
        roles={"embed": EMBED_BINDING},
        rerank_provider=rerank_provider,
        rerank_base_url=rerank_base_url,
    )
    monkeypatch.setattr(settings, "MODEL_SERVICES", _services_json(_services()))
    monkeypatch.setattr(settings, "MODEL_ROLES", json.dumps({"embed": EMBED_BINDING}, ensure_ascii=False))

    before = fake_dotenv.read_text(encoding="utf-8")
    assert config_mod.migrate_legacy_rerank() == {}
    assert fake_dotenv.read_text(encoding="utf-8") == before, "不该动文件"
    assert settings.rerank_binding is None, "不该写出指向自定义服务的绑定"
    out = capsys.readouterr().out
    assert "未自动迁移" in out and "重排序" in out             # 显式提示，不静默
    assert "RERANK_MODEL" in before                           # 遗留三行原样保留（只是不再被读）


@pytest.mark.parametrize(
    ("rerank_provider", "rerank_base_url"),
    [
        ("dashscope", "https://example.invalid/v1"),    # 与 embed 那条服务完全一致
        ("DashScope", "https://example.invalid/v1"),    # 厂商名大小写无关
        ("", "https://example.invalid/v1"),             # provider 空 ⇒ 这一条不判断
        ("dashscope", ""),                              # base_url 空 ⇒ 这一条不判断
        ("dashscope", "https://example.invalid/v1/"),   # 尾斜杠不算「不一致」
    ],
)
def test_migrate_legacy_rerank_migrates_when_legacy_target_matches(
    fake_dotenv, monkeypatch, rerank_provider, rerank_base_url
):
    """**反面对照**：遗留配置确实指向 embed 那条服务（或这两行没写）⇒ 照常迁移。

    没有这组对照，上面那条「不迁」用例靠把实现改成「永不迁移」也能绿。
    """
    _write_legacy_env(
        fake_dotenv,
        rerank_model=LEGACY_RERANK_MODEL,
        roles={"embed": EMBED_BINDING},
        rerank_provider=rerank_provider,
        rerank_base_url=rerank_base_url,
    )
    monkeypatch.setattr(settings, "MODEL_SERVICES", _services_json(_services()))
    monkeypatch.setattr(settings, "MODEL_ROLES", json.dumps({"embed": EMBED_BINDING}, ensure_ascii=False))

    updates = config_mod.migrate_legacy_rerank()

    assert updates.get("MODEL_ROLES"), "这条形态该正常迁移"
    assert _env_model_roles(fake_dotenv)["rerank"] == RERANK_BINDING
    assert settings.rerank_binding == RERANK_BINDING


def test_ensure_service_registry_keeps_handwritten_rerank_binding(fake_dotenv, monkeypatch):
    """无注册表时重建注册表，**不得**抹掉手写的 ``MODEL_ROLES.rerank``。

    与已删的 `repair_missing_role_bindings` 同一类地雷：`dump_registry` 是**整串重写**，
    不把既有绑定合并进去，手写的 rerank 就会被静默抹掉（`回归修复`）。
    """
    handwritten = {"service_id": SVC_EMBED, "model": "m-rerank-handwritten"}
    monkeypatch.setattr(settings, "MODEL_SERVICES", "")
    monkeypatch.setattr(settings, "MODEL_ROLES", json.dumps({"rerank": handwritten}, ensure_ascii=False))
    # 扁平键：足够让旧配置迁移出一条服务（否则 build_registry_from_legacy 返回 None，走不到合并）
    monkeypatch.setattr(settings, "MODEL_PROVIDER", "dashscope")
    monkeypatch.setattr(settings, "LLM_API_KEY", "sk-test")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "")
    monkeypatch.setattr(settings, "LLM_MODEL", "m-chat")
    fake_dotenv.write_text("MODEL_PROVIDER=dashscope\nLLM_API_KEY=sk-test\n", encoding="utf-8")

    updates = config_mod.ensure_service_registry()

    assert updates, "本用例的前提是「真的重建了注册表」"
    roles = json.loads(updates["MODEL_ROLES"])
    assert roles["rerank"] == handwritten, "手写的 rerank 绑定被整串重写抹掉了"
    assert settings.roles["rerank"] == handwritten


def test_ensure_service_registry_keeps_unknown_role_keys(fake_dotenv, monkeypatch):
    """重建注册表时，``MODEL_ROLES`` 里**任何**迁移没产出的角色键都要原样留下（不止 4 个已知角色）。

    为什么：`dump_registry` 是整串重写，而合并的数据源若取 `settings.roles`（只遍历
    `ALL_ROLE_KEYS`），用户手写的 `translate` 之类**未知**角色键就会被静默抹掉——注释说的
    「保留手写的非三角色绑定」与实现不一致（回归修复 对抗性复核 P2-4 实测）。
    断言取**逐字相同**：未知键的额外字段（这里是 `extra`）也不许被裁剪或规范化。
    """
    handwritten = {"service_id": SVC_EMBED, "model": "m-rerank-handwritten"}
    translate = {"service_id": SVC_EMBED, "model": "m-translate", "extra": "手写字段也不许动"}
    monkeypatch.setattr(settings, "MODEL_SERVICES", "")
    monkeypatch.setattr(settings, "MODEL_ROLES", json.dumps({"rerank": handwritten, "translate": translate}, ensure_ascii=False))
    # 扁平键：足够让旧配置迁移出一条服务（否则 build_registry_from_legacy 返回 None，走不到合并）
    monkeypatch.setattr(settings, "MODEL_PROVIDER", "dashscope")
    monkeypatch.setattr(settings, "LLM_API_KEY", "sk-test")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "")
    monkeypatch.setattr(settings, "LLM_MODEL", "m-chat")
    fake_dotenv.write_text("MODEL_PROVIDER=dashscope\nLLM_API_KEY=sk-test\n", encoding="utf-8")

    updates = config_mod.ensure_service_registry()

    assert updates, "本用例的前提是「真的重建了注册表」"
    roles = json.loads(updates["MODEL_ROLES"])
    assert roles["rerank"] == handwritten, "已知角色（rerank）的手写绑定被丢掉了"
    assert roles["translate"] == translate, "**未知**角色键被整串重写丢掉了"


def test_migrate_legacy_rerank_already_bound_does_not_touch_env(fake_dotenv, monkeypatch):
    """已有 rerank 绑定时连文件都不读：现值不被遗留键改写（幂等不靠「值一致」碰运气）。"""
    bound = {"service_id": SVC_EMBED, "model": "m-rerank-current"}
    _write_legacy_env(fake_dotenv, rerank_model=LEGACY_RERANK_MODEL, roles={"embed": EMBED_BINDING, "rerank": bound})
    monkeypatch.setattr(settings, "MODEL_SERVICES", json.dumps(_services(), ensure_ascii=False))
    monkeypatch.setattr(settings, "MODEL_ROLES", json.dumps({"embed": EMBED_BINDING, "rerank": bound}, ensure_ascii=False))

    before = fake_dotenv.read_text(encoding="utf-8")
    assert config_mod.migrate_legacy_rerank() == {}
    assert fake_dotenv.read_text(encoding="utf-8") == before
    assert settings.rerank_binding == bound


def test_migrate_legacy_rerank_persist_failure_is_silent(fake_dotenv, monkeypatch, capsys):
    """落盘失败（.env 不可写）⇒ **不抛**（I5）+ 一行中文说明 + 内存里仍生效。"""
    _write_legacy_env(fake_dotenv, rerank_model=LEGACY_RERANK_MODEL, roles={"embed": EMBED_BINDING})
    monkeypatch.setattr(settings, "MODEL_SERVICES", json.dumps(_services(), ensure_ascii=False))
    monkeypatch.setattr(settings, "MODEL_ROLES", json.dumps({"embed": EMBED_BINDING}, ensure_ascii=False))

    def boom(_updates: dict[str, str]) -> None:
        raise OSError("磁盘只读（模拟）")

    monkeypatch.setattr(env_store, "persist_env", boom)

    assert config_mod.migrate_legacy_rerank() == {}          # 落盘失败返回空字典，不抛
    assert settings.rerank_binding == RERANK_BINDING         # 本次运行仍生效
    assert "rerank" in capsys.readouterr().out               # 只打一行中文说明，不做别的


def test_read_env_file_key_rules(fake_dotenv):
    """遗留键的读法 = **python-dotenv 的读法**（P2-3 实测的 4 处分歧全在这里钉住）。

    期望值一律取**探针实测**结果（不按猜测写），并逐条与 ``dotenv_values(路径)`` 本身对照——
    ``Settings`` 读同一个文件走的就是这个解析器，两套解析 = 迁移读到的值可能不是应用生效的值。
    实现若退回手写「dotenv 最小集」，本用例会在引号 / 同名键 / ``export`` / BOM 四条上一起变红。
    """
    cases = [
        ("plain", "# 注释行\nRERANK_MODEL=plain-model\nOTHER=1\n", "plain-model"),
        # 引号 + 行内注释：旧手写实现会读出**带引号**的 "'m x'"（错型号被写进绑定）
        ("quoted_inline_comment", "RERANK_MODEL='m x' # 注释\n", "m x"),
        # 同名键：真实 dotenv 取**最后**一行；旧实现取第一行
        ("dup_key_last_wins", "RERANK_MODEL=first\nRERANK_MODEL=second\n", "second"),
        # export 前缀：旧实现键名匹配不上 ⇒ 读成空串（漏迁）
        ("export_prefix", "export RERANK_MODEL=m-export\n", "m-export"),
        # 文件带 BOM：旧实现首行键名带 \ufeff ⇒ 读成空串（漏迁）
        ("bom", "\ufeffRERANK_MODEL=m-bom\n", "m-bom"),
        # 值里有 # 但**没有前导空格**：dotenv 不当注释（实测），整串保留
        ("hash_without_space", "RERANK_MODEL=m#hash\n", "m#hash"),
        # 空值
        ("empty_value", "RERANK_MODEL=\n", ""),
    ]
    for name, text, expected in cases:
        fake_dotenv.write_text(text, encoding="utf-8")
        assert _dotenv_ref(fake_dotenv, "RERANK_MODEL") == expected, f"{name}: 对照基准与实测不符（算法有变？）"
        assert config_mod._read_env_file_key("RERANK_MODEL") == expected, name
        # 对照：实现读数必须与真实解析器一致（钉住路径/编码/解析选项都没被改歪）
        assert config_mod._read_env_file_key("RERANK_MODEL") == _dotenv_ref(fake_dotenv, "RERANK_MODEL"), name

    # 注释行里出现的同名键不算数；不存在的键 = 空串
    fake_dotenv.write_text("# RERANK_MODEL=nope\nRERANK_MODEL=real\n", encoding="utf-8")
    assert config_mod._read_env_file_key("RERANK_MODEL") == "real"
    assert config_mod._read_env_file_key("MISSING") == ""

    # 文件不存在也不能炸（首启机器上 .env 可能真没有）
    fake_dotenv.unlink()
    assert config_mod._read_env_file_key("RERANK_MODEL") == ""


# --------------------------------------------------------------------------
# ⑤ 回归栅栏：地雷函数不得复生
# --------------------------------------------------------------------------


def test_repair_missing_role_bindings_stays_deleted():
    """``repair_missing_role_bindings`` 已整函数删除，**不得**以任何形式复活。

    删它的理由：① 死代码（全仓只有定义处一处命中、零调用方、零测试）；
    ② 一旦被接线就会 ``roles = dict(target.roles)``（旧实现只解析三角色）
    + ``dump_registry`` **整串重写** MODEL_ROLES ⇒ 静默抹掉手写的 rerank 绑定，
    正是 回归修复 要根除的行为。

    断言必须是**模块级 / 类级 / 实例级**三条（各挡一种复活形态：模块函数 / `Settings` 方法 /
    `Settings` 字段）；只写 `hasattr(settings, ...)` 挡不住「复活成模块函数」，那是本条的原始诉求。
    """
    assert not hasattr(config_mod, "repair_missing_role_bindings")
    assert not hasattr(config_mod.Settings, "repair_missing_role_bindings")
    # 这条不是恒真：pydantic 字段只在**实例**上可见，复活成字段时只有它能抓住
    assert not hasattr(settings, "repair_missing_role_bindings")


# --------------------------------------------------------------------------
# ⑥ 前端冻结面：rerank 不得污染三角色口径
# --------------------------------------------------------------------------


def test_role_list_and_model_roles_still_three_roles():
    """``parse_role_list`` / ``parse_model_roles`` 仍只认 chat/embed/vision。"""
    assert ROLE_KEYS == ("chat", "embed", "vision")
    assert config_mod.ALL_ROLE_KEYS == ("chat", "embed", "vision", RERANK_ROLE)

    assert config_mod.parse_role_list(["rerank", "chat"]) == ["chat"]
    assert config_mod.parse_role_list("rerank") == []
    assert config_mod.parse_model_roles({"m-embed": ["embed", "rerank"]}, ["m-embed"]) == {
        "m-embed": ["embed"]
    }


def test_for_roles_never_contains_rerank(monkeypatch):
    """``for_roles`` 派生（前端读的字段）里绝不出现 ``rerank``。

    只被 rerank 绑定的服务，派生结果为空 ⇒ 回落既有默认 ``["chat"]``
    （与改造前行为一致：rerank 不进前端角色表，但绑定本身在 MODEL_ROLES 里留着）。
    """
    services = _services()
    services[1]["for_roles"] = ["rerank", "embed"]  # 手改 .env 塞进非法角色名
    monkeypatch.setattr(settings, "MODEL_SERVICES", json.dumps(services, ensure_ascii=False))
    monkeypatch.setattr(settings, "MODEL_ROLES", json.dumps({"rerank": RERANK_BINDING}, ensure_ascii=False))

    parsed = settings.services
    for item in parsed:
        assert "rerank" not in item["for_roles"]
    assert parsed[0]["for_roles"] == ["chat"]     # 没有任何三角色绑定 ⇒ 既有默认
    assert parsed[1]["for_roles"] == ["embed"]    # 非法的 rerank 被丢掉，合法的 embed 留下
