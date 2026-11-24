"""统一账号纳管：扫描各平台当前/历史账号，自动加入签到与网关池。

设计要点：
- 桌面客户端同一时刻只保存一个登录态，所以「当前登录」不能代表全部账号。
  以 当前登录态 + 历史签到快照 + 网关池文件 的并集为准，按 uid 去重。
- WorkBuddy：未 bind 的 uid 用 pool 快照 bind 进签到脚本；同时确保网关池
  文件和 config.extraFiles 覆盖所有 uid。
- 其它 5 家：登录态即当前账号，网关/面板天然自动识别；这里只做清单报告，
  并把「可签到」的账号纳入统一签到调度（由 hub.py checkin 执行）。
- 全程不打印令牌；只输出 uid 前缀、别名与动作。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from .adapter import DATA_DIR, read_json, write_json

ROOT = Path(__file__).resolve().parent.parent
POOL_DIR = ROOT / "runtime" / "pool"
CONFIG_FILE = ROOT / "config" / "workbuddy-gateway.json"
SIGNIN_DIR = ROOT / "signin"
SIGNIN_SCRIPT = SIGNIN_DIR / "signin.py"
REGISTRY_FILE = SIGNIN_DIR / "accounts" / "accounts.json"


def _signin_module() -> Any:
    spec = importlib.util.spec_from_file_location("hub_signin", SIGNIN_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_sessions() -> list[tuple[str, dict[str, Any]]]:
    """返回 [(来源标签, session)]，session 含 auth/account，不含对外输出。"""
    found: list[tuple[str, dict[str, Any]]] = []
    sg = _signin_module()
    # 1. 历史签到快照（已 bind 的账号）
    try:
        reg = sg.load_registry()
        for entry in reg.get("accounts", []):
            path = sg.credential_path(entry)
            if Path(path).exists():
                found.append((f"快照:{entry['name']}", sg.load_session(path)))
    except Exception:
        pass
    # 2. 网关池文件（含只存在于池里的号，如第 5/6 号）
    for p in sorted(POOL_DIR.glob("pool-*.info")):
        try:
            found.append((f"池:{p.stem[5:13]}", json.loads(p.read_text(encoding="utf-8-sig"))))
        except (OSError, ValueError):
            continue
    # 3. 客户端当前登录
    try:
        auth_file, _ = sg.find_auth_file()
        if auth_file and Path(auth_file).exists():
            found.append(("客户端当前登录", sg.load_session(auth_file)))
    except Exception:
        pass
    return found


def _plain_token(sg: Any, value: Any) -> str | None:
    if isinstance(value, str) and "wbEncrypted" not in value:
        return value
    if value is None:
        return None
    try:
        helper = sg._run_auth_helper(sg.find_workbuddy_runtime(),
                                     {"operation": "decrypt", "value": value})
        return helper.get("accessToken")
    except Exception:
        return None


def _pool_payload(sg: Any, session: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    auth = session.get("auth") or {}
    acct = session.get("account") or {}
    uid = acct.get("uid") or auth.get("uid")
    if not uid:
        return None
    payload = {
        "account": {"uid": uid, "nickname": acct.get("nickname"), "uin": acct.get("uin")},
        "auth": {
            "accessToken": _plain_token(sg, auth.get("accessToken")),
            "refreshToken": _plain_token(sg, auth.get("refreshToken")),
            "expiresAt": auth.get("expiresAt"),
            "refreshExpiresAt": auth.get("refreshExpiresAt"),
            "domain": auth.get("domain"),
        },
    }
    if not payload["auth"]["accessToken"]:
        return None
    return str(uid), payload


def sync_workbuddy() -> dict[str, Any]:
    """把 WorkBuddy 所有已知账号纳管进签到快照与网关池。返回变更报告。"""
    sg = _signin_module()
    sessions = _load_sessions()
    # 按 uid 去重，取 refresh 有效期更长的那份
    by_uid: dict[str, dict[str, Any]] = {}
    for label, session in sessions:
        got = _pool_payload(sg, session)
        if not got:
            continue
        uid, pool = got
        old = by_uid.get(uid)
        old_exp = (old["auth"].get("refreshExpiresAt") or 0) if old else 0
        new_exp = pool["auth"].get("refreshExpiresAt") or 0
        if old and old_exp >= new_exp:
            continue
        by_uid[uid] = pool

    report: dict[str, Any] = {"workbuddy": {"found": len(by_uid), "pool_written": [],
                                            "bind_added": [], "bind_skipped": []}}

    # 1. 写网关池文件（含 uid 前缀标记）
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for uid, pool in by_uid.items():
        short = uid[:8]
        path = POOL_DIR / f"pool-{short}.info"
        path.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(f"runtime/pool/{path.name}")
    report["workbuddy"]["pool_written"] = [w.split("/")[-1] for w in written]

    # 2. 更新 config.extraFiles（相对路径，指向新框架内）
    cfg = read_json(CONFIG_FILE, {})
    cfg.setdefault("accounts", {})["extraFiles"] = sorted(written)
    write_json(CONFIG_FILE, cfg)

    # 3. bind 未纳管的 uid 进签到脚本
    try:
        reg = sg.load_registry()
        bound_uids = {str(a.get("uid")) for a in reg.get("accounts", [])}
    except Exception:
        bound_uids = set()
    for uid, pool in by_uid.items():
        if uid in bound_uids:
            report["workbuddy"]["bind_skipped"].append(uid[:8])
            continue
        short = uid[:8]
        # 账号名用 uid 前缀，稳定且可读
        name = f"wb-{short}"
        source = POOL_DIR / f"pool-{short}.info"
        try:
            sg.bind_account(name, source=source, force=False)
            report["workbuddy"]["bind_added"].append({"name": name, "uid": short})
        except Exception as e:
            report["workbuddy"]["bind_added"].append(
                {"name": name, "uid": short, "error": str(e)[:120]})
    return report


def sync_all(svc: Any) -> dict[str, Any]:
    """全平台纳管：WorkBuddy 做池+签到同步，其余平台报告当前账号。"""
    report = sync_workbuddy()
    platforms: dict[str, list[str]] = {}
    for platform, adapter in svc.adapters.items():
        if platform == "workbuddy":
            continue
        try:
            accts = adapter.discover_accounts()
            platforms[platform] = [f"{a.uid[:12]}({a.label})" for a in accts]
        except Exception as e:
            platforms[platform] = [f"发现失败:{str(e)[:60]}"]
    report["platforms"] = platforms
    report["note"] = ("其它平台登录态即当前账号，网关/面板自动识别；"
                      "签到由 hub.py checkin 统一调度。")
    return report
