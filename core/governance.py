"""统一账号池治理层：sticky 选号 / 冷却 / preflight / 健康检测。

边界（重要）：WorkBuddy 有自己的 6 号池和完整的 429 换号 + token 刷新，
在 core/workbuddy_native.py 里实现，不走本层。本层服务于新 5 家平台——
它们目前每家只有一个本机登录态账号，所以「429 换号」实际退化为
「模型级冷却 + 明确等待时间」，不会假装存在多账号轮换。

所有状态落盘到 data/hub-cooldowns.json，重启后仍生效。网关(8899)和面板(8798)
是不同进程，所以每次访问都从磁盘重载，以文件为唯一事实来源。
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from pathlib import Path
from typing import Any

from .adapter import DATA_DIR, read_json, write_json
from .models import Account

COOL_FILE = DATA_DIR / "hub-cooldowns.json"
DEFAULT_ACCOUNT_MS = 60_000
DEFAULT_MODEL_MS = 60_000
FAIL_THRESHOLD = 3
DISABLE_MS = 5 * 60_000
MAX_COOL_MS = 24 * 3600_000


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_retry_ms(text: str) -> int | None:
    """从上游错误体里猜重置时间。数值 <1e12 当秒，否则当毫秒。"""
    if not text:
        return None
    m = re.search(r'"(?:reset|reset_at|resetAt|retryAfter|retry_after|retry_after_ms)"\s*:\s*"?(\d+(?:\.\d+)?)"?', text, re.I)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    return int(v * 1000) if v < 1e12 else int(v)


class Governance:
    """线程安全 + 跨进程一致的冷却/健康状态存储。mark 才写盘。"""

    def __init__(self, path: Path = COOL_FILE) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.data: dict[str, Any] = {}

    def _load(self) -> None:
        """调用方必须已持有 self.lock。以磁盘为准重载。"""
        raw = read_json(self.path, {})
        self.data = raw if isinstance(raw, dict) else {}
        for bucket in ("accounts", "models", "health"):
            if not isinstance(self.data.get(bucket), dict):
                self.data[bucket] = {}

    def _write(self) -> None:
        write_json(self.path, self.data)

    def _prune(self) -> None:
        now = now_ms()
        for bucket in ("accounts", "models"):
            for key, val in list(self.data[bucket].items()):
                try:
                    expired = int(val) <= now
                except (TypeError, ValueError):
                    expired = True
                if expired:
                    self.data[bucket].pop(key, None)

    @staticmethod
    def account_key(platform: str, uid: str) -> str:
        return f"{platform}|{uid}"

    @staticmethod
    def model_key(platform: str, uid: str, model: str) -> str:
        return f"{platform}|{uid}|{model}"

    # --- 冷却 -------------------------------------------------------------
    def account_until(self, platform: str, uid: str) -> int:
        with self.lock:
            self._load()
            self._prune()
            try:
                return int(self.data["accounts"].get(self.account_key(platform, uid), 0) or 0)
            except (TypeError, ValueError):
                return 0

    def model_until(self, platform: str, uid: str, model: str) -> int:
        with self.lock:
            self._load()
            self._prune()
            try:
                return int(self.data["models"].get(self.model_key(platform, uid, model), 0) or 0)
            except (TypeError, ValueError):
                return 0

    def mark_account(self, platform: str, uid: str, until_ms: int | None = None,
                     ms: int = DEFAULT_ACCOUNT_MS) -> int:
        now = now_ms()
        until = min(max(until_ms if until_ms is not None else now + max(1000, ms), now + 1000), now + MAX_COOL_MS)
        with self.lock:
            self._load()
            key = self.account_key(platform, uid)
            self.data["accounts"][key] = max(until, int(self.data["accounts"].get(key, 0) or 0))
            self._write()
            return int(self.data["accounts"][key])

    def mark_model(self, platform: str, uid: str, model: str, until_ms: int | None = None,
                   ms: int = DEFAULT_MODEL_MS) -> int:
        now = now_ms()
        until = min(max(until_ms if until_ms is not None else now + max(1000, ms), now + 1000), now + MAX_COOL_MS)
        with self.lock:
            self._load()
            key = self.model_key(platform, uid, model)
            self.data["models"][key] = max(until, int(self.data["models"].get(key, 0) or 0))
            self._write()
            return int(self.data["models"][key])

    def clear(self, platform: str, uid: str, model: str | None = None) -> None:
        with self.lock:
            self._load()
            self.data["accounts"].pop(self.account_key(platform, uid), None)
            if model:
                self.data["models"].pop(self.model_key(platform, uid, model), None)
            self._write()

    # --- 健康 -------------------------------------------------------------
    def health_state(self, platform: str, uid: str) -> dict[str, Any]:
        with self.lock:
            self._load()
            st = self.data["health"].get(self.account_key(platform, uid))
            return dict(st) if isinstance(st, dict) else {}

    def is_disabled(self, platform: str, uid: str) -> bool:
        try:
            return int(self.health_state(platform, uid).get("disabledUntil", 0) or 0) > now_ms()
        except (TypeError, ValueError):
            return False

    def note_failure(self, platform: str, uid: str, error: str) -> None:
        with self.lock:
            self._load()
            key = self.account_key(platform, uid)
            st = dict(self.data["health"].get(key) or {})
            st["fails"] = int(st.get("fails", 0) or 0) + 1
            st["lastError"] = str(error)[:200]
            st["lastAt"] = now_ms()
            if st["fails"] >= FAIL_THRESHOLD:
                st["disabledUntil"] = now_ms() + DISABLE_MS
                st["fails"] = 0
            self.data["health"][key] = st
            self._write()

    def note_success(self, platform: str, uid: str) -> None:
        with self.lock:
            self._load()
            key = self.account_key(platform, uid)
            st = self.data["health"].get(key)
            if not isinstance(st, dict):
                return
            changed = bool(st.get("fails") or st.get("disabledUntil"))
            st["fails"] = 0
            st.pop("disabledUntil", None)
            st["lastOkAt"] = now_ms()
            if changed:
                self._write()

    # --- 选号 -------------------------------------------------------------
    def pick(self, platform: str, accounts: list[Account], model: str, *,
             session_key: str | None = None, preferred: str | None = None) -> Account | None:
        """按 sticky 哈希选号，跳过禁用/冷却；全冷却时返回 sticky 命中的账号。"""
        usable = [a for a in accounts if not self.is_disabled(platform, a.uid)]
        if preferred:
            for a in usable:
                if a.uid == preferred or a.uin == preferred:
                    return a
        if not usable:
            return None
        if session_key:
            digest = hashlib.sha256(session_key.encode("utf-8")).digest()
            start = int.from_bytes(digest[:8], "big") % len(usable)
        else:
            start = 0
        for i in range(len(usable)):
            acct = usable[(start + i) % len(usable)]
            if not self.account_until(platform, acct.uid) and not self.model_until(platform, acct.uid, model):
                return acct
        return usable[start % len(usable)]

    def wait_reason(self, platform: str, uid: str, model: str) -> str:
        """给客户端看的等待说明，不暴露凭据。"""
        parts = []
        for name, until in (("账号", self.account_until(platform, uid)),
                            ("模型", self.model_until(platform, uid, model))):
            if until > now_ms():
                sec = (until - now_ms()) // 1000
                parts.append(f"{name}冷却剩余约 {sec}s")
        st = self.health_state(platform, uid)
        if self.is_disabled(platform, uid):
            sec = (int(st.get("disabledUntil", 0)) - now_ms()) // 1000
            parts.append(f"账号连续失败已临时停用，剩余约 {sec}s")
        return "；".join(parts)

    def snapshot(self) -> dict[str, Any]:
        """供 /api/health 展示，不含敏感字段。"""
        with self.lock:
            self._load()
            self._prune()
            return {
                "accounts": dict(self.data["accounts"]),
                "models": dict(self.data["models"]),
                "health": {
                    k: {"fails": v.get("fails", 0), "disabled": bool(
                        int(v.get("disabledUntil", 0) or 0) > now_ms()),
                        "lastError": v.get("lastError", "")}
                    for k, v in self.data["health"].items() if isinstance(v, dict)
                },
            }


GOVERNANCE = Governance()
