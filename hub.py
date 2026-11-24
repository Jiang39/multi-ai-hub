#!/usr/bin/env python3
"""多平台统一入口：签到 / 网关 / 面板。

用法：
    python hub.py status              # 查看六家平台接入状态
    python hub.py checkin             # 跑所有平台签到，结果写 data/signin.jsonl
    python hub.py checkin --platform qwenwork
    python hub.py serve --port 8798   # 本地面板（与 WorkBuddy 8799 区分）
    python hub.py gateway --port 8899 # OpenAI 兼容反代网关 /v1/chat/completions
    python hub.py selftest            # 框架自检（联网冒烟，确认登录态与协议就绪）

设计：只依赖标准库；平台协议在 adapters/ 下逐个补；未实现的平台返回
UNSUPPORTED / 协议待补，不伪造额度或签到成功。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def netstat_listening_pid(port: int) -> str:
    """从 netstat 找本机端口的监听 PID。中文 Windows 输出是 GBK，必须显式指定编码。"""
    import subprocess
    try:
        txt = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True,
            text=True, encoding="gbk", errors="replace", timeout=5,
        ).stdout
    except Exception:
        return ""
    for line in txt.splitlines():
        if f"127.0.0.1:{port}" in line and "LISTENING" in line.upper():
            return line.split()[-1]
    return ""

from core.adapter import load_registry, read_json  # noqa: E402
from core.service import HubService, LIMITS_FILE, STATUS_FILE  # noqa: E402

# 模型别名映射，Claude Desktop 等客户端无需改 model 配置
WB_GATEWAY_CONFIG = ROOT / "config" / "workbuddy-gateway.json"


def workbuddy_model_map() -> dict[str, str]:
    """读 WorkBuddy config.json 的 models.map：claude-sonnet -> deepseek-v4.1-flash 等。"""
    cfg = read_json(WB_GATEWAY_CONFIG, {})
    return {str(k): str(v) for k, v in ((cfg.get("models") or {}).get("map") or {}).items()}


# 统一模型别名：cc-switch / Claude Desktop 里只显示这些名字，后台自动代理到
# 任一已登录平台的对应模型（按 alias_routes 顺序尝试，失败/限流自动换平台）。
# 若客户端显式写 "平台/模型"，仍可按前缀硬绑到指定平台。
ALIAS_ROUTES: dict[str, tuple[tuple[str, str], ...]] = {
    "deepseek-v4.1-flash": (
        ("workbuddy", "deepseek-v4.1-flash"),
        ("traework", "deepseek-v4.1-flash"),
        ("xiaohuanxiong", "sn-deepseek-v4-1-flash-1017"),
        ("qwenwork", "deepseek-flash"),
        ("loomy", "DeepSeek-V4.1-Flash"),
        ("qoder-intl", "DeepSeek-Flash"),
    ),
    "glm-5.3": (
        ("workbuddy", "glm-5.3-flash"),
        ("traework", "glm-5.3"),
        ("xiaohuanxiong", "sn-glm-5-3"),
        ("qwenwork", "glm-5.3"),
        ("loomy", "GLM-5.3-Flash"),
        ("qoder-intl", "GLM-5.3"),
    ),
    "kimi-k3.1-pro": (
        ("workbuddy", "kimi-k3-1"),
        ("xiaohuanxiong", "sn-kimi-k3-1-pro"),
        ("qwenwork", "kimi-k3.1-pro"),
        ("qoder-intl", "Kimi-K3.1-Pro"),
        ("traework", "kimi-k3"),
    ),
}

# Claude 系别名 → 统一别名（保留现有客户端配置，不改 base_url 就想切自动路由时）
CLAUDE_TO_ALIAS = {
    "claude-sonnet-1": "deepseek-v4.1-flash",
    "claude-haiku-2": "deepseek-v4.1-flash",
    "claude-fable-1": "glm-5.3",
    "claude-mythos-1": "kimi-k3.1-pro",
}


def normalize_alias(model: str) -> str:
    """Claude 系假名 → 统一别名；已是别名或平台/模型则原样返回。"""
    return CLAUDE_TO_ALIAS.get(model, model)


def alias_routes(model: str) -> list[tuple[str, str]]:
    """返回该模型可自动代理的 (平台, 上游模型) 列表。"""
    alias = normalize_alias(model)
    return list(ALIAS_ROUTES.get(alias, ()))


# Anthropic 原生：8899 的 /v1/messages 由 core/workbuddy_native.py 直接调用
# WorkBuddy 上游（6 号池 / sticky 缓存 / 账号×模型冷却 / token 刷新），不依赖 8789。
ANTHROPIC_PATHS = ("/v1/messages", "/v1/messages/count_tokens")

def _anthropic_to_openai_messages(req: dict[str, Any]) -> list[dict[str, Any]]:
    """Anthropic messages/system → OpenAI messages（新平台适配器用）。"""
    msgs: list[dict[str, Any]] = []
    sys_text = req.get("system")
    if isinstance(sys_text, list):
        sys_text = "\n".join(b.get("text", "") for b in sys_text
                             if isinstance(b, dict) and b.get("type") == "text")
    if isinstance(sys_text, str) and sys_text.strip():
        msgs.append({"role": "system", "content": sys_text})
    for m in req.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if isinstance(content, list):
            # tool_result / text / image 混排：尽量保留成 OpenAI 结构
            texts: list[str] = []
            parts: list[dict[str, Any]] = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    texts.append(str(b.get("text") or ""))
                elif b.get("type") == "tool_result":
                    msgs.append({"role": "tool", "tool_call_id": b.get("tool_use_id", ""),
                                 "content": str(b.get("content") or "")})
                elif b.get("type") == "image":
                    src = b.get("source") or {}
                    if src.get("type") == "base64":
                        parts.append({"type": "image_url", "image_url": {
                            "url": f"data:{src.get('media_type','image/png')};base64,{src.get('data','')}"}})
            if parts:
                msgs.append({"role": role, "content": parts})
            elif texts:
                msgs.append({"role": role, "content": "\n".join(texts)})
        else:
            msgs.append({"role": role, "content": content if isinstance(content, str) else json.dumps(content or {})})
    return msgs


def _openai_to_anthropic(result: dict[str, Any], model: str) -> dict[str, Any]:
    """OpenAI chat.completion dict → Anthropic message dict。"""
    if "error" in result:
        err = result["error"] or {}
        return {"type": "error", "error": {"type": "api_error",
                "message": str(err.get("message") or err)[:300]}}
    choices = result.get("choices") or []
    msg = (choices[0].get("message") or {}) if choices and isinstance(choices[0], dict) else {}
    content = msg.get("content") or ""
    blocks: list[dict[str, Any]] = []
    if content:
        blocks.append({"type": "text", "text": str(content)})
    for tc in msg.get("tool_calls") or []:
        fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        blocks.append({"type": "tool_use", "id": tc.get("id") or "",
                       "name": fn.get("name") or "", "input": args})
    u = result.get("usage") or {}
    stop = (choices[0].get("finish_reason") if choices else None) or "end_turn"
    usage_out: dict[str, Any] = {
        "input_tokens": u.get("prompt_tokens") or 0,
        "output_tokens": u.get("completion_tokens") or 0,
    }
    details = u.get("prompt_tokens_details") or {}
    if isinstance(details, dict) and details.get("cached_tokens"):
        usage_out["cache_read_input_tokens"] = details["cached_tokens"]
    return {
        "id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant",
        "model": model, "content": blocks,
        "stop_reason": {"length": "max_tokens", "tool_calls": "tool_use"}.get(stop, "end_turn"),
        "stop_sequence": None,
        "usage": usage_out,
    }


def _dispatch_platform_model(svc: HubService, model: str, req: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """若 model 是 平台/模型 前缀且平台非 workbuddy，调适配器返回 Anthropic dict。
    返回 (handled, response)。"""
    platform, _, real_model = model.partition("/")
    if not real_model or platform == "workbuddy":
        return False, {}
    adapter = svc.adapters.get(platform)
    if not adapter:
        return True, {"type": "error", "error": {"type": "invalid_request",
                      "message": f"未知平台 {platform}"}}
    openai_req = {
        "model": real_model,
        "messages": _anthropic_to_openai_messages(req),
        "max_tokens": req.get("max_tokens") or 4096,
        "stream": False,  # 新平台统一走非流式聚合，避免两套流式翻译
    }
    # 工具定义：Anthropic input_schema → OpenAI function.parameters
    tools = [{"type": "function", "function": {
        "name": t["name"], "description": t.get("description") or "",
        "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
        for t in (req.get("tools") or []) if isinstance(t, dict) and isinstance(t.get("name"), str)]
    if tools:
        openai_req["tools"] = tools
        choice = (req.get("tool_choice") or {}).get("type") if isinstance(req.get("tool_choice"), dict) else None
        if choice == "any":
            openai_req["tool_choice"] = "required"
        elif choice == "tool":
            openai_req["tool_choice"] = {"type": "function", "function": {
                "name": (req.get("tool_choice") or {}).get("name")}}
        elif choice == "auto":
            openai_req["tool_choice"] = "auto"
    try:
        result = svc.chat(platform, real_model, openai_req)
    except Exception as e:
        return True, {"type": "error", "error": {"type": "api_error", "message": str(e)[:200]}}
    return True, _openai_to_anthropic(result, real_model)



def utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def build_service() -> HubService:
    return HubService(load_registry())


def cmd_status(svc: HubService, service: bool = False) -> None:
    data = svc.status()
    print(f"平台状态  {data['at']}")
    for p in data["platforms"]:
        flag = "OK" if p["ok"] else "待补"
        print(f"  [{flag:4s}] {p['displayName']} ({p['platform']}): {p['message']}")
        print(f"           声明模型: {', '.join(p['declaredModels'][:6])}" +
              (" ..." if len(p["declaredModels"]) > 6 else ""))
    if service:
        print("\n服务状态")
        for name, port, probe in (("面板", 8798, "/api/status"),
                                   ("网关", 8899, "/v1/models")):
            info = probe_service(port, probe)
            if info["alive"]:
                print(f"  [RUN ] {name}  127.0.0.1:{port}  PID {info['pid']}  HTTP {info['http']}")
            else:
                print(f"  [STOP] {name}  127.0.0.1:{port}  {info['reason']}")
        print("\n客户端版本（适配值 vs 本机实测；漂移建议跑 probe）")
        from core.versions import drift_report
        for r in drift_report(list(svc.adapters)):
            flag = "DRIFT" if r["drifted"] else "~" if r["note"] else "OK"
            cosy = f" cosy={r['cosy']}" if r["cosy"] else ""
            print(f"  [{flag:5s}] {r['platform']:14s} 适配 {r['expected']:8s} / 本机 {r['actual']:10s}{cosy}"
                  + (f"  ({r['note']})" if r["note"] else ""))


def probe_service(port: int, probe_path: str) -> dict[str, Any]:
    """只读探测本地服务：端口是否有人监听 + HTTP 探针响应。"""
    import socket
    from urllib.request import urlopen

    out: dict[str, Any] = {"alive": False, "pid": "", "http": "", "reason": ""}
    # 端口监听检测（尝试连接本地端口）
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        listening = True
    except OSError:
        listening = False
    if not listening:
        out["reason"] = "端口未监听"
        return out
    # 尝试拿 PID（Windows 用 netstat，失败不影响探测）
    out["pid"] = netstat_listening_pid(port)
    try:
        with urlopen(f"http://127.0.0.1:{port}{probe_path}", timeout=3) as res:
            out["http"] = res.status
            out["alive"] = 200 <= res.status < 400
    except Exception as e:
        out["reason"] = f"端口通但探测失败：{str(e)[:60]}"
    return out


def cmd_probe(svc: HubService, platform: str | None = None) -> int:
    """主动探测各平台协议是否仍可用（max_tokens=1，消耗极小）。

    验证三件事：登录态可读、签名/请求头仍被接受、模型 key 仍有效。
    返回 0=全部通过，1=有平台失败（可用于计划任务告警）。
    """
    status = svc.status()
    logged = {p["platform"] for p in status["platforms"] if p["ok"]}
    targets = [platform] if platform else sorted(logged)
    if not targets:
        print("没有已登录的平台可探测")
        return 0
    failures: list[str] = []
    for pid in targets:
        adapter = svc.adapters.get(pid)
        if not adapter:
            print(f"  [FAIL] {pid}: 未知平台")
            failures.append(pid)
            continue
        if pid not in logged and platform is None:
            print(f"  [SKIP] {pid}: 未登录")
            continue
        # 选最便宜且最常用的 DS 模型
        from core.protocols import VERIFIED_MODELS
        models = VERIFIED_MODELS.get(pid, {})
        model = next((m for m in models if "deepseek" in m.lower()),
                     next(iter(models), adapter.declared_models[0] if adapter.declared_models else ""))
        try:
            r = svc.chat(pid, model, {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 1})
            choices = r.get("choices") or []
            msg = (choices[0].get("message") or {}) if choices and isinstance(choices[0], dict) else {}
            ok = "error" not in r and bool(msg.get("content") or msg.get("tool_calls"))
            detail = "OK" if ok else str(r.get("error", {}).get("message", "内容为空"))[:80]
        except Exception as e:
            ok, detail = False, str(e)[:80]
        print(f"  [{'PASS' if ok else 'FAIL'}] {pid:14s} model={model:28s} {detail}")
        if not ok:
            failures.append(pid)
    print(f"\n探测结果：{'全部通过' if not failures else '失败 ' + ', '.join(failures)}")
    return 0 if not failures else 1


def cmd_sync(svc: HubService) -> None:
    """扫描并纳管所有平台账号：WorkBuddy 写网关池+bind 签到，其余平台报告当前账号。"""
    from core.sync import sync_all
    report = sync_all(svc)
    wb = report.get("workbuddy", {})
    print(f"WorkBuddy：发现 {wb.get('found', 0)} 个账号，网关池写入 {len(wb.get('pool_written', []))} 个")
    for item in wb.get("bind_added", []):
        if item.get("error"):
            print(f"  [跳过] {item['name']} ({item['uid']})：{item['error']}")
        else:
            print(f"  [新增签到] {item['name']} ({item['uid']})")
    skipped = wb.get("bind_skipped", [])
    if skipped:
        print(f"  已纳管 {len(skipped)} 个：{', '.join(skipped)}")
    print("其它平台当前账号：")
    for platform, accts in (report.get("platforms") or {}).items():
        print(f"  {platform:14s}: {', '.join(accts) if accts else '未登录'}")


def cmd_checkin(svc: HubService, platform: str | None) -> None:
    results = svc.run_checkin(platform)
    if not results:
        print("没有可签到的账号（协议待补或平台未登录）")
        return
    for r in results:
        print(f"[{r['status']:12s}] {r['platform']}/{r['label']} +{r['gained']:.2f} {r['message']}")


def cmd_selftest(svc: HubService, offline: bool = False) -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    check("加载全部平台适配器", len(svc.adapters) >= 1, str(list(svc.adapters)))
    status = svc.status()
    check("status.json 可写入", STATUS_FILE.exists())
    logged = [p for p in status["platforms"] if p["ok"]]
    check("至少一家平台已登录", len(logged) >= 1,
          ", ".join(f"{p['platform']}={'OK' if p['ok'] else 'NO'}" for p in status["platforms"]))
    check("各家声明 deepseek/glm", all(
        any("deepseek" in m.lower() for m in p["declaredModels"])
        and any("glm" in m.lower() for m in p["declaredModels"])
        for p in status["platforms"]
    ))
    # Kimi：Loomy 无 Kimi 是用户已确认接受的例外
    check("除 Loomy 外均声明 kimi", all(
        any("kimi" in m.lower() for m in p["declaredModels"])
        for p in status["platforms"] if p["platform"] != "loomy"
    ))
    limits = svc.refresh_limits()
    check("limits.json 可写入", LIMITS_FILE.exists() and isinstance(limits["limits"], list),
          f"共 {len(limits['limits'])} 条（含 WorkBuddy 网关限流）")
    accts = svc.accounts()
    check("发现平台账号（数量因本机登录情况而异）", len(accts) >= 1,
          f"共 {len(accts)} 个：" + ", ".join(a.key for a in accts[:12]))
    # chat 冒烟：联网验证网关链路（会消耗极少额度）；--offline 可跳过。
    # 只对实际已登录的平台做冒烟，且必须拿到真实内容才算 PASS（避免 error 假阳性）。
    if offline:
        print("  [SKIP] 网关 chat 冒烟（--offline，不联网、不消耗额度）")
    else:
        smoke_platform = None
        for pid in ("workbuddy", "qoder-intl", "qwenwork", "xiaohuanxiong", "traework", "loomy"):
            if any(p["platform"] == pid and p["ok"] for p in status["platforms"]):
                smoke_platform = pid
                break
        if not smoke_platform:
            print("  [SKIP] 网关 chat 冒烟（无已登录平台）")
        else:
            model = "claude-sonnet-1" if smoke_platform == "workbuddy" else \
                next(iter(svc.adapters[smoke_platform].declared_models), "")
            chat = svc.chat(smoke_platform, model,
                            {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8})
            choices = chat.get("choices") or []
            msg = (choices[0].get("message") or {}) if choices and isinstance(choices[0], dict) else {}
            has_content = bool(msg.get("content") or msg.get("tool_calls"))
            check(f"网关 chat 返回真实内容（{smoke_platform}）", has_content,
                  "OK" if has_content else str(chat.get("error", {}).get("message", "内容为空"))[:80])
    # 治理层：冷却标记能落盘且 preflight 能识别（不实际发请求）
    from core.governance import Governance
    g = Governance()
    g.mark_model("__selftest__", "u1", "m1", ms=60_000)
    check("模型冷却 preflight 生效", bool(g.wait_reason("__selftest__", "u1", "m1")),
          g.wait_reason("__selftest__", "u1", "m1"))
    g.clear("__selftest__", "u1", "m1")
    check("冷却清除后恢复", not g.wait_reason("__selftest__", "u1", "m1"))
    import core.workbuddy_native as wn
    wn.POOL._scan(force=True)
    check("8899 Anthropic 原生网关可用", len(wn.POOL.credentials) >= 1 and WB_GATEWAY_CONFIG.exists(),
          f"原生协议层加载 {len(wn.POOL.credentials)} 个 WorkBuddy 账号")
    # sync 纳管：只验证扫描能发现全部 WorkBuddy 账号（不写盘）
    from core.sync import _load_sessions, _signin_module, _pool_payload
    sg = _signin_module()
    uids = set()
    for _label, sess in _load_sessions():
        got = _pool_payload(sg, sess)
        if got:
            uids.add(got[0])
    check("账号纳管扫描可发现 WorkBuddy 号", len(uids) >= 1, f"发现 {len(uids)} 个唯一 uid")
    print(f"\n自检结果：{'全部通过' if not failures else '失败 ' + ', '.join(failures)}")
    return 0 if not failures else 1


# ---------------------------------------------------------------- 面板 HTML
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>多平台积分中心</title>
<style>
  :root { color-scheme: light; --bg:#f5f6f8; --card:#fff; --border:#e8eaed; --text:#1a1d21;
          --muted:#6b7280; --subtle:#9ca3af; --brand:#4f46e5; --ok:#059669; --warn:#d97706; --hot:#dc2626; }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px 28px 44px; font-family:"Inter","Segoe UI","Microsoft YaHei",sans-serif;
         background:var(--bg); color:var(--text); -webkit-font-smoothing:antialiased; }
  .app { max-width:1500px; margin:0 auto; }
  header { display:flex; align-items:baseline; justify-content:space-between; gap:12px; margin-bottom:16px; flex-wrap:wrap; }
  h1 { font-size:20px; font-weight:650; letter-spacing:-.3px; margin:0; }
  .meta { color:var(--muted); font-size:12px; }
  .layout { display:grid; grid-template-columns:minmax(0,1.15fr) minmax(0,1fr); gap:16px; align-items:start; }
  .col { display:flex; flex-direction:column; gap:14px; min-width:0; }
  .kpis { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; }
  .kpi { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:14px 16px; }
  .kpi-label { font-size:11px; color:var(--muted); letter-spacing:.3px; }
  .kpi-value { font-size:23px; font-weight:650; letter-spacing:-.5px; margin-top:6px; font-variant-numeric:tabular-nums; }
  .kpi-sub { font-size:11px; color:var(--subtle); margin-top:4px; }
  .accounts { display:grid; grid-template-columns:repeat(2,1fr); gap:10px; }
  .acct { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:14px 16px; }
  .acct-head { display:flex; align-items:center; gap:9px; }
  .avatar { width:30px; height:30px; border-radius:9px; display:grid; place-items:center; color:#fff;
            font-size:13px; font-weight:600; flex-shrink:0; }
  .acct-name { font-size:13px; font-weight:600; }
  .acct-uin { font-size:11px; color:var(--subtle); font-variant-numeric:tabular-nums; }
  .plat-tag { font-size:10px; color:var(--muted); background:#f1f3f5; border-radius:4px; padding:2px 6px; }
  .edit { margin-left:auto; cursor:pointer; color:var(--subtle); font-size:14px; line-height:1; padding:2px 4px; border-radius:4px; }
  .edit:hover { color:var(--brand); background:#eef2ff; }
  .acct-value { font-size:22px; font-weight:650; letter-spacing:-.4px; margin-top:13px; font-variant-numeric:tabular-nums; }
  .acct-value span { font-size:12px; color:var(--muted); font-weight:400; }
  .bar { height:5px; background:#eef0f3; border-radius:3px; margin-top:10px; overflow:hidden; }
  .bar>i { display:block; height:100%; border-radius:3px; }
  .acct-foot { display:flex; justify-content:space-between; font-size:11px; color:var(--muted); margin-top:7px; }
  .usage { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }
  .usage-card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:12px 14px; }
  .usage-label { font-size:11px; color:var(--muted); }
  .usage-value { font-size:20px; font-weight:650; letter-spacing:-.4px; margin-top:5px; font-variant-numeric:tabular-nums; }
  .usage-note { font-size:11px; color:var(--subtle); margin-top:4px; }
  .panel { background:var(--card); border:1px solid var(--border); border-radius:12px; overflow:hidden; }
  .panel-head { padding:12px 14px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; gap:10px; flex-wrap:wrap; }
  .panel-title { font-size:14px; font-weight:600; }
  .panel-note { font-size:11px; color:var(--subtle); }
  .table-wrap { overflow-x:auto; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th { text-align:left; font-weight:500; color:var(--muted); background:#fafbfc; padding:9px 12px;
       border-bottom:1px solid var(--border); white-space:nowrap; }
  td { padding:10px 12px; border-bottom:1px solid #f1f2f4; white-space:nowrap; }
  tr:last-child td { border-bottom:0; }
  .num { text-align:right; font-variant-numeric:tabular-nums; }
  .badge { display:inline-flex; align-items:center; gap:6px; padding:3px 9px; border-radius:6px; font-size:11px; font-weight:500; }
  .badge.ok { background:#ecfdf5; color:var(--ok); }
  .badge.warn { background:#fffbeb; color:var(--warn); }
  .badge.hot { background:#fef2f2; color:var(--hot); }
  .dot { width:6px; height:6px; border-radius:50%; background:currentColor; flex-shrink:0; }
  .recover { color:var(--hot); font-size:11px; margin-left:7px; font-variant-numeric:tabular-nums; }
  .err { color:var(--hot); font-size:12px; }
  .dim { color:var(--subtle); font-size:12px; }
  .empty { padding:24px 14px; text-align:center; color:var(--subtle); font-size:12px; }
  @media (max-width:1100px){ .layout{grid-template-columns:1fr} .kpis,.accounts{grid-template-columns:repeat(2,1fr)} }
</style>
</head>
<body>
<div class="app">
<header>
  <h1>多平台积分中心</h1>
  <div class="meta" id="meta">加载中…</div>
</header>
<div class="layout">
  <div class="col">
    <div class="kpis" id="kpis"></div>
    <div class="usage" id="usage"></div>
    <div class="accounts" id="accounts"><div class="dim">加载中…</div></div>
  </div>
  <div class="col">
    <div class="panel">
      <div class="panel-head"><span class="panel-title">模型限流状态</span><span class="panel-note" id="status-meta"></span></div>
      <div id="model-status" class="dim" style="padding:14px">加载中…</div>
    </div>
  </div>
</div>
<script>
const fmt = (n) => (n===null||n===undefined||!isFinite(n)) ? '—' : Number(n).toFixed(2);
const PALETTE = ['#4f46e5','#0891b2','#c026d3','#ea580c','#059669','#2563eb'];
// 备注存服务端 data/aliases.json（不再用 localStorage —— 它按端口隔离，8799 的备注在 8798 读不到）
let aliases = {};
fetch('/api/aliases',{cache:'no-store'}).then(r=>r.json()).then(d=>{ aliases=d||{}; renderAll(); }).catch(()=>{});
function aliasOf(key, fallback) {
  if (aliases[key]) return aliases[key];
  const uid = String(key).split(':').slice(1).join(':');
  return aliases[uid] || fallback || key;
}
async function editAlias(key, current) {
  const v = window.prompt('给这个账号起个备注名（留空恢复默认）：', aliasOf(key, current));
  if (v === null) return;
  try {
    aliases = await (await fetch('/api/aliases',{method:'POST',headers:{'content-type':'application/json'},
      body: JSON.stringify({key, value: v.trim()})})).json();
    renderAll();
  } catch(e) { alert('保存备注失败: '+e); }
}
function avatar(name, key) {
  const idx = [...String(key)].reduce((a,c)=>a+c.charCodeAt(0),0) % PALETTE.length;
  const text = (aliasOf(key,name) || '?').trim().slice(0,1).toUpperCase();
  return `<span class="avatar" style="background:${PALETTE[idx]}">${text}</span>`;
}
let CACHE = {};
function renderCredits(data) {
  CACHE.overview = data;
  // WorkBuddy 账号排在最前，其余平台随后；同平台内按余额高的在前
  const rows = [...(data.accounts || [])].sort((a,b) => {
    if (a.platform === 'workbuddy' && b.platform !== 'workbuddy') return -1;
    if (b.platform === 'workbuddy' && a.platform !== 'workbuddy') return 1;
    const ar = typeof a.remain === 'number' ? a.remain : -1;
    const br = typeof b.remain === 'number' ? b.remain : -1;
    return br - ar;
  });
  const ok = rows.filter(r => r.ok && typeof r.remain === 'number' && isFinite(r.remain));
  const remain = ok.reduce((s,r)=>s+r.remain,0);
  const withTotal = ok.filter(r=>typeof r.total==='number' && r.total>0);
  const total = withTotal.reduce((s,r)=>s+r.total,0);
  const used = total ? total - withTotal.reduce((s,r)=>s+r.remain,0) : 0;
  const u = data.usage || {}, g = data.gains || {};
  document.getElementById('meta').textContent = '更新于 ' + data.at + ' · 自动刷新';
  document.getElementById('kpis').innerHTML = [
    ['平台 / 账号', new Set(rows.map(r=>r.platform)).size + ' / ' + rows.length, '已接入 / 已发现'],
    ['总剩余', ok.length ? fmt(remain) : '—', '已提供余额的平台合计'],
    ['今日净变化', fmt((g.today||0)-(u.today||0)), '签到 +' + fmt(g.today||0) + ' · 消耗 -' + fmt(u.today||0)],
    ['今日签到', '+' + fmt(g.today||0), '积分入账'],
  ].map(([label,value,sub]) => `<div class="kpi"><div class="kpi-label">${label}</div><div class="kpi-value">${value}</div><div class="kpi-sub">${sub}</div></div>`).join('');
  document.getElementById('accounts').innerHTML = rows.map(r => {
    const key = r.key || r.platform + ':' + r.uid;
    const head = `<div class="acct-head">${avatar(r.label,key)}
        <div><div class="acct-name">${aliasOf(key,r.label||r.uid)}</div><div class="acct-uin">${r.uin||r.uid||''} <span class="plat-tag">${r.platform}</span></div></div>
        <span class="edit" title="备注名" onclick="editAlias('${key}', '${(r.label||'').replace(/'/g,"")}')">✎</span>
      </div>`;
    if (!r.ok) return `<div class="acct">${head}<div class="err" style="margin-top:12px">${r.error||'查询失败'}</div></div>`;
    const hasRemain = typeof r.remain === 'number' && isFinite(r.remain);
    const hasTotal = typeof r.total === 'number' && isFinite(r.total) && r.total > 0;
    const hasUsed = typeof r.used === 'number' && isFinite(r.used);
    const pct = hasTotal && hasRemain ? r.remain/r.total*100 : 0;
    const color = pct>50 ? 'var(--brand)' : pct>20 ? 'var(--warn)' : 'var(--hot)';
    const value = hasRemain
      ? `${fmt(r.remain)}<span>${hasTotal ? ' / '+fmt(r.total) : ' 无总额度'}</span>`
      : `<span style="font-size:14px">— 该平台无数值余额接口</span>`;
    const foot = hasTotal
      ? `<span>剩余 ${pct.toFixed(1)}%</span><span>已用 ${hasUsed ? fmt(r.used) : '—'}</span>`
      : `<span>${hasUsed ? '已用 '+fmt(r.used) : (r.detail || '无已用数据')}</span>`;
    return `<div class="acct">${head}
      <div class="acct-value" style="${hasRemain?'':'font-size:14px;font-weight:500;color:var(--muted)'}">${value}</div>
      ${hasTotal ? `<div class="bar"><i style="width:${pct.toFixed(1)}%;background:${color}"></i></div>` : ''}
      <div class="acct-foot">${foot}</div>
    </div>`;
  }).join('');
  const us = CACHE.usage || u;
  document.getElementById('usage').innerHTML = [
    ['今日', us.today, us.gains_today, '网关日志 · 今日 00:00 起'],
    ['近 7 天', us.last7, us.gains_last7, '网关日志口径（不含非反代消耗）'],
    ['近 30 天', us.last30, us.gains_last30, '网关日志口径（不含非反代消耗）'],
  ].map(([label,spent,gain,note]) => `<div class="usage-card"><div class="usage-label">${label}</div><div class="usage-value">-${fmt(spent)} <span style="font-size:13px;color:var(--ok)">+${fmt(gain)}</span></div><div class="usage-note">净 ${fmt((gain||0)-(spent||0))} · ${note}</div></div>`).join('');
}
function renderModelStatus(d) {
  const el = document.getElementById('model-status');
  const items = d.modelStatus || [];
  const credits = (CACHE.overview?.accounts) || [];
  document.getElementById('status-meta').textContent = (d.note || '') + ' · 每 60 秒刷新';
  if (!items.length) { el.innerHTML = '<div class="empty">暂无限流数据（网关无 429 记录时为空）</div>'; return; }
  el.innerHTML = '<div class="table-wrap"><table><thead><tr><th>模型</th><th>账号</th><th>状态</th><th class="num">剩余</th><th class="num">消耗</th></tr></thead><tbody>' +
    items.flatMap(s => {
      const free = /hy4flash|free/i.test(s.model);
      return (s.cells || []).map((c, i) => {
        const bal = credits.find(b => String(b.uin) === String(c.account));
        const remain = bal && typeof bal.remain === 'number' ? fmt(bal.remain) : '—';
        const used = free ? '<span style="color:var(--ok)">免费</span>'
                          : (bal && typeof bal.used === 'number' ? fmt(bal.used) : '—');
        const key = bal ? (bal.key || bal.platform+':'+bal.uid) : c.account;
        const displayName = bal ? aliasOf(key, c.name || c.account) : (c.name || c.account);
        const limited = c.state === 'limited';
        const badge = limited
          ? `<span class="badge hot"><span class="dot"></span>限流中</span><span class="recover">${c.recovery || ''} 恢复</span>`
          : `<span class="badge ok"><span class="dot"></span>正常</span>`;
        const model = i === 0 ? `<td rowspan="${(s.cells||[]).length}"><b>${s.model}</b><div class="panel-note" style="margin-top:4px">${free ? '免费模型 · ' : ''}429 ${s.rate429||0} 次</div></td>` : '';
        return `<tr>${model}<td>${displayName}</td><td>${badge}</td><td class="num">${remain}</td><td class="num">${used}</td></tr>`;
      });
    }).join('') + '</tbody></table></div>';
}
function renderAll() {
  if (CACHE.overview) renderCredits(CACHE.overview);
}
async function load() {
  const [overview, models, usage] = await Promise.all([
    fetch('/api/overview',{cache:'no-store'}).then(r=>r.json()),
    fetch('/api/models',{cache:'no-store'}).then(r=>r.json()),
    fetch('/api/usage',{cache:'no-store'}).then(r=>r.json()),
  ]);
  CACHE.usage = usage;
  renderCredits(overview);
  renderModelStatus(models);
}
load().catch(e => { document.getElementById('meta').textContent = '加载失败: ' + e; });
setInterval(load, 10000);
</script>
</body>
</html>
"""


MAX_BODY_BYTES = 4 * 1024 * 1024  # 请求体上限，防止恶意 Content-Length 打满内存


class LocalOnlyMixin:
    """本地服务的基础防护：Host 白名单 + Origin 校验 + 请求体大小/类型限制。

    阻止任意网页用 no-cors POST 静默烧额度/改备注，以及 DNS rebinding 读取面板。
    """

    def _reject_foreign_request(self) -> bool:  # type: ignore[no-untyped-def]
        host = (self.headers.get("host") or "").split(":")[0].lower()  # type: ignore[attr-defined]
        if host not in ("127.0.0.1", "localhost", "::1"):
            self._json({"error": "forbidden host"}, 403)  # type: ignore[attr-defined]
            return True
        origin = (self.headers.get("origin") or "").strip()
        if origin:
            o = urlparse(origin)
            if o.hostname not in ("127.0.0.1", "localhost", "::1"):
                self._json({"error": "forbidden origin"}, 403)  # type: ignore[attr-defined]
                return True
        return False

    def _read_body(self) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
        """读取并校验 JSON body；失败时已写出 400/413，返回 None。"""
        try:
            n = int(self.headers.get("content-length") or 0)  # type: ignore[attr-defined]
        except ValueError:
            self._json({"error": "invalid content-length"}, 400)  # type: ignore[attr-defined]
            return None
        if n < 0 or n > MAX_BODY_BYTES:
            self._json({"error": "body too large"}, 413)  # type: ignore[attr-defined]
            return None
        try:
            data = json.loads(self.rfile.read(n) or b"{}")  # type: ignore[attr-defined]
        except (ValueError, json.JSONDecodeError):
            self._json({"error": "invalid json"}, 400)  # type: ignore[attr-defined]
            return None
        if not isinstance(data, dict):
            self._json({"error": "body must be an object"}, 400)  # type: ignore[attr-defined]
            return None
        return data


class PanelHandler(LocalOnlyMixin, BaseHTTPRequestHandler):
    svc: HubService

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self._reject_foreign_request():
            return
        path = urlparse(self.path).path
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("content-length", "0")
            self.end_headers()
        elif path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/overview":
            self._json(self.svc.snapshot_credits())
        elif path == "/api/status":
            self._json(self.svc.status())
        elif path == "/api/usage":
            self._json(self.svc.usage())
        elif path == "/api/models":
            self._json(self.svc.model_status())
        elif path == "/api/limits":
            self._json(read_json(LIMITS_FILE, {"limits": []}))
        elif path == "/api/health":
            from core.governance import GOVERNANCE
            self._json(GOVERNANCE.snapshot())
        elif path == "/api/aliases":
            self._json(self.svc.get_aliases())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if self._reject_foreign_request():
            return
        if path == "/api/aliases":
            data = self._read_body()
            if data is None:
                return
            self._json(self.svc.set_alias(str(data.get("key", "")), str(data.get("value", ""))))
        else:
            self._json({"error": "not found"}, 404)


class GatewayHandler(LocalOnlyMixin, BaseHTTPRequestHandler):
    svc: HubService

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self._reject_foreign_request():
            return
        path = urlparse(self.path).path
        if path == "/v1/models":
            # 默认只列统一别名：客户端不必关心平台，后台自动代理 + 失败换平台。
            # 需要绑定具体平台时仍可手填 "平台/模型"（见 ALIAS_ROUTES / declared_models）。
            models: list[dict[str, Any]] = [
                {"id": a, "object": "model", "owned_by": "multi-ai-hub"}
                for a in ALIAS_ROUTES
            ]
            self._json({"object": "list", "data": models})
        elif path in ANTHROPIC_PATHS:
            self._proxy_anthropic(path, b"")
        else:
            self._json({"error": {"message": "not found", "type": "invalid_request"}}, 404)

    def _proxy_anthropic(self, path: str, body: bytes) -> None:
        """原生处理 Anthropic /v1/messages（不再透传 8789）。"""
        import core.workbuddy_native as wn
        if path == "/v1/messages/count_tokens":
            # count_tokens 轻量本地估算，避免依赖 8789
            try:
                payload = json.loads(body or b"{}")
                txt = json.dumps(payload.get("messages") or [], ensure_ascii=False)
                self._json({"type": "count_tokens", "count": wn.estimate_tokens(txt)})
            except ValueError:
                self._json({"type": "error", "error": {"type": "invalid_request", "message": "invalid json"}}, 400)
            return
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            self._json({"type": "error", "error": {"type": "invalid_request", "message": "invalid json"}}, 400)
            return
        # 平台前缀分发：
        #   workbuddy/模型 → 剥掉前缀，走原生协议（上游不认识带前缀的模型名，直接传会 11102）
        #   其它平台/模型   → 走对应适配器
        model_name = payload.get("model") if isinstance(payload.get("model"), str) else ""
        if model_name.startswith("workbuddy/"):
            payload = {**payload, "model": model_name[len("workbuddy/"):]}
            model_name = payload["model"]
        elif "/" in model_name:
            handled, resp = _dispatch_platform_model(self.svc, model_name, payload)
            if handled:
                self._json(resp, 200 if resp.get("type") != "error" else 400)
                return
        preferred = self.headers.get("x-wb-account")
        # 统一别名 → WorkBuddy 真实模型：让只显示模型的客户端无需关心平台。
        model_name = payload.get("model") if isinstance(payload.get("model"), str) else ""
        if model_name in ALIAS_ROUTES:
            wb_model = next((mid for pid, mid in ALIAS_ROUTES[model_name] if pid == "workbuddy"), None)
            if wb_model:
                payload = {**payload, "model": wb_model}
        try:
            status, result, is_stream, meta = wn.handle_messages(payload, preferred)
        except Exception as e:
            self._json({"type": "error", "error": {"type": "api_error",
                        "message": f"网关内部错误：{str(e)[:150]}"}}, 500)
            return
        if is_stream:
            self.send_response(status)
            self.send_header("content-type", "text/event-stream; charset=utf-8")
            self.send_header("cache-control", "no-cache")
            self.send_header("connection", "close")
            self.send_header("x-accel-buffering", "no")
            self.send_header("x-wb-model-used", str(meta.get("used_model", "")))
            self.end_headers()
            try:
                for chunk in result:
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()  # 立即送出，避免客户端长时间等不到字节而读超时
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                # 上游/翻译器异常必须在流内告知客户端，否则半截流会让客户端报 ECONNRESET。
                from core.workbuddy_native import format_sse, safe_error
                try:
                    self.wfile.write(format_sse("error", {
                        "type": "error",
                        "error": {"type": "api_error", "message": safe_error(e)},
                    }).encode("utf-8"))
                    self.wfile.write(format_sse("message_stop", {"type": "message_stop"}).encode("utf-8"))
                except (BrokenPipeError, ConnectionResetError):
                    pass
        else:
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("x-wb-model-used", str(meta.get("used_model", "")))
            data = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        if self._reject_foreign_request():
            return
        path = urlparse(self.path).path
        if path in ANTHROPIC_PATHS:
            n = 0
            try:
                n = int(self.headers.get("content-length") or 0)
            except ValueError:
                self._json({"error": {"message": "invalid content-length", "type": "invalid_request"}}, 400)
                return
            if n < 0 or n > MAX_BODY_BYTES:
                self._json({"error": {"message": "body too large", "type": "invalid_request"}}, 413)
                return
            self._proxy_anthropic(path, self.rfile.read(n))
            return
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._json({"error": {"message": "not found", "type": "invalid_request"}}, 404)
            return
        payload = self._read_body()
        if payload is None:
            return
        model = str(payload.get("model") or "")
        explicit = "/" in model
        preferred = self.headers.get("x-hub-account")
        # sticky 依据：客户端给的 session id，否则用首个用户消息文本哈希
        session_key = self.headers.get("x-hub-session")
        if not session_key:
            msgs = payload.get("messages") or []
            for m in msgs:
                if isinstance(m, dict) and m.get("role") == "user":
                    c = m.get("content")
                    session_key = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                    break

        if explicit:
            platform, _, real_model = model.partition("/")
            result = self.svc.chat(platform, real_model, payload, preferred, session_key=session_key)
        else:
            routes = alias_routes(model)
            if not routes:
                # 兼容旧 Claude 假名（走 WorkBuddy 原生映射）
                alias = workbuddy_model_map()
                routes = [("workbuddy", alias.get(model, model))]
            # 自动代理：逐个平台尝试；只有限流/ overload 才换平台，参数错误直接返回
            result: dict[str, Any] = {}
            for pid, mid in routes:
                result = self.svc.chat(pid, mid, payload, preferred, session_key=session_key)
                err = result.get("error") or {}
                if "error" not in result:
                    if isinstance(result.get("usage"), dict):
                        result["usage"]["routed_platform"] = pid
                    break
                text = f"{err.get('type', '')} {err.get('message', '')}".lower()
                if not any(k in text for k in ("rate", "limit", "429", "overload", "unsupported", "unknown")):
                    break  # 认证/参数错误，换平台也不会好
        status = 400 if "error" in result else 200
        self._json(result, status)


def main(argv: list[str] | None = None) -> int:
    utf8_stdout()
    parser = argparse.ArgumentParser(description="多平台签到/反代/面板统一框架")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status")
    p.add_argument("--service", action="store_true", help="同时检查面板/网关服务是否存活")
    p = sub.add_parser("selftest")
    p.add_argument("--offline", action="store_true", help="跳过联网 chat 冒烟（不消耗额度）")
    p = sub.add_parser("probe")
    p.add_argument("--platform", help="只探测指定平台（默认探测所有已登录平台）")
    sub.add_parser("sync")  # 自动识别并纳管所有平台账号到签到/网关/面板
    p = sub.add_parser("checkin")
    p.add_argument("--platform")
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=8798)
    p = sub.add_parser("gateway")
    p.add_argument("--port", type=int, default=8899)
    args = parser.parse_args(argv)

    svc = build_service()
    if args.cmd == "status":
        cmd_status(svc, service=args.service)
    elif args.cmd == "selftest":
        return cmd_selftest(svc, offline=args.offline)
    elif args.cmd == "probe":
        return cmd_probe(svc, platform=args.platform)
    elif args.cmd == "sync":
        cmd_sync(svc)
    elif args.cmd == "checkin":
        cmd_checkin(svc, args.platform)
    elif args.cmd == "serve":
        ensure_port_free(args.port, "面板")
        handler = type("BoundPanelHandler", (PanelHandler,), {"svc": svc})
        print(f"面板 http://127.0.0.1:{args.port}/")
        ThreadingHTTPServer(("127.0.0.1", args.port), handler).serve_forever()
    elif args.cmd == "gateway":
        ensure_port_free(args.port, "网关")
        import core.workbuddy_native as wn
        wn.LOG_MANAGER.start()  # 日志定时清理守护线程（1h 检查 / 14 天过期）
        handler = type("BoundGatewayHandler", (GatewayHandler,), {"svc": svc})
        print(f"网关 http://127.0.0.1:{args.port}/v1")
        ThreadingHTTPServer(("127.0.0.1", args.port), handler).serve_forever()
    return 0


def ensure_port_free(port: int, name: str) -> None:
    """启动前检查端口，占用时给出可操作的提示而不是抛 WinError 10048。"""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
    except OSError as e:
        pid = netstat_listening_pid(port)
        hint = f" PID {pid}" if pid else ""
        raise SystemExit(
            f"端口 {port} 已被占用{hint}（{name}可能已在运行）。\n"
            f"  查看服务：python hub.py status --service\n"
            f"  或换端口：--port {port + 1}（原错误：{str(e)[:80]}）"
        )


if __name__ == "__main__":
    raise SystemExit(main())
