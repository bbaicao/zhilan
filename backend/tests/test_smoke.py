"""阶段 0 空包冒烟：确认目录骨架各包可 import。"""


def test_smoke_import_app_packages():
    import app.api, app.core, app.db, app.vector, app.pipeline
    import app.retrieval, app.agent, app.generation, app.session, app.observe, app.system
