"""工具定义格式回归：Trae 要求 parameters 为 JSON 字符串，其它平台为对象。"""
from __future__ import annotations

import json
import unittest

from core.protocols import attach_tools


TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "天气",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}]


class ToolFormatTests(unittest.TestCase):
    def test_trae_parameters_is_string(self) -> None:
        body: dict = {}
        attach_tools(body, {"tools": TOOLS}, "traework")
        params = body["tools"][0]["function"]["parameters"]
        self.assertIsInstance(params, str)
        self.assertEqual(json.loads(params), TOOLS[0]["function"]["parameters"])

    def test_other_platforms_keep_object(self) -> None:
        for platform in ("loomy", "xiaohuanxiong", "qoder-intl", "qwenwork", ""):
            body: dict = {}
            attach_tools(body, {"tools": TOOLS}, platform)
            self.assertIsInstance(body["tools"][0]["function"]["parameters"], dict, platform)


if __name__ == "__main__":
    unittest.main()
