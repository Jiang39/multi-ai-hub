"""WorkBuddy 适配器（自包含）。

- 账号与积分：读 runtime/pool/*.info + 官方资源接口（core/workbuddy_data.py）
- 模型级限流：读 runtime/ratelimit.json 的 models 字段
- 签到状态：读 runtime/signin/signin.log（签到由外部调度器负责）
- 反代 chat：直接调用 core/workbuddy_native.py 的原生协议层

不依赖任何外部目录或遗留网关。
"""

from __future__ import annotations

import json
import time
from typing import Any

from core import workbuddy_data as wd
from core.adapter import PlatformAdapter
from core.models import Account, Credits, ModelLimit, SigninResult, UsageRecord

RL_FILE = wd.RL_FILE

PLATFORM = "workbuddy"


def _wb_module():
    return wd


# wb.collect() 要 2.3 秒（6 个号逐个查官方接口），一次面板请求里会被调用 7 次。
# 用短 TTL 缓存：首次查一次，后续复用，整页从 ~17s 降到 ~3s。
_COLLECT_CACHE: list[dict[str, Any]] = []
_COLLECT_AT = 0.0
COLLECT_TTL = 8.0
_COLLECT_LOCK = __import__("threading").Lock()


def _collect_cached() -> list[dict[str, Any]]:
    global _COLLECT_CACHE, _COLLECT_AT
    with _COLLECT_LOCK:
        if _COLLECT_CACHE and time.monotonic() - _COLLECT_AT < COLLECT_TTL:
            return _COLLECT_CACHE
        _COLLECT_CACHE = _wb_module().collect()
        _COLLECT_AT = time.monotonic()
        return _COLLECT_CACHE


class WorkBuddyAdapter(PlatformAdapter):
    platform = PLATFORM
    display_name = "WorkBuddy（腾讯）"
    declared_models = (
        "deepseek-v4.1-flash", "hy4flash-free", "glm-5.3-flash",
        "kimi-k3-1", "deepseek-v4-pro",
    )

    def discover_accounts(self) -> list[Account]:
        try:
            rows = _collect_cached()
        except Exception:
            return []
        out = []
        for r in rows:
            out.append(Account(
                platform=self.platform, uid=str(r.get("uidFull") or r.get("uid") or r.get("uin")),
                label=str(r.get("name") or r.get("uin")), uin=str(r.get("uin") or ""),
                source=str(r.get("source") or ""), raw={"ok": r.get("ok")},
            ))
        return out

    def fetch_credits(self, account: Account) -> Credits:
        try:
            rows = _collect_cached()
        except Exception as e:
            return Credits(ok=False, error=str(e)[:200])
        for r in rows:
            uid = str(r.get("uidFull") or r.get("uid") or r.get("uin"))
            if uid == account.uid or str(r.get("uin")) == account.uin:
                ok = bool(r.get("ok"))
                return Credits(
                    ok=ok, remain=float(r.get("remain") or 0), total=float(r.get("total") or 0),
                    used=float(r.get("used") or 0),
                    detail=f"{r.get('name')} · {r.get('source')}", error="" if ok else "余额查询失败",
                )
        return Credits(ok=False, error="账号不在池中")

    def checkin(self, account: Account) -> SigninResult:
        # 签到由现有计划任务负责；这里只读当天是否已领，不重复触发
        try:
            today = _wb_module().signin_gains().get("today", 0)
            return SigninResult(ok=True, status="ALREADY", gained=0,
                                message=f"WorkBuddy 签到由计划任务负责（今日已入账 {today:.0f}）")
        except Exception as e:
            return SigninResult(ok=False, status="ERROR", message=str(e)[:200])

    def fetch_limits(self, accounts: list[Account]) -> list[ModelLimit]:
        if not RL_FILE.exists():
            return []
        try:
            data = json.loads(RL_FILE.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return []
        names = {a.uin: a.label for a in accounts if a.uin}
        out: list[ModelLimit] = []
        for key, val in (data.get("models") or {}).items():
            uin, _, model = key.partition("|")
            until = int(val.get("until") or 0)
            limited = until > __import__("time").time() * 1000
            out.append(ModelLimit(
                platform=self.platform, uid=uin, model=model, limited=limited,
                until=until or None, local=str(val.get("local") or ""),
                source="live", raw={"name": names.get(uin, uin)},
            ))
        return out

    def chat(self, account: Account, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        # 直接走新框架的 WorkBuddy 原生协议层（self-contained，不依赖任何旧网关）
        import core.workbuddy_native as wn
        try:
            return wn.chat_sync(model, payload, preferred=account.uin or account.uid)
        except Exception as e:
            return {"error": {"message": str(e)[:300], "type": "upstream_error"}}

    def health(self) -> tuple[bool, str]:
        accts = self.discover_accounts()
        return (True, f"已发现 {len(accts)} 个账号（原生协议层）") if accts else (False, "未发现 WorkBuddy 账号池")
