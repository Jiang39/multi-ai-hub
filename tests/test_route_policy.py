"""会话级 sticky 路由回归测试：同一会话固定渠道，失败迁移后记住。"""
from __future__ import annotations

import unittest

from core.route_policy import get_sticky_router


class StickyRouterTests(unittest.TestCase):
    def test_same_session_sticks(self) -> None:
        routes = (("workbuddy", "m"), ("traework", "m"), ("qwenwork", "m"))
        r = get_sticky_router(routes)
        logged = {p for p, _ in routes}
        first = r.pick("session-A", logged)
        for _ in range(5):
            self.assertEqual(r.pick("session-A", logged), first)

    def test_different_sessions_spread(self) -> None:
        routes = (("workbuddy", "m"), ("traework", "m"), ("qwenwork", "m"),
                  ("loomy", "m"), ("qoder-intl", "m"), ("xiaohuanxiong", "m"))
        r = get_sticky_router(routes)
        logged = {p for p, _ in routes}
        picks = {r.pick(f"session-{i}", logged)[0] for i in range(60)}
        self.assertGreaterEqual(len(picks), 3, f"渠道分布过窄：{picks}")

    def test_advance_remembers(self) -> None:
        routes = (("workbuddy", "m"), ("traework", "m"), ("qwenwork", "m"))
        r = get_sticky_router(routes)
        logged = {p for p, _ in routes}
        first = r.pick("session-B", logged)
        second = r.advance("session-B", logged)
        self.assertNotEqual(first, second)
        self.assertEqual(r.pick("session-B", logged), second)


if __name__ == "__main__":
    unittest.main()
