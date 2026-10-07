"""SSE 流式协议契约：/api/chat/stream 7 类事件（error/meta/reasoning/token/agent/stopped/done）+ generate-note/stream 四事件。

阶段0口径：
- 源码静态分析为主（核对事件 type 与字段）
- live 烟测标记 @pytest.mark.live，默认不跑（会写库/烧模型）
"""

import re
from pathlib import Path

import pytest


# --- 源码静态分析：/api/chat/stream 7 类事件 ---------------------------------


class TestChatStreamSSESource:
    """从 api/__init__.py 源码核对 SSE 7 类事件协议。"""

    @pytest.fixture(scope="class")
    def api_source(self, old_repo_backend: Path) -> str:
        return (old_repo_backend / "app" / "api" / "__init__.py").read_text(encoding="utf-8")

    def test_sse_helper_function(self, api_source):
        """_sse() 辅助函数存在，产出 data: <JSON>\\n\\n 格式。"""
        assert "def _sse" in api_source
        assert "data: " in api_source
        assert "ensure_ascii=False" in api_source

    def test_event_error(self, api_source):
        """error 事件：type=error, detail 字段。"""
        assert re.search(r'\{"type":\s*"error",\s*"detail"', api_source)

    def test_event_meta(self, api_source):
        """meta 事件：type=meta, chat_id, skill_ids。"""
        assert re.search(r'\{"type":\s*"meta",\s*"chat_id"', api_source)
        assert '"skill_ids"' in api_source

    def test_event_reasoning(self, api_source):
        """reasoning 事件：type=reasoning, text 字段。"""
        assert re.search(r'\{"type":\s*"reasoning",\s*"text"', api_source)

    def test_event_token(self, api_source):
        """token 事件：type=token, text 字段。"""
        assert re.search(r'\{"type":\s*"token",\s*"text"', api_source)

    def test_event_agent(self, api_source):
        """agent 事件：type=agent, step 字段。"""
        assert re.search(r'\{"type":\s*"agent",\s*"step"', api_source)

    def test_event_stopped(self, api_source):
        """stopped 事件：type=stopped, chat_id 字段。"""
        assert re.search(r'\{"type":\s*"stopped",\s*"chat_id"', api_source)

    def test_event_done_fields(self, api_source):
        """done 事件：含 type 共 10 键 = type 标识 + 9 个业务键。

        业务键 9 个：chat_id, refs, kind, reasoning, thinking, skill_ids, agent_trace, answer_source, recovered。
        注意：recovered 系 v1.0.12 起补入的第 9 个业务键（旧「九字段 / 8 个业务字段 + type = 9 键」口径已勘误，见 02 契约 §8.1③）；
        本用例逐字锁定的 required_fields 仍为前 8 个历史业务键，recovered 由契约文档计数口径（含 type 共 10 键 / 业务 9 键）钉住。
        """
        # 找到 done 事件的 yield 块（多行 dict，用更宽松的匹配）
        done_match = re.search(
            r'"type":\s*"done",\s*\n\s*"chat_id".*?"answer_source"',
            api_source,
            re.DOTALL,
        )
        assert done_match, "找不到 done 事件的 yield 块"
        done_block = done_match.group(0)
        # 十键核对：本用例锁定 8 个历史业务键（recovered 不在断言内）
        required_fields = [
            "chat_id",
            "refs",
            "kind",
            "reasoning",
            "thinking",
            "skill_ids",
            "agent_trace",
            "answer_source",
        ]
        for field in required_fields:
            assert f'"{field}"' in done_block, f"done 事件缺少字段 {field}"

    def test_sse_media_type(self, api_source):
        """StreamingResponse media_type 为 text/event-stream。"""
        assert "text/event-stream" in api_source

    def test_sse_headers(self, api_source):
        """SSE 响应头包含 Cache-Control: no-cache 与 X-Accel-Buffering: no。"""
        assert "no-cache" in api_source
        assert "X-Accel-Buffering" in api_source

    def test_empty_text_400_before_stream(self, api_source):
        """空 text 且无图 → 400「请输入问题」，在流开始前。"""
        assert "请输入问题" in api_source

    def test_image_too_large_413(self, api_source):
        """图片超限 → 413「文件超过限制，请压缩后重试」。"""
        assert "文件超过限制，请压缩后重试" in api_source

    def test_agent_mode_validated_before_stream(self, api_source):
        """Agent 模式在流开始前定死，非法值 400。"""
        assert "未知的 Agent 模式" in api_source

    def test_stop_mechanism(self, api_source):
        """停止机制：request_id 登记 asyncio.Event，流内检查。"""
        assert "_chat_stop_events" in api_source
        assert "request_id" in api_source

    def test_skill_unavailable_400(self, api_source):
        """skills 预检失败 → 400（SkillUnavailable）。"""
        assert "SkillUnavailable" in api_source


# --- 源码静态分析：generate-note/stream 四事件 -----------------------------


class TestGenerateNoteStreamSource:
    """从 api/__init__.py 源码核对 generate-note/stream 四事件子协议。"""

    @pytest.fixture(scope="class")
    def api_source(self, old_repo_backend: Path) -> str:
        return (old_repo_backend / "app" / "api" / "__init__.py").read_text(encoding="utf-8")

    def test_gn_stream_event_status(self, api_source):
        """status 事件：type=status, detail 字段。"""
        assert re.search(r'\{"type":\s*"status",\s*"detail"', api_source)

    def test_gn_stream_event_token(self, api_source):
        """token 事件：type=token, text 字段。"""
        # generate-note/stream 里的 token 事件
        assert re.search(r'\{"type":\s*"token",\s*"text"', api_source)

    def test_gn_stream_event_error(self, api_source):
        """error 事件：type=error, detail 字段。"""
        assert re.search(r'\{"type":\s*"error",\s*"detail"', api_source)

    def test_gn_stream_done_fields(self, api_source):
        """done 事件携带 GeneratedNoteOut 形状字段。"""
        done_match = re.search(
            r'"type":\s*"done",\s*\n\s*"title".*?"completeness"',
            api_source,
            re.DOTALL,
        )
        assert done_match, "找不到 generate-note/stream 的 done 事件"
        done_block = done_match.group(0)
        required = ["title", "content", "category", "tags", "source_document_id", "source_filename", "completeness"]
        for field in required:
            assert f'"{field}"' in done_block, f"generate-note/stream done 缺少字段 {field}"

    def test_gn_stream_completeness_structure(self, api_source):
        """done 事件的 completeness 子结构：complete/checker/checked/missing。"""
        assert '"complete"' in api_source
        assert '"checker"' in api_source
        assert '"checked"' in api_source
        assert '"missing"' in api_source

    def test_gn_stream_media_type(self, api_source):
        """generate-note/stream 也用 text/event-stream。"""
        # 文件中有两处 StreamingResponse，都是 text/event-stream
        count = api_source.count("text/event-stream")
        assert count >= 2


# --- live 烟测（标记 live，默认不跑） --------------------------------------


@pytest.mark.live
class TestChatStreamSSELive:
    """对 /api/chat/stream 做 live 烟测（最小请求，**不创建会话**）。

    阶段 1 条目 1.5 起该端点已实装 → 两条断言不再 xfail、真实生效。
    两个用例都传空 message / 非法 mode，**在流开始前就被 400 拦掉**，
    因此不会建会话、不会写库。

    注意：这会创建会话并写库。仅在明确需要 live 基线时运行：
        pytest tests/contract/test_sse_protocol.py -m live
    """

    def test_stream_smoke_minimal(self, http_client, contract_target):
        """最小请求：空 message 应在流开始前返回 400（不吐半截 SSE）。"""
        resp = http_client.post("/api/chat/stream", json={"message": ""})
        assert resp.status_code == 400
        assert "请输入问题" in resp.json().get("detail", "")
        # 关键：必须是普通 JSON 错误，而不是先吐了 SSE 再报错
        assert "text/event-stream" not in resp.headers.get("content-type", "")

    def test_stream_smoke_bad_mode(self, http_client, contract_target):
        """非法 agent_mode 应在流开始前返回 400。"""
        resp = http_client.post(
            "/api/chat/stream",
            json={"message": "hi", "agent_mode": "bad_mode"},
        )
        assert resp.status_code == 400
        assert "text/event-stream" not in resp.headers.get("content-type", "")
