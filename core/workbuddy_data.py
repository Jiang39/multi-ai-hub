#!/usr/bin/env python3
"""WorkBuddy 数据层（自包含）。

从旧 wb-credits.py 迁移的只读函数：账号池 / 官方余额 / 签到记录 / 用量统计 / 模型限流。
所有路径已改为指向本框架的 config/ 与 runtime/，不依赖任何外部目录。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POOL_DIR = ROOT / "runtime"
POOL_FILES_DIR = POOL_DIR / "pool"
LOG_FILE = POOL_DIR / "gateway.log"
RL_FILE = POOL_DIR / "ratelimit.json"
ACCOUNTS_FILE = POOL_DIR / "signin" / "accounts.json"
SIGNIN_LOG = POOL_DIR / "signin" / "signin.log"
ENDPOINT = "https://copilot.tencent.com/billing/meter/get-user-resource-summary"
TIMEOUT = 15
TZ_CN = timezone(timedelta(hours=8))

# 迁移后的路径变量（函数体里引用旧常量名的地方统一在这里兜底）
BASE_DIR = ROOT


def _plain(value) -> str:
    """池文件里的昵称可能是 $wbEncrypted 信封，只取明文。"""
    if isinstance(value, str):
        text = value.strip()
        return "" if "wbEncrypted" in text else text
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def load_accounts() -> list[dict]:
    """读取所有明文池文件；同一 uid 只保留一份。"""
    seen: dict[str, dict] = {}
    for path in sorted(POOL_FILES_DIR.glob("pool-*.info")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        auth = data.get("auth") or {}
        account = data.get("account") or {}
        token = auth.get("accessToken")
        uid = account.get("uid")
        if not isinstance(token, str) or not token or not uid:
            continue
        if uid in seen:
            continue
        seen[uid] = {
            "uid": str(uid),
            "uin": _plain(account.get("uin")),
            "nickname": _plain(account.get("nickname")),
            "token": token,
            "source": path.name,
        }
    return list(seen.values())


def fetch_credits(account: dict) -> dict:
    body = b"{}"
    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        method="POST",
        headers={
            "authorization": "Bearer " + account["token"],
            "x-user-id": account["uid"],
            "user-agent": "WorkBuddy",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:  # 网络错误、超时
        return {"ok": False, "error": type(exc).__name__}

    if payload.get("code") not in (0, None):
        return {"ok": False, "error": str(payload.get("msg"))[:60]}

    data = payload.get("data") or {}
    packages = []
    for pkg in data.get("Packages") or []:
        try:
            packages.append(
                {
                    "code": str(pkg.get("PackageCode") or "")[:28],
                    "total": float(pkg.get("CycleTotalCapacity") or 0),
                    "remain": float(pkg.get("CycleRemainCapacity") or 0),
                    "used": float(pkg.get("CycleUsedCapacity") or 0),
                }
            )
        except (TypeError, ValueError):
            continue

    total = sum(p["total"] for p in packages)
    remain = sum(p["remain"] for p in packages)
    return {
        "ok": True,
        "total": total,
        "remain": remain,
        "used": max(total - remain, 0.0),
        "packages": packages,
        "reported": data.get("TotalCredits"),
    }


def signin_names() -> dict[str, str]:
    """签到快照里的账号名（主号/新号/三号/四号），按完整 uid 索引。

    池文件 nickname 是 $wbEncrypted 信封解不开，只能靠这份本地快照认账号。
    """
    result: dict[str, str] = {}
    snap = ACCOUNTS_FILE
    if snap.exists():
        try:
            for a in (json.loads(snap.read_text(encoding="utf-8-sig")).get("accounts") or []):
                if a.get("uid") and a.get("name"):
                    result[str(a["uid"])] = str(a["name"])
        except (OSError, ValueError):
            pass
    return result


def collect() -> list[dict]:
    names = signin_names()
    rows = []
    for account in load_accounts():
        info = fetch_credits(account)
        label = names.get(account["uid"], "") or account["nickname"] or account["uin"] or account["uid"][:8]
        rows.append(
            {
                "name": label,
                "uin": account["uin"],
                "uid": account["uid"][:8],
                "uidFull": account["uid"],
                "source": account["source"],
                **info,
            }
        )
    return rows


def usage_stats(official_used: float | None = None) -> dict:
    """从网关日志统计每日积分消耗，按本地时区 UTC+8 归日。

    上游积分包只有「当前周期已用」，没有时间序列；网关日志每行请求结束都记了
    credit=，这是唯一真实按日可分的消耗数据。日志同时读轮转的 .1/.2。
    """
    import re
    from datetime import date, timedelta, timezone

    local_tz = timezone(timedelta(hours=8))
    pattern = re.compile(
        r"^\[(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})\.\d+Z\]"
        r".*?credit=([0-9.]+)"
    )
    by_day: dict[str, float] = {}
    for name in ("gateway.log", "gateway.log.1", "gateway.log.2"):
        path = POOL_DIR / name
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            m = pattern.match(line)
            if not m:
                continue
            d, hh, mm, ss, credit = m.groups()
            try:
                dt = datetime(
                    int(d[:4]), int(d[5:7]), int(d[8:10]),
                    int(hh), int(mm), int(ss), tzinfo=timezone.utc,
                ).astimezone(local_tz)
            except ValueError:
                continue
            day = dt.date().isoformat()
            by_day[day] = by_day.get(day, 0.0) + float(credit)

    today = datetime.now(local_tz).date()

    def window(days: int) -> float:
        s = 0.0
        for day, value in by_day.items():
            try:
                age = (today - date.fromisoformat(day)).days
            except ValueError:
                continue
            if 0 <= age < days:
                s += value
        return round(s, 2)

    last7 = window(7)
    last30 = window(30)
    official = round(official_used, 2) if official_used is not None else None
    # 近 7/30 天窗口用官方「当前周期累计已用」对齐余额；网关日志只覆盖反代
    # 流量且起始晚，按天累加会系统性偏低。今日仍用日志（粒度更细）。
    if official is not None and official > last30:
        last30 = official
    if official is not None and official > last7:
        last7 = official
    return {
        "today": round(by_day.get(today.isoformat(), 0.0), 2),
        "last7": last7,
        "last30": last30,
        "by_day": dict(sorted(by_day.items())),
        "since": min(by_day) if by_day else None,
        "official_used": official,
        "log_total": round(sum(by_day.values()), 2),
    }


def signin_gains() -> dict:
    """从签到日志统计积分入账，按本地日期归日（所有账号合计）。

    口径（踩过两次坑后的修正版）：
      - 签到分：每个 (日期, 账号) 只算一次。ALREADY 轮次里的「今日 +100」是
        历史信息、不是本轮入账，全部累加会重复；但客户端自己签的账号（脚本
        看到 ALREADY + credits_gained=0）也必须算进来，否则漏算。
      - 礼物 / 任务奖：每轮 report 里提取，按轮累加（同一天确实可能领多次）。
      - 单账号轮次（无 accounts 字段）的 credits_gained 直接计入。
    """
    import re
    from datetime import date, timedelta, timezone

    local_tz = timezone(timedelta(hours=8))
    path = SIGNIN_LOG
    # (day, account) -> {"checkin": 当日签到分(取一次), "extra": 礼物+任务(累加)}
    per: dict[tuple[str, str], dict[str, int]] = {}
    by_day: dict[str, float] = {}

    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            lines = []
        for line in lines:
            idx = line.find("] ")
            if idx < 0:
                continue
            try:
                stamp = datetime.strptime(line[1:idx], "%Y-%m-%d %H:%M:%S")
                payload = json.loads(line[idx + 2:])
            except (ValueError, json.JSONDecodeError):
                continue
            day = stamp.date().isoformat()
            accounts = payload.get("accounts") or []
            if not accounts:
                # 单账号旧格式：credits_gained 整轮直接计入
                by_day[day] = by_day.get(day, 0.0) + float(payload.get("credits_gained") or 0)
                continue
            for account in accounts:
                # 用 uid 做 key：名字可变（备注/重绑），uid 才是账号唯一标识
                name = str(account.get("uid") or account.get("account") or "?")
                report = account.get("report") or ""
                slot = per.setdefault((day, name), {"checkin": 0, "extra": 0})
                # 本轮签到分：CLAIMED 用「成功领取 N」，ALREADY 用「今日 +N」；当天只取一次
                claimed = re.findall(r"成功领取\s*(\d+)\s*积分", report)
                today = re.findall(r"今日\s*\+(\d+)", report)
                checkin = max([int(x) for x in (claimed + today)] or [0])
                slot["checkin"] = max(slot["checkin"], checkin)
                # 礼物 + 任务奖：按轮累加
                gifts = sum(int(x) for x in re.findall(r"领旅行礼物\s*\+(\d+)", report))
                tasks = sum(int(x) for x in re.findall(r"\+credit(\d+)", report))
                slot["extra"] += gifts + tasks

    for (day, _), vals in per.items():
        by_day[day] = by_day.get(day, 0.0) + vals["checkin"] + vals["extra"]

    today = datetime.now(local_tz).date()

    def window(days: int) -> float:
        s = 0.0
        for day, value in by_day.items():
            try:
                age = (today - date.fromisoformat(day)).days
            except ValueError:
                continue
            if 0 <= age < days:
                s += value
        return round(s, 2)

    return {
        "today": round(by_day.get(today.isoformat(), 0.0), 2),
        "last7": window(7),
        "last30": window(30),
        "by_day": dict(sorted(by_day.items())),
        "since": min(by_day) if by_day else None,
    }


def model_usage() -> dict:
    """按账号 + 模型统计网关日志里的用量与限流（429）。

    网关日志每行 `→ model=... account=...` 记请求，`← ... credit=N` 记成功消耗，
    失败行（如「失败 HTTP 429」）记限流。← 行不带 account，按「最近的 →」
    配对归账号。返回 deepseek 分账号统计 + 全局按模型汇总。
    """
    import re
    from datetime import datetime as _dt, timedelta

    stats: dict[str, dict] = {}
    model_totals: dict[str, dict] = {}
    cur_model = cur_acct = None

    def slot(key: str) -> dict:
        if key not in stats:
            stats[key] = {
                "req": 0, "ok": 0, "credit": 0.0, "rate429": 0,
                "last429": "", "lastUse": "", "models": set(),
            }
        return stats[key]

    def mslot(name: str) -> dict:
        if name not in model_totals:
            model_totals[name] = {"req": 0, "ok": 0, "credit": 0.0, "rate429": 0}
        return model_totals[name]

    req_re = re.compile(r"^\[(.*?)\] → (?:流式|非流式) model=(\S+) account=(\S+)")
    ok_re = re.compile(r"^\[(.*?)\] ← (\S+).*?credit=([0-9.]+)")
    for name in ("gateway.log", "gateway.log.1", "gateway.log.2"):
        path = POOL_DIR / name
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            m = req_re.match(line)
            if m:
                cur_model, cur_acct = m.group(2), m.group(3)
                if "deepseek" in cur_model:
                    s = slot(cur_acct)
                    s["req"] += 1
                    s["lastUse"] = m.group(1)[:19]
                    s["models"].add(cur_model)
                mm = mslot(cur_model)
                mm["req"] += 1
                continue
            if cur_model and "失败 HTTP 429" in line and cur_acct:
                if "deepseek" in cur_model:
                    s = slot(cur_acct)
                    s["rate429"] += 1
                    stamp = re.match(r"^\[(.*?)\]", line)
                    if stamp:
                        s["last429"] = stamp.group(1)[:19]
                mslot(cur_model)["rate429"] += 1
                continue
            m = ok_re.match(line)
            if m and cur_model and cur_acct:
                credit = float(m.group(3))
                mm = mslot(m.group(2))
                mm["ok"] += 1
                mm["credit"] += credit
                if "deepseek" in cur_model:
                    s = slot(cur_acct)
                    s["ok"] += 1
                    s["credit"] += credit

    # uin -> 昵称：池文件给 uid↔uin，签到快照给 uid→中文名（主号/新号/三号）
    # 池文件里的 nickname 是加密信封解不开，所以用签到快照的名字
    names: dict[str, str] = {}
    uid_to_name: dict[str, str] = {}
    snap = ACCOUNTS_FILE
    if snap.exists():
        try:
            sd = json.loads(snap.read_text(encoding="utf-8-sig"))
            for a in sd.get("accounts") or []:
                if a.get("uid") and a.get("name"):
                    uid_to_name[str(a["uid"])] = str(a["name"])
        except (OSError, ValueError):
            pass
    for path in POOL_FILES_DIR.glob("pool-*.info"):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        acct = data.get("account") or {}
        uid, uin = str(acct.get("uid") or ""), str(acct.get("uin") or "")
        if uin:
            names[uin] = uid_to_name.get(uid, "") or uin

    # 限流状态优先读网关实时写的 data/ratelimit.json（含上游报文里的真实重置时间）：
    #   accounts：账号级冷却（兼容旧网关）
    #   models：账号×模型级冷却，键 `uin|模型`，每个组合独立重置时间
    live: dict[str, dict] = {}
    live_models: dict[str, dict] = {}
    rl_file = POOL_DIR / "ratelimit.json"
    if rl_file.exists():
        try:
            rd = json.loads(rl_file.read_text(encoding="utf-8-sig"))
            live = rd.get("accounts") or {}
            live_models = rd.get("models") or {}
        except (OSError, ValueError):
            pass

    # 网关的 429 冷却时长(毫秒)从它的 config.json 读，兜底估算用
    cooldown_s = 60.0
    gw_cfg = ROOT / "config" / "workbuddy-gateway.json"
    if gw_cfg.exists():
        try:
            cooldown_s = int(json.loads(gw_cfg.read_text(encoding="utf-8-sig")).get("rateLimitCooldownMs") or 60_000) / 1000
        except (OSError, ValueError):
            pass

    now_local = _dt.now()
    deepseek = []
    for acct, s in sorted(stats.items(), key=lambda x: -x[1]["req"]):
        # last429 记的是 UTC，转成 UTC+8 展示，和面板其它时间一致
        last429 = s["last429"]
        recovery = ""
        cooling = False
        if last429:
            try:
                last429 = (_dt.strptime(last429, "%Y-%m-%dT%H:%M:%S") + timedelta(hours=8)).strftime("%m-%d %H:%M:%S")
            except ValueError:
                pass
        lv = live.get(acct)
        if lv and int(lv.get("until") or 0) / 1000 > _dt.now().timestamp():
            cooling = True
            recovery = str(lv.get("local") or "")
            recovery = recovery[5:] if len(recovery) >= 16 else recovery  # 去掉年份，留 MM-DD HH:MM
        elif last429:
            try:
                dt = _dt.strptime(last429, "%m-%d %H:%M:%S")
                est = dt.replace(year=now_local.year) + timedelta(seconds=cooldown_s)
                if est > now_local:
                    cooling = True
                    recovery = est.strftime("%H:%M:%S")
            except ValueError:
                pass
        deepseek.append({
            "account": acct,
            "name": names.get(acct, "") or acct,
            "req": s["req"],
            "ok": s["ok"],
            "credit": round(s["credit"], 2),
            "rate429": s["rate429"],
            "last429": last429,
            "cooling": cooling,
            "recovery": recovery,
            "lastUse": s["lastUse"][:19],
            "models": sorted(s["models"]),
        })

    totals = {
        name: {**v, "credit": round(v["credit"], 2)}
        for name, v in sorted(model_totals.items(), key=lambda x: -x[1]["req"])
    }

    # 按「真实下游模型」汇总限流状态，供面板展示指定模型的限流。
    # 网关日志里可能记别名（如 claude-haiku-2），按 config 的 models.map 归一。
    model_map: dict[str, str] = {}
    gw_cfg = ROOT / "config" / "workbuddy-gateway.json"
    if gw_cfg.exists():
        try:
            model_map = {
                str(k): str(v)
                for k, v in (json.loads(gw_cfg.read_text(encoding="utf-8-sig"))
                            .get("models", {}).get("map", {})).items()
            }
        except (OSError, ValueError):
            pass

    def real_model(name: str) -> str:
        return model_map.get(name, name)

    # 每个模型：累计 429 / 最近 429（UTC+8）/ 最近成功；限流中与否看实时账号冷却。
    # 账号限流是全账号池共用，该模型正在使用的账号若冷却中则视为受限。
    focus: dict[str, dict] = {}

    def fslot(name: str) -> dict:
        if name not in focus:
            focus[name] = {"rate429": 0, "last429": "", "lastUse": "", "name": name}
        return focus[name]

    req2_re = re.compile(r"^\[(.*?)\] → (?:流式|非流式) model=(\S+) account=(\S+)")
    ok2_re = re.compile(r"^\[(.*?)\] ← (\S+).*?credit=([0-9.]+)")
    fail429_re = re.compile(r"^\[(.*?)\]\s+(\S+)\s+失败 HTTP 429")
    for name in ("gateway.log", "gateway.log.1", "gateway.log.2"):
        path = POOL_DIR / name
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        cur = None
        for line in lines:
            m = req2_re.match(line)
            if m:
                cur = real_model(m.group(2))
                fs = fslot(cur)
                fs["lastUse"] = m.group(1)[:19]
                continue
            m = fail429_re.match(line)
            if m:
                model = real_model(m.group(2))
                fs = fslot(model)
                fs["rate429"] += 1
                fs["last429"] = m.group(1)[:19]
                continue
            m = ok2_re.match(line)
            if m:
                model = real_model(m.group(2))
                fslot(cur or model)["lastUse"] = m.group(1)[:19]

    def fmt_429(stamp: str) -> str:
        if not stamp:
            return ""
        try:
            return (_dt.strptime(stamp, "%Y-%m-%dT%H:%M:%S") + timedelta(hours=8)).strftime("%m-%d %H:%M:%S")
        except ValueError:
            return stamp[:19]

    # 当前正在冷却的账号（uin 或 uid）集合 + 最早恢复时间，作为限流中判据
    cooling_accounts: dict[str, int] = {}
    for key, lv in live.items():
        until = int(lv.get("until") or 0)
        if until / 1000 > _dt.now().timestamp():
            cooling_accounts[str(key)] = until

    def account_used_for(model: str) -> set[str]:
        # 取该模型最近 20 次请求用到的账号：代表「当前」由谁承载，
        # 避免把几天前用过、现在已换走的账号也算成正在受限。
        recent: list[str] = []
        p = re.compile(r"^\[(.*?)\] → (?:流式|非流式) model=(\S+) account=(\S+)")
        for name in ("gateway.log", "gateway.log.1", "gateway.log.2"):
            path = POOL_DIR / name
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            for line in lines:
                m = p.match(line)
                if m and real_model(m.group(2)) == model:
                    recent.append(m.group(3))
        return set(recent[-20:])

    # 账号×模型限流矩阵：上游 429 是「账号+模型」级，同一账号不同模型状态不同。
    # 权威来源是网关写的 ratelimit.json models 字段（含每个组合的真实重置时间）。
    watching = ("hy4flash-free", "deepseek-v4.1-flash")
    # uin -> 昵称：池文件给 uid↔uin，签到快照给 uid→名字（主号/新号/三号/四号）
    signin_names_map = signin_names()
    uin_names: dict[str, str] = {}
    # 所有账号（来自 ratelimit.json 的 models/accounts 键 + 池文件），按 uin 排序
    accounts: set[str] = set()
    for key in live_models:
        accounts.add(key.split("|", 1)[0])
    for key in cooling_accounts:
        accounts.add(key)
    for path in POOL_FILES_DIR.glob("pool-*.info"):
        try:
            acct = (json.loads(path.read_text(encoding="utf-8-sig")).get("account") or {})
            if acct.get("uin"):
                uin = str(acct["uin"])
                accounts.add(uin)
                uid = str(acct.get("uid") or "")
                if uid and uid in signin_names_map:
                    uin_names[uin] = signin_names_map[uid]
        except Exception:
            pass

    now_ts = _dt.now().timestamp()
    model_status = []
    for model in watching:
        fs = fslot(model)
        cells = []
        for acct in sorted(accounts):
            key = f"{acct}|{model}"
            lv = live_models.get(key)
            until = int(lv.get("until") or 0) if lv else 0
            label = uin_names.get(acct, "")
            if until / 1000 > now_ts:
                # ratelimit.json 的 local 字段已是 UTC+8 展示串，直接用，避免二次 +8h
                cells.append({
                    "account": acct,
                    "name": label,
                    "state": "limited",
                    "recovery": str(lv.get("local") or "") if lv else "",
                })
            else:
                cells.append({"account": acct, "name": label, "state": "ok", "recovery": ""})
        limited = [c["account"] for c in cells if c["state"] == "limited"]
        overall = "limited" if limited else ("warn" if fs["rate429"] else "ok")
        model_status.append({
            "model": model,
            "state": overall,
            "cells": cells,
            "limitedAccounts": limited,
            "rate429": fs["rate429"],
            "last429": fmt_429(fs["last429"]),
            "lastUse": fmt_429(fs["lastUse"]),
        })

    return {
        "deepseek": deepseek,
        "models": totals,
        "modelStatus": model_status,
        "note": "429 为账号×模型级；限流中 = 网关实时上报的组合冷却(上游重置时间)；累计 429 读网关日志",
    }
