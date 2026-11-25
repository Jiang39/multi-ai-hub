"""会话级 sticky 路由：同一对话固定一个渠道，保住上游 prompt caching。

注意：只用于 OpenAI 端点（无状态 OpenAI 结构）。Anthropic 原生端点保持 WorkBuddy 优先，
避免跨平台 tool/流式协议不一致导致上下文丢失或服务报错。
"""
from __future__ import annotations

import hashlib
import threading
from typing import Iterable


_STICKY_ROUTERS: dict[tuple[tuple[str, str], ...], "StickyRouter"] = {}
_STICKY_LOCK = threading.Lock()


def get_sticky_router(routes: Iterable[tuple[str, str]]) -> "StickyRouter":
    """按 routes 复用同一个 StickyRouter 实例，保证进程内跨请求记住会话渠道。"""
    key = tuple(routes)
    with _STICKY_LOCK:
        router = _STICKY_ROUTERS.get(key)
        if router is None:
            router = StickyRouter(key)
            _STICKY_ROUTERS[key] = router
        return router


class StickyRouter:
    """按会话哈希选渠道；失败迁移后记住新渠道，避免同会话来回跳。"""

    def __init__(self, routes: Iterable[tuple[str, str]]) -> None:
        self.routes = tuple(routes)
        self._sticky: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()

    def _candidates(self, logged: set[str] | None) -> list[tuple[str, str]]:
        if not logged:
            return list(self.routes)
        usable = [r for r in self.routes if r[0] in logged]
        return usable or list(self.routes)

    def pick(self, session_key: str, logged: set[str] | None = None) -> tuple[str, str]:
        candidates = self._candidates(logged)
        if not candidates:
            return "", ""
        cache_key = f"{session_key}|{self.routes}"
        with self._lock:
            sticky = self._sticky.get(cache_key)
            if sticky and sticky in candidates:
                return sticky
            digest = hashlib.sha256(cache_key.encode("utf-8")).digest()
            chosen = candidates[int.from_bytes(digest[:8], "big") % len(candidates)]
            self._sticky[cache_key] = chosen
            return chosen

    def advance(self, session_key: str, logged: set[str] | None = None) -> tuple[str, str]:
        """当前渠道失败时切到下一个，并记住，后续同会话保持稳定。"""
        candidates = self._candidates(logged)
        if not candidates:
            return "", ""
        cache_key = f"{session_key}|{self.routes}"
        with self._lock:
            cur = self._sticky.get(cache_key)
            if cur is None:
                return self.pick(session_key, logged)
            try:
                idx = candidates.index(cur)
            except ValueError:
                idx = -1
            nxt = candidates[(idx + 1) % len(candidates)]
            self._sticky[cache_key] = nxt
            return nxt
