"""``tests/search`` 的公共说明（阶段 3 · 条目 3.4）。

用例分两层，与老仓库 ``tests/test_search_operators.py`` 同一套口径：

- ``test_query.py``：离线纯函数（解析、文案），不连库、不连任何服务；
- ``tests/api/test_search_endpoint.py``：走 HTTP + 真测试库（夹具在 ``tests/conftest.py``
  与 ``tests/api/test_chat_contract.py`` 的 ``client``）。

本目录**不需要额外夹具**：解析用例连 DB 都不用，端点用例直接复用 ``client`` / ``db``。
"""
