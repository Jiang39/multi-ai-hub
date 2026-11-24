"""统一服务层：聚合所有平台的账号、额度、签到、限流、用量。

网关、签到调度、面板只依赖这一层，不直接碰适配器。
所有结果都写入 data/ 下的 JSON，供面板读取；不缓存令牌。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import workbuddy_data as wd
from .adapter import DATA_DIR, PlatformAdapter, read_json, write_json
from .governance import GOVERNANCE, parse_retry_ms
from .models import Account, Credits, SigninResult, account_to_dict, credits_to_dict

TZ_CN = timezone(timedelta(hours=8))
SNAPSHOT_FILE = DATA_DIR / "credits-history.json"
SIGNIN_LOG = DATA_DIR / "signin.jsonl"
LIMITS_FILE = DATA_DIR / "limits.json"
STATUS_FILE = DATA_DIR / "status.json"
ALIASES_FILE = DATA_DIR / "aliases.json"


def wb_module() -> Any:
    """WorkBuddy 数据层（自包含），提供 usage_stats/signin_gains/model_usage。"""
    return wd


def safe_error(exc: BaseException, limit: int = 200) -> str:
    """面板错误字段脱敏（统一实现见 core.redact）。"""
    from .redact import safe_error as _safe
    return _safe(exc, limit)


def now_ms() -> int:
    return int(time.time() * 1000)


def local_clock(ms: int | None) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, TZ_CN).strftime("%m-%d %H:%M:%S")


class HubService:
    def __init__(self, adapters: dict[str, PlatformAdapter]):
        self.adapters = adapters

    # --- 账号 & 额度 ------------------------------------------------------
    def accounts(self) -> list[Account]:
        out: list[Account] = []
        for adapter in self.adapters.values():
            try:
                out.extend(adapter.discover_accounts())
            except Exception as exc:  # 单个平台失败不影响其它平台
                out.append(Account(
                    platform=adapter.platform, uid="__error__",
                    label=f"{adapter.display_name} 发现账号失败",
                    raw={"error": safe_error(exc)},
                ))
        return out

    def snapshot_credits(self) -> dict[str, Any]:
        """聚合额度 + 签到今日入账 + 写每日快照。面板 /api/overview 用。"""
        rows: list[dict[str, Any]] = []
        for acct in self.accounts():
            adapter = self.adapters.get(acct.platform)
            if acct.uid == "__error__":
                rows.append({**account_to_dict(acct), "ok": False,
                             "error": acct.raw.get("error", "unknown")})
                continue
            try:
                c: Credits = adapter.fetch_credits(acct) if adapter else Credits(ok=False, error="无适配器")
            except Exception as exc:
                c = Credits(ok=False, error=safe_error(exc))
            # remain/total 为 None 时如实传 None（面板显示「—」），不填 0 冒充无余额
            rows.append({**account_to_dict(acct), **credits_to_dict(c)})

        # 每日快照：key 用 platform:uid，不依赖展示名
        day = datetime.now(TZ_CN).strftime("%Y-%m-%d")
        history = read_json(SNAPSHOT_FILE, {})
        slot = history.setdefault(day, {})
        for row in rows:
            if row.get("ok"):
                slot[row["key"]] = row["remain"]
        write_json(SNAPSHOT_FILE, history)

        return {
            "at": datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S"),
            "accounts": rows,
            "gains": self.today_gains(),
            "usage": self.usage(),
        }

    def today_gains(self) -> dict[str, Any]:
        """今日签到收益 = WorkBuddy 签到日志 + 新框架其它平台签到。

        signin_gains() 日志缺失时正常返回 0（不抛异常），所以不能靠 except 兜底：
        主动合并 data/signin.jsonl，避免非 WorkBuddy 平台的收益被吞掉。
        """
        try:
            g = wb_module().signin_gains()
            by_day = dict(g.get("by_day") or {})
        except Exception:
            by_day = {}
        # 新框架 hub.py checkin 的收益（Qoder/Trae/小浣熊等）
        for line in self._read_signin_log():
            d = str(line.get("at", ""))[:10]
            if not d or line.get("status") not in ("CLAIMED", "ALREADY", "OK"):
                continue
            try:
                gained = float(line.get("gained") or 0)
            except (TypeError, ValueError):
                continue
            if gained > 0:
                by_day[d] = by_day.get(d, 0.0) + gained
        today = datetime.now(TZ_CN).strftime("%Y-%m-%d")
        return {"today": round(by_day.get(today, 0.0), 2),
                "byDay": {k: round(v, 2) for k, v in by_day.items()}}

    def today_usage(self) -> dict[str, Any]:
        # 协议待补阶段没有网关日志，返回 0 而不是编造
        return {"today": 0.0, "d7": 0.0, "d30": 0.0, "note": "用量统计待网关接入后启用"}

    def usage(self) -> dict[str, Any]:
        """近 7/30 天用量：复用 WorkBuddy 网关日志的真实按日统计。"""
        try:
            wb = wb_module()
            u = wb.usage_stats()
            g = self.today_gains()  # 与面板今日签到同源（含非 WorkBuddy 平台）
            by_day = g.get("byDay") or {}
            g7 = sum(v for d, v in by_day.items() if self._within(d, 7))
            g30 = sum(v for d, v in by_day.items() if self._within(d, 30))
            return {
                "today": u.get("today", 0.0), "last7": u.get("last7", 0.0),
                "last30": u.get("last30", 0.0), "by_day": u.get("by_day", {}),
                "gains_today": g.get("today", 0.0), "gains_last7": round(g7, 2),
                "gains_last30": round(g30, 2),
                "note": "消耗来自网关日志（仅反代流量，可能低于官方周期已用）；签到收益来自签到记录",
                "ok": True,
            }
        except Exception as e:
            return {"today": 0.0, "last7": 0.0, "last30": 0.0,
                    "note": f"用量统计读取失败：{str(e)[:100]}", "ok": False}

    @staticmethod
    def _within(day: str, days: int) -> bool:
        try:
            d = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            return False
        from datetime import date
        return 0 <= (datetime.now(TZ_CN).date() - d).days < days

    def model_status(self) -> dict[str, Any]:
        """账号×模型限流矩阵：WorkBuddy 用原面板口径（含 429 累计/恢复时间）。"""
        try:
            return wb_module().model_usage()
        except Exception as e:
            return {"modelStatus": [], "note": f"限流状态读取失败：{str(e)[:100]}"}

    # --- 签到 -------------------------------------------------------------
    def run_checkin(self, platform: str | None = None) -> list[dict[str, Any]]:
        """跑签到，结果追加 JSONL 日志。重复签到由适配器保证幂等。"""
        results: list[dict[str, Any]] = []
        for p, adapter in self.adapters.items():
            if platform and p != platform:
                continue
            for acct in adapter.discover_accounts():
                try:
                    r: SigninResult = adapter.checkin(acct)
                except Exception as exc:
                    r = SigninResult(ok=False, status="ERROR", message=safe_error(exc))
                item = {
                    "at": datetime.now(TZ_CN).isoformat(timespec="seconds"),
                    "platform": p, "uid": acct.uid, "label": acct.label,
                    "ok": r.ok, "status": r.status,
                    "gained": r.gained, "message": r.message,
                }
                results.append(item)
                self._append_signin_log(item)
        return results

    # --- 限流矩阵 ---------------------------------------------------------
    def refresh_limits(self) -> dict[str, Any]:
        all_limits: list[dict[str, Any]] = []
        for adapter in self.adapters.values():
            accts = adapter.discover_accounts()
            try:
                limits = adapter.fetch_limits(accts)
            except Exception:
                limits = []
            for lim in limits:
                all_limits.append({
                    "platform": lim.platform, "uid": lim.uid, "model": lim.model,
                    "name": str(lim.raw.get("name") or lim.uid),
                    "limited": lim.limited,
                    "until": lim.until, "local": lim.local or local_clock(lim.until),
                    "source": lim.source,
                })
        data = {
            "updatedAt": now_ms(),
            "updatedLocal": datetime.now(TZ_CN).strftime("%m-%d %H:%M:%S"),
            "limits": all_limits,
            "note": "WorkBuddy 限流来自统一网关 ratelimit.json，其余平台待网关接入",
        }
        write_json(LIMITS_FILE, data)
        return data

    def status(self) -> dict[str, Any]:
        """平台健康状态：面板用它显示「已接入/协议待补」。"""
        items = []
        for adapter in self.adapters.values():
            ok, msg = adapter.health()
            items.append({
                "platform": adapter.platform, "displayName": adapter.display_name,
                "ok": ok, "message": msg,
                "declaredModels": list(adapter.declared_models),
                "stubReason": getattr(adapter, "stub_reason", ""),
            })
        data = {"at": datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S"), "platforms": items}
        write_json(STATUS_FILE, data)
        return data

    # --- 网关调用 ---------------------------------------------------------
    @staticmethod
    def _is_rate_limited(result: dict[str, Any]) -> tuple[bool, str]:
        """判断适配器返回的错误是否属于限流，并提取上游给的重试时间文本。"""
        err = result.get("error") if isinstance(result.get("error"), dict) else {}
        text = f"{err.get('type', '')} {err.get('message', '')}".lower()
        status = int(err.get("status") or 0)
        limited = status == 429 or any(k in text for k in (
            "429", "rate limit", "ratelimit", "too many requests", "限流", "频率",
            "额度不足", "code 112", "pricingurl", "1005",
        ))
        return limited, str(err.get("message") or "")

    def chat(self, platform: str, model: str, payload: dict[str, Any],
             preferred_uid: str | None = None, *, session_key: str | None = None,
             max_tries: int = 3) -> dict[str, Any]:
        """统一网关入口：sticky 选号 + 冷却 preflight + 429 换号/模型回退。

        新 5 家每家只有一个本机登录态账号，所以重试主要在「账号级/模型级冷却」
        之间切换等待；发现多账号时才会真正换号。WorkBuddy 走原生层，不经过这里。
        """
        adapter = self.adapters.get(platform)
        if not adapter:
            return {"error": {"message": f"未知平台 {platform}", "type": "invalid_request"}}
        accts = [a for a in adapter.discover_accounts() if a.enabled]
        if preferred_uid:
            accts = [a for a in accts if a.uid == preferred_uid] or accts
        if not accts:
            return adapter.chat(Account(platform=platform, uid=""), model, payload)

        tried: list[str] = []
        last: dict[str, Any] = {}
        for _ in range(max_tries):
            acct = GOVERNANCE.pick(platform, accts, model, session_key=session_key,
                                   preferred=preferred_uid)
            if acct is None:
                return {"error": {"message": "所有账号已被临时停用，请稍后重试",
                                  "type": "rate_limit_error"}}
            tried.append(acct.uid)
            # preflight：账号或该账号×模型在冷却中，直接换下一个/返回等待时间
            wait = GOVERNANCE.wait_reason(platform, acct.uid, model)
            preflight_blocked = bool(wait)
            if wait:
                candidates = [a for a in accts if a.uid not in tried and
                              not GOVERNANCE.wait_reason(platform, a.uid, model)]
                if candidates:
                    continue
                result: dict[str, Any] = {"error": {
                    "message": f"上游限流中，{wait}", "type": "rate_limit_error"}}
            else:
                result = adapter.chat(acct, model, payload)
            limited, err_msg = self._is_rate_limited(result)
            if "error" not in result:
                GOVERNANCE.note_success(platform, acct.uid)
                return result
            # preflight 拦截是已知冷却，不算新的上游失败，避免误触发自动停用
            if not preflight_blocked:
                GOVERNANCE.note_failure(platform, acct.uid, err_msg)
            if not limited:
                return result
            retry = parse_retry_ms(err_msg)
            GOVERNANCE.mark_account(platform, acct.uid, retry)
            GOVERNANCE.mark_model(platform, acct.uid, model, retry)
            last = result
            if len(tried) >= len(accts):
                break
        return last or {"error": {"message": "所有账号限流", "type": "rate_limit_error"}}

    # --- 账号备注（服务端持久化，不绑浏览器/端口） -------------------------
    def get_aliases(self) -> dict[str, str]:
        data = read_json(ALIASES_FILE, {})
        return {str(k): str(v) for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}

    def set_alias(self, key: str, value: str) -> dict[str, str]:
        data = self.get_aliases()
        if value.strip():
            data[key] = value.strip()
        else:
            data.pop(key, None)
        write_json(ALIASES_FILE, data)
        return data

    # --- 内部 -------------------------------------------------------------
    @staticmethod
    def _read_signin_log() -> list[dict[str, Any]]:
        if not SIGNIN_LOG.exists():
            return []
        out = []
        for line in SIGNIN_LOG.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    @staticmethod
    def _append_signin_log(item: dict[str, Any]) -> None:
        SIGNIN_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SIGNIN_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
