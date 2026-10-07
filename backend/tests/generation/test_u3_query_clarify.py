"""U3.5：有歧义的长追问复用澄清门，失败和轮数上限仍回原链路。"""

import pytest

from app.generation.clarify import judge_query
from cubeloop.providers.faux import faux_assistant_message


class ClarifyModel:
    def __init__(self, *, broken=False):
        self.calls = 0
        self.broken = broken

    async def generate(self, *_args, **_kwargs):
        self.calls += 1
        if self.broken:
            raise RuntimeError("injected clarify failure")
        return faux_assistant_message("你指的是哪一个设备？")


@pytest.mark.asyncio
async def test_long_unresolved_reference_forces_existing_clarify():
    model = ClarifyModel()
    verdict = await judge_query(
        "它是否符合预算3000元并且不要订阅服务的要求？",
        [],
        force=True,
        enabled=True,
        limit=2,
        model=model,
    )
    assert verdict.is_clarify and verdict.reason == "unresolved_reference"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_failed_clarify_keeps_original_answer_path():
    model = ClarifyModel(broken=True)
    verdict = await judge_query(
        "它是否符合预算3000元并且不要订阅服务的要求？",
        force=True,
        enabled=True,
        limit=2,
        model=model,
    )
    assert verdict.clear and verdict.reason == "no_question"
    assert model.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,rounds", [(False, 0), (True, 2)])
async def test_force_preserves_existing_switch_and_round_limit(enabled, rounds):
    model = ClarifyModel()
    verdict = await judge_query(
        "它是否符合预算3000元并且不要订阅服务的要求？",
        force=True,
        enabled=enabled,
        clarify_rounds=rounds,
        limit=2,
        model=model,
    )
    assert verdict.clear and model.calls == 0
    assert verdict.assume == (rounds == 2)
