"""Two independently invoked pytest processes share only a disposable SQLite file."""
import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.core.config import settings
from app.models import Base, Message
from app.generation import answer
from app.session import memory, repository, service
from tests.session.test_installed_repair_s5 import FACTS, QUESTIONS, row, fixed_provider, capture


def restart_engine():
    return create_async_engine("sqlite+aiosqlite:///" + os.environ["S5_RESTART_DB"].replace("\\", "/"))


@pytest.mark.asyncio
async def test_seed_persistent_memory_and_summary(monkeypatch):
    engine = restart_engine()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            db.add_all([row(fact) for fact in FACTS])
            chat = await repository.create_chat(db, title="independent-process-restart")
            db.add_all([
                Message(id=85, chat_id=chat.id, role="user", content="First marker is RESTART-CONTROL-919."),
                Message(id=86, chat_id=chat.id, role="assistant", content="OK"),
                Message(id=87, chat_id=chat.id, role="user", content="Preserve this decision."),
                Message(id=88, chat_id=chat.id, role="assistant", content="OK"),
            ])
            chat.context_summary = "First marker is RESTART-CONTROL-919."
            chat.compressed_up_to = 86
            await db.commit()
            capture("restart-seed", {"pid": os.getpid(), "chat_id": chat.id})
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_read_persistent_memory_and_summary_after_restart(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", True)
    engine = restart_engine()
    bodies = []
    provider = fixed_provider(bodies)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            chat = await repository.get_chat(db, 1)
            assert chat.context_summary == "First marker is RESTART-CONTROL-919."
            facts = await memory.recall(db, question=QUESTIONS[0])
            assert set(item.text for item in facts) == set(FACTS)
            history = await service.build_model_history(db, 1, chat=chat, model=None)
            await answer.answer_direct(QUESTIONS[0], history=history,
                                       memory_text=memory.render_block(facts),
                                       model=provider.model("controlled", context_window=8192))
            sent = "\n".join(item.get("content", "") for item in bodies[0]["messages"])
            assert all(fact in sent for fact in FACTS)
            assert chat.context_summary in sent
            capture("restart-read", {"pid": os.getpid(), "bodies": bodies,
                                     "quality": "Unverified; fixed provider"})
    finally:
        await provider._client.close()
        await engine.dispose()
