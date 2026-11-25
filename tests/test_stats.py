"""统计口径回归测试：不联网、不读真实凭据，用固定 fixture 验证解析逻辑。"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from core import workbuddy_data as wd


class SigninGainsTests(unittest.TestCase):
    @staticmethod
    def _today_stamp() -> str:
        # signin_gains() 的 today 按运行时当天(UTC+8)归日，fixture 必须动态取当天，
        # 否则跨天后固定日期会落到 last7、today 恒为 0，测试假失败。
        tz = timezone(timedelta(hours=8))
        return datetime.now(tz).strftime("%Y-%m-%d 10:00:00")

    def _run(self, lines: list[tuple[str, dict]]) -> dict:
        text = "\n".join(
            f"[{stamp}] " + json.dumps(payload, ensure_ascii=False)
            for stamp, payload in lines
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "signin.log"
            p.write_text(text, encoding="utf-8")
            with patch.object(wd, "SIGNIN_LOG", p):
                return wd.signin_gains()

    def test_single_account_gifts_and_redeem(self):
        payload = {
            "result": "ALL",
            "accounts": [{
                "uid": "u1",
                "report": "成功领取 100 积分；领旅行礼物 +15 积分；"
                          "领任务奖「看广告」+credit50+energy5；"
                          "连登兑换「进阶」（+50 积分）；今日 +100",
            }],
        }
        gains = self._run([(self._today_stamp(), payload)])
        # 签到分 100（取一次）+ 礼物 15 + 任务 50 + 连登 50；今日+100 不重复加
        self.assertEqual(gains["today"], 215.0)

    def test_multi_account_already_not_double_counted(self):
        payload = {
            "result": "ALL",
            "accounts": [
                {"uid": "u1", "report": "今日 +100，今日 +100；领旅行礼物 +15"},
                {"uid": "u2", "report": "成功领取 200 积分"},
            ],
        }
        gains = self._run([(self._today_stamp(), payload)])
        # u1: 100 + 15，u2: 200
        self.assertEqual(gains["today"], 315.0)


class Fail429PatternTests(unittest.TestCase):
    def test_arrow_and_plain_formats(self):
        from core.workbuddy_data import FAIL_429_RE
        samples = [
            "[2026-11-24T07:39:03.750Z] ← deepseek-v4.1-flash 失败 HTTP 429",
            "[2026-11-22T12:57:06.935Z]    deepseek-v4.1-flash 失败 HTTP 429，放弃",
            "[2026-11-21T16:10:25.225Z]    hy4flash-free 失败 HTTP 429",
        ]
        for line in samples:
            m = FAIL_429_RE.match(line)
            self.assertIsNotNone(m, line)
            self.assertIn(m.group(2), ("deepseek-v4.1-flash", "hy4flash-free"))


if __name__ == "__main__":
    unittest.main()
