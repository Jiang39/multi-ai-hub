"""chat_sync 解析层回归测试（mock 上游，不联网、不读真实凭据）。

覆盖曾经出现的静默 bug：上游 OpenAI SSE chunk 被当成 Anthropic 事件解析，
导致 HTTP 200 但 content/usage 全空。这里用假 chunk 断言解析正确。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.workbuddy_native as wn  # noqa: E402


def sse(chunks: list[dict]) -> str:
    """把 dict 列表拼成上游 OpenAI SSE 文本。"""
    lines = ["data: " + json.dumps(c, ensure_ascii=False) for c in chunks]
    return "\n".join(lines) + "\n\n"


def fake_cred() -> wn.Credential:
    return wn.Credential(
        uid="test-uid", uin="test-uin", nickname="test",
        access_token="fake-token", refresh_token=None,
        expires_at=None, refresh_expires_at=None, domain=None, enterprise_id=None,
    )


class TestChatSyncParsing(unittest.TestCase):
    def setUp(self) -> None:
        # 完全隔离账号池与模型配置，不触碰真实文件
        self.patcher_cfg = patch.object(wn, "_load_cfg", return_value={})
        self.patcher_pool = patch.object(wn.POOL, "pick", return_value=fake_cred())
        self.patcher_token = patch.object(wn.POOL, "token_for", return_value="fake-token")
        self.patcher_fwd = patch.object(wn, "forward_chat")
        self.patcher_log = patch.object(wn, "_log_usage")
        self.mock_cfg = self.patcher_cfg.start()
        self.patcher_pool.start()
        self.patcher_token.start()
        self.mock_fwd = self.patcher_fwd.start()
        self.patcher_log.start()
        self.addCleanup(self.patcher_cfg.stop)
        self.addCleanup(self.patcher_pool.stop)
        self.addCleanup(self.patcher_token.stop)
        self.addCleanup(self.patcher_fwd.stop)
        self.addCleanup(self.patcher_log.stop)

    def test_text_content_and_usage(self) -> None:
        """回归：文本与 usage 必须从 OpenAI chunk 正确解析，不能为空。"""
        self.mock_fwd.return_value = (200, sse([
            {"id": "cmb-1", "model": "deepseek-v4.1-flash",
             "choices": [{"index": 0, "delta": {"content": "收到"}}]},
            {"id": "cmb-1", "model": "deepseek-v4.1-flash",
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"id": "cmb-1", "model": "deepseek-v4.1-flash", "choices": [],
             "usage": {"prompt_tokens": 36, "completion_tokens": 16,
                       "total_tokens": 52, "prompt_cache_hit_tokens": 20}},
        ]))
        r = wn.chat_sync("deepseek-v4.1-flash",
                         {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 64})
        self.assertNotIn("error", r, r)
        msg = r["choices"][0]["message"]
        self.assertEqual(msg["content"], "收到")
        self.assertEqual(r["usage"]["prompt_tokens"], 36)
        self.assertEqual(r["usage"]["completion_tokens"], 16)
        self.assertEqual(r["choices"][0]["finish_reason"], "stop")

    def test_tool_call(self) -> None:
        """回归：tool_calls 必须按 index 聚合成 tool_use 块。"""
        self.mock_fwd.return_value = (200, sse([
            {"id": "cmb-2", "model": "deepseek-v4.1-flash", "choices": [{"index": 0, "delta": {
                "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                "function": {"name": "get_weather", "arguments": "{\"city\":"}}]}}]},
            {"id": "cmb-2", "model": "deepseek-v4.1-flash", "choices": [{"index": 0, "delta": {
                "tool_calls": [{"index": 0, "function": {"arguments": "\"Beijing\"}"}}]}}]},
            {"id": "cmb-2", "model": "deepseek-v4.1-flash",
             "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]))
        r = wn.chat_sync("deepseek-v4.1-flash",
                         {"messages": [{"role": "user", "content": "weather?"}], "max_tokens": 64})
        self.assertNotIn("error", r, r)
        choice = r["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        tcs = choice["message"]["tool_calls"]
        self.assertEqual(len(tcs), 1)
        self.assertEqual(tcs[0]["function"]["name"], "get_weather")
        self.assertEqual(json.loads(tcs[0]["function"]["arguments"]), {"city": "Beijing"})

    def test_upstream_error(self) -> None:
        """429 应返回 error 而不是空内容，且不抛异常。"""
        self.mock_fwd.return_value = (429, "rate limited")
        r = wn.chat_sync("deepseek-v4.1-flash",
                         {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8})
        self.assertIn("error", r)
        self.assertEqual(r["error"]["status"], 429)


if __name__ == "__main__":
    unittest.main(verbosity=2)
