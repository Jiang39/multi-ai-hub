"""五家平台的正式协议层。

登录态解密 / COSY 签名 / chat 请求构造已在 probe_models.py + probe_chat.py
里用真实账号验证通过（2026-11-23 全量最小聊天实测）。这里直接复用那些函数，
只补上「返回 OpenAI 兼容响应」「额度」「签到」三件事。

不重复实现协议、不打印令牌。所有网络请求用标准库 urllib。
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import probe_models as pm  # noqa: E402
import probe_chat as pc  # noqa: E402

TZ_CN = timezone(timedelta(hours=8))

# 各平台在用户账号上验证过的、符合其精确标准的模型（展示名 → 上游调用名）。
# 用户标准：DS=V4.1-Flash(1017 优先)、GLM=5.3、Kimi=3.1pro(无则 K3)。
VERIFIED_MODELS: dict[str, dict[str, str]] = {
    "qoder-intl": {
        "DeepSeek-Flash": "dfmodel", "GLM-5.3": "gmodel", "Kimi-K3.1-Pro": "kmodel_latest",
    },
    "qwenwork": {
        "deepseek-flash": "deepseek-flash", "glm-5.3": "glm-5.3", "kimi-k3.1-pro": "kimi-k3.1-pro",
    },
    "loomy": {
        "DeepSeek-V4.1-Flash": "DeepSeek-V4.1-Flash", "GLM-5.3-Flash": "GLM-5.3-Flash",
    },
    "xiaohuanxiong": {
        "sn-deepseek-v4-1-flash-1017": "sn-deepseek-v4-1-flash-1017",
        "sn-glm-5-3": "sn-glm-5-3", "sn-kimi-k3-1-pro": "sn-kimi-k3-1-pro",
    },
    "traework": {
        "deepseek-v4.1-flash": "deepseek-v4.1-flash", "glm-5.3": "glm-5.3",
        "kimi-k3": "kimi-k3",  # kimi-k3.1-pro 套餐受限(code 1005)，按用户规则回退 K3
    },
}


def now_ms() -> int:
    return int(time.time() * 1000)


def _request(url: str, headers: dict[str, str], data: bytes | None = None,
             method: str = "GET", timeout: int = 30) -> tuple[int, str]:
    req = Request(url, headers=headers, data=data, method=method)
    try:
        with urlopen(req, timeout=timeout) as res:
            return res.status, res.read().decode("utf-8", "replace")
    except HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)[:300]


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return {}


def probe(platform: str) -> tuple[bool, str]:
    """零网络健康检测：登录态文件是否可读、令牌字段非空。
    只读字段结构/长度，不打印令牌值。上游是否仍接受该令牌由实际 chat 判定。"""
    fn = {
        "qoder-intl": pm.qoder_session, "qwenwork": pm.qwenwork_session,
        "loomy": pm.loomy_session, "xiaohuanxiong": pm.xhx_session,
        "traework": pm.traework_session,
    }.get(platform)
    if not fn:
        return False, "无登录态读取器"
    try:
        s = fn()
        token = str(s.get("token") or "")
        if not token:
            return False, "登录态缺少令牌字段"
        return True, f"登录态可读（令牌长度 {len(token)}）"
    except Exception as e:
        return False, f"登录态读取失败：{str(e)[:120]}"


def discover(platform: str) -> list[dict[str, Any]]:
    """从本机登录态发现账号（返回脱敏标识，不含令牌）。"""
    fn = {
        "qoder-intl": pm.qoder_session, "qwenwork": pm.qwenwork_session,
        "loomy": pm.loomy_session, "xiaohuanxiong": pm.xhx_session,
        "traework": pm.traework_session,
    }.get(platform)
    if not fn:
        return []
    try:
        s = fn()
    except Exception:
        return []
    return [{
        "platform": platform,
        "uid": s.get("uid") or s.get("name") or "default",
        "name": s.get("name") or platform,
        "source": platform,
    }]


# ---------------------------------------------------------------- 反代 chat
def attach_tools(body: dict[str, Any], payload: dict[str, Any], platform: str = "") -> None:
    """把 OpenAI 格式 tools/tool_choice 透传给支持它的上游。

    Trae 的 Go 网关要求 tools[].function.parameters 是 JSON 字符串，
    其它平台接受对象。COSY 系（Qoder/QwenWork）本次无工具调用证据，
    透传对象不会报错，但不保证工具能被触发。
    """
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        if platform == "traework":
            tools = [
                {**t, "function": {
                    **t.get("function", {}),
                    "parameters": json.dumps(
                        t.get("function", {}).get("parameters") or {"type": "object", "properties": {}},
                        ensure_ascii=False,
                    ),
                }} if isinstance(t, dict) and isinstance(t.get("function"), dict) else t
                for t in tools
            ]
        body["tools"] = tools
        choice = payload.get("tool_choice")
        if choice is not None:
            body["tool_choice"] = choice


def _extract_tool_calls(raw_tool_calls: Any) -> list[dict[str, Any]]:
    """从上游 delta/message 的 tool_calls 提取标准 OpenAI 结构。"""
    out: list[dict[str, Any]] = []
    if not isinstance(raw_tool_calls, list):
        return out
    for tc in raw_tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        if not isinstance(fn, dict):
            fn = {}
        out.append({
            "id": tc.get("id") or f"call_{uuid.uuid4().hex[:10]}",
            "type": "function",
            "function": {"name": fn.get("name") or "", "arguments": fn.get("arguments") or "{}"},
        })
    return out


def _parse_json_chat(raw: str) -> tuple[str, dict[str, Any]]:
    """标准非流式 OpenAI JSON：choices[0].message.content（或 reasoning_content）。"""
    d = _json(raw)
    if not isinstance(d, dict):
        return "", {}
    if d.get("error"):
        raise RuntimeError(str(d["error"])[:200])
    choices = d.get("choices") or []
    content = ""
    usage = dict(d.get("usage") or {})
    if choices and isinstance(choices[0], dict):
        msg = choices[0].get("message") or {}
        content = msg.get("content") or msg.get("reasoning_content") or ""
        tcs = _extract_tool_calls(msg.get("tool_calls"))
        if tcs:
            usage["__tool_calls__"] = tcs
    return str(content), usage


def _collect_sse(raw: str, *, trae: bool = False) -> tuple[str, dict[str, Any]]:
    """把各家 SSE 流转成 (正文, usage)。兼容 Qoder body / Trae response / 标准 delta。"""
    parts: list[str] = []
    usage: dict[str, Any] = {}
    tool_acc: dict[int, dict[str, Any]] = {}

    def acc_tools(delta: dict[str, Any]) -> None:
        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            idx = int(tc.get("index") or 0)
            slot = tool_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if isinstance(fn, dict):
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]

    cur_event = ""
    for line in raw.splitlines():
        if line.startswith("event:"):
            cur_event = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        d = line[5:].strip()
        if not d or d == "[DONE]":
            continue
        try:
            ev = json.loads(d)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if cur_event == "error" or ev.get("error"):
            err = ev.get("error")
            msg = err.get("message") if isinstance(err, dict) else str(err or "")
            raise RuntimeError(f"上游错误 code={ev.get('code') or (err.get('code') if isinstance(err, dict) else '')} {msg} {ev.get('extra') or ''}"[:300])
        u = ev.get("usage")
        if isinstance(u, dict):
            usage = u
        # 标准 OpenAI delta
        choices = ev.get("choices")
        if isinstance(choices, list) and choices:
            delta = (choices[0] or {}).get("delta") or (choices[0] or {}).get("message") or {}
            if isinstance(delta, dict):
                text = delta.get("content") or delta.get("reasoning_content")
                if isinstance(text, str) and text and text != "[DONE]":
                    parts.append(text)
                acc_tools(delta)
        # Qoder/QwenWork 包在 body
        inner = ev.get("body")
        if isinstance(inner, dict):
            inner = inner.get("content") or inner.get("body") or ""
        if isinstance(inner, str) and inner:
            try:
                nested = json.loads(inner)
                if isinstance(nested, dict):
                    # Qoder 额度不足：body 里是 {"code":"112","message":"...pricingUrl..."}
                    if str(nested.get("code") or "") == "112" or "pricingUrl" in str(nested):
                        raise RuntimeError("账号额度不足（上游 code 112 / pricingUrl）")
                    for ch in nested.get("choices") or []:
                        delta = (ch or {}).get("delta") or (ch or {}).get("message") or {}
                        if isinstance(delta, dict):
                            # 正文优先 content；空正文时用 reasoning_content（推理模型常见）
                            piece = delta.get("content") or delta.get("reasoning_content")
                            if isinstance(piece, str) and piece and piece != "[DONE]":
                                parts.append(piece)
                            acc_tools(delta)
            except ValueError:
                if inner != "[DONE]":
                    parts.append(inner)
        # Trae output
        if trae and cur_event == "output":
            resp = ev.get("response")
            if isinstance(resp, str) and resp:
                parts.append(resp)
    if tool_acc:
        usage = dict(usage)
        usage["__tool_calls__"] = [
            {"id": s["id"] or f"call_{uuid.uuid4().hex[:10]}", "type": "function",
             "function": {"name": s["name"], "arguments": s["args"] or "{}"}}
            for s in (tool_acc[i] for i in sorted(tool_acc)) if s["name"]
        ]
    return "".join(parts), usage


def _openai_response(model: str, text: str, usage: dict[str, Any]) -> dict[str, Any]:
    prompt = usage.get("prompt_tokens") or usage.get("promptTokens") or 0
    completion = usage.get("completion_tokens") or usage.get("completionTokens") or 0
    tool_calls = usage.pop("__tool_calls__", None)
    # 小浣熊 / Loomy 上游自动做 prompt 缓存（无需客户端 cache_control），
    # 缓存命中数藏在 details 里，透传出来供面板/日志统计，不必盲加标记字段。
    details = usage.get("prompt_tokens_details") or usage.get("promptTokensDetails") or {}
    cached = (details.get("cached_tokens") if isinstance(details, dict) else 0) or \
             usage.get("prompt_cache_hit_tokens") or usage.get("cached_tokens") or 0
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    out_usage: dict[str, Any] = {
        "prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion,
    }
    if cached:
        out_usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": out_usage,
    }


def chat(platform: str, model: str, payload: dict[str, Any]) -> dict[str, Any]:
    """调用平台上游，返回 OpenAI 兼容响应。失败返回 {'error': {...}}。"""
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    prompt = "hi"
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                prompt = c
            elif isinstance(c, list):
                prompt = " ".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
            break
    limit = payload.get("max_tokens") or 4096
    try:
        if platform == "qoder-intl":
            sess = pm.qoder_session()
            key = VERIFIED_MODELS[platform].get(model, model)
            url = "https://api3.qoder.sh/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
            body = pc._qoder_body(key)
            body["parameters"] = {"max_tokens": limit}
            attach_tools(body, payload, "qoder-intl")
            raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            encoded = pc._encode_qoder(raw)
            headers = pm._cosy_auth(sess, url, encoded.decode("ascii"))
            headers["Accept"] = "text/event-stream"
            st, text = pc._post_raw(url, headers, encoded)
            content, usage = _collect_sse(text)
        elif platform == "qwenwork":
            sess = pm.qwenwork_session()
            key = VERIFIED_MODELS[platform].get(model, model)
            url = "https://gateway.qwenwork.cn/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common"
            body = pc._qwen_body(key)
            body["parameters"] = {"max_tokens": limit}
            attach_tools(body, payload, "qwenwork")
            rid = uuid.uuid4().hex
            raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            headers = pm._cosy_auth(sess, url, raw)
            headers.update({
                "User-Agent": "qoderwork/1.0.4", "X-QwenWork-Version": "1.0.4",
                "X-QwenWork-Release-Version": "1.0.4-26090412", "X-QwenWork-Build": "26090412",
                "X-QwenWork-Platform": "win32", "X-QwenWork-Arch": "x64", "X-QwenWork-Channel": "stable",
                "Cosy-Version": "1.1.32", "Cosy-ClientType": "6", "Cosy-Business-Product": "qoder_work",
                "Cosy-Business-Type": "agent", "Cosy-Scene": "qwork", "Cosy-MachineOS": "x86_64_win32",
                "Cosy-Data-Policy": "disagree", "X-Model-Key": key, "x-model-key": key,
                "Accept": "text/event-stream",
            })
            st, text = pc._post_raw(url, headers, raw.encode())
            content, usage = _collect_sse(text)
        elif platform == "loomy":
            sess = pm.loomy_session()
            t, tr = sess["token"], uuid.uuid4().hex
            headers = {
                "Accept": "text/event-stream", "Content-Type": "application/json",
                "Authorization": f"Bearer {t}", "token": t,
                "traceparent": f"00-{tr}-{tr[:16]}-01", "loomy-version": "0.9.38",
                "X-Loomy-Request-Purpose": "chat.message",
            }
            body = {"model": model, "messages": messages, "max_tokens": limit, "stream": False}
            attach_tools(body, payload, "loomy")
            raw = json.dumps(body, ensure_ascii=False).encode()
            st, text = pc._post_raw("https://loomyad.xunfei.cn/api/v1/chat/completions", headers, raw)
            content, usage = _parse_json_chat(text)
        elif platform == "xiaohuanxiong":
            sess = pm.xhx_session()
            headers = {
                "Accept": "text/event-stream", "Content-Type": "application/json",
                "Origin": "https://xiaohuanxiong.com",
                "Referer": "https://xiaohuanxiong.com/", "User-Agent": "office-raccoon/1.0.0",
                "X-Client-Platform": "desktop-windows", "X-Client-Version": "v1.0.0",
                "X-Raccoon-Language": "zh", "Authorization": f"Bearer {sess['token']}",
            }
            key = VERIFIED_MODELS[platform].get(model, model)
            body = {"model": key, "messages": messages, "max_tokens": limit, "stream": False}
            attach_tools(body, payload, "xiaohuanxiong")
            raw = json.dumps(body, ensure_ascii=False).encode()
            st, text = pc._post_raw("https://xiaohuanxiong.com/api/web/llm/v2/chat/completions", headers, raw)
            content, usage = _parse_json_chat(text)
        elif platform == "traework":
            sess = pm.traework_session()
            key = VERIFIED_MODELS[platform].get(model, model)
            headers = {
                "Content-Type": "application/json", "Accept": "text/event-stream",
                "Authorization": f"Cloud-IDE-JWT {sess['token']}",
                "X-Cloudide-Token": sess["token"], "X-Ide-Token": sess["token"],
                "X-App-Id": "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8", "X-App-Version": "default",
                "X-Ide-Version": "0.1.56", "X-Ide-Version-Code": "20260820",
                "X-App-Version-Code": "20260820", "X-Ide-Version-Type": "stable",
                "X-Device-Type": "windows", "Request-Traffic-Type": "prod",
                "User-Agent": "TRAE-SOLO-CN/0.1.56",
            }
            if sess.get("device_id"):
                headers["x-device-id"] = sess["device_id"]
            body = pc._trae_body(key)
            body["max_tokens"] = limit
            body["messages"] = messages or [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
            attach_tools(body, payload, "traework")
            if body["messages"] and isinstance(body["messages"][0].get("content"), str):
                body["messages"] = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
            raw = json.dumps(body, ensure_ascii=False).encode()
            st, text = pc._post_raw("https://trae-api-cn.mchost.guru/api/agent/v3/llm_utils_chat", headers, raw)
            content, usage = _collect_sse(text, trae=True)
        else:
            return {"error": {"message": f"未知平台 {platform}", "type": "invalid_request"}}
    except Exception as e:
        return {"error": {"message": str(e)[:300], "type": "upstream_error"}}
    if st >= 400:
        return {"error": {"message": text[:300], "type": "upstream_error", "status": st}}
    return _openai_response(model, content, usage)


# ---------------------------------------------------------------- 额度查询
def credits(platform: str) -> dict[str, Any]:
    """读余额。返回 {ok, remain, total, detail}；无明确余额接口的平台如实说明。"""
    try:
        if platform == "xiaohuanxiong":
            s = pm.xhx_session()
            h = {"Accept": "application/json", "Origin": "https://xiaohuanxiong.com",
                 "Referer": "https://xiaohuanxiong.com/", "User-Agent": "office-raccoon/1.0.0",
                 "X-Client-Platform": "desktop-windows", "X-Client-Version": "v1.0.0",
                 "X-Raccoon-Language": "zh", "Authorization": f"Bearer {s['token']}"}
            st, txt = _request("https://xiaohuanxiong.com/api/web/points/v1/balance", h)
            d = _json(txt).get("data") or {}
            return {"ok": st == 200, "remain": d.get("available_points"), "total": None,
                    "detail": f"可用 {d.get('available_points')} / 日 {d.get('daily_points')} / 月 {d.get('monthly_points')}"}
        if platform == "loomy":
            s = pm.loomy_session()
            t, tr = s["token"], uuid.uuid4().hex
            h = {"Accept": "application/json", "Authorization": f"Bearer {t}", "token": t,
                 "traceparent": f"00-{tr}-{tr[:16]}-01", "loomy-version": "0.9.38"}
            st, txt = _request("https://loomyad.xunfei.cn/api/v1/points/records?record_type=all&page_no=1&page_size=1", h)
            d = _json(txt)
            data = d.get("data") if isinstance(d.get("data"), dict) else d
            return {"ok": st == 200, "remain": data.get("balance"), "total": None,
                    "detail": f"余额 {data.get('balance')} / 可用 {data.get('available')}"}
        if platform == "qoder-intl":
            return _credits_qoder()
        if platform == "qwenwork":
            return _credits_qwenwork()
        if platform == "traework":
            return _credits_trae()
    except Exception as e:
        return {"ok": False, "remain": None, "total": None, "error": str(e)[:200]}
    return {"ok": False, "remain": None, "total": None, "error": "无余额接口"}


def _quota_balance(sess_token: str, url: str, *, camel: bool) -> tuple[int, Any, str]:
    """Qoder/QwenWork 共用的配额接口：普通 Bearer，签名头反而 401。"""
    h = {"Accept": "application/json", "Authorization": f"Bearer {sess_token}",
         "User-Agent": "quota-probe"}
    st, txt = _request(url, h)
    d = _json(txt)
    total_key = "userQuota" if camel else "user_quota"
    add_key = "addOnQuota" if camel else "add_on_quota"
    quota = d.get(total_key) or d.get(add_key) or {}
    if not isinstance(quota, dict):
        quota = {}
    def pick(*keys: str) -> Any:
        for k in keys:
            if quota.get(k) is not None:
                return quota[k]
        return None
    remain = pick("remaining", "remain", "available")
    total = pick("total", "limit", "amount")
    used = pick("used", "consumed")
    unit = str(quota.get("unit") or d.get("usageType") or "")
    detail = f"配额剩余 {remain} / 总 {total} / 已用 {used}" + (f"（{unit}）" if unit else "")
    if not quota:
        detail = "账号为百分比配额制（无数值余额），isQuotaExceeded=" + str(
            d.get("isQuotaExceeded", d.get("is_quota_exceeded", "unknown")))
    return st, {"remain": remain, "total": total, "used": used, "detail": detail}, txt


def _credits_qoder() -> dict[str, Any]:
    s = pm.qoder_session()
    st, d, txt = _quota_balance(s["token"], "https://openapi.qoder.sh/api/v2/quota/usage", camel=True)
    return {"ok": st == 200, "remain": d["remain"], "total": d["total"],
            "used": d["used"], "detail": d["detail"],
            "error": "" if st == 200 else txt[:150]}


def _credits_qwenwork() -> dict[str, Any]:
    s = pm.qwenwork_session()
    st, d, txt = _quota_balance(s["token"], "https://gateway.qwenwork.cn/api/v2/quota/usage", camel=False)
    return {"ok": st == 200, "remain": d["remain"], "total": d["total"],
            "used": d["used"], "detail": d["detail"],
            "error": "" if st == 200 else txt[:150]}


def _credits_trae() -> dict[str, Any]:
    """Trae 余额：/pay/ide_user_ent_usage 的 total_balance（普通 Cloud-IDE-JWT）。"""
    s = pm.traework_session()
    h = {"Accept": "application/json", "Authorization": f"Cloud-IDE-JWT {s['token']}",
         "User-Agent": "TRAE-SOLO-CN/0.1.56"}
    if s.get("device_id"):
        h["x-device-id"] = s["device_id"]
    st, txt = _request("https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage", h)
    d = _json(txt)
    remain = d.get("total_balance")
    detail = f"余额 {remain} · {d.get('usage_summary', {}).get('consumed_amount', '?')} 已用 / {d.get('usage_summary', {}).get('total_amount', '?')} 总额"
    return {"ok": st == 200, "remain": remain, "total": (d.get("usage_summary") or {}).get("total_amount"),
            "used": (d.get("usage_summary") or {}).get("consumed_amount"),
            "detail": detail, "error": "" if st == 200 else txt[:150]}


# ---------------------------------------------------------------- 签到
def checkin(platform: str) -> dict[str, Any]:
    """签到/领积分。幂等：已领取返回 ALREADY、gained=0。"""
    try:
        if platform == "qoder-intl":
            return _checkin_qoder()
        if platform == "xiaohuanxiong":
            return _checkin_xhx()
        if platform == "traework":
            return _checkin_trae()
        if platform in ("qwenwork", "loomy"):
            return {"ok": False, "status": "UNSUPPORTED", "gained": 0,
                    "message": f"{platform} 每日积分由上游自动发放，签到接口待实测"}
    except Exception as e:
        return {"ok": False, "status": "ERROR", "gained": 0, "message": str(e)[:200]}
    return {"ok": False, "status": "UNSUPPORTED", "gained": 0, "message": "未知平台"}


def _checkin_qoder() -> dict[str, Any]:
    """Qoder 活动：GET campaigns → POST claim（活动列表按 UTC+8 刷新）。"""
    s = pm.qoder_session()
    h = {"Accept": "application/json", "Authorization": f"Bearer {s['token']}",
         "Cosy-ClientType": "10", "User-Agent": "Qoder/claim"}
    base = "https://openapi.qoder.sh"
    st, txt = _request(base + "/sash/api/v1/me/campaigns?forceRefresh=true", h)
    payload = _json(txt)
    rows = payload.get("campaigns") or (payload.get("data") if isinstance(payload.get("data"), list) else [])
    claimed = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("claimStatus") or "").upper() == "CLAIMED":
            continue
        cid = row.get("campaignId") or row.get("id")
        if not cid:
            continue
        benefit = row.get("benefit") if isinstance(row.get("benefit"), dict) else {}
        amount = benefit.get("amount") or 0
        st2, txt2 = _request(base + f"/sash/api/v1/me/campaigns/{cid}/claim", h,
                             data=b"{}", method="POST")
        if st2 == 200:
            claimed += float(amount or 0)
    return {"ok": True, "status": "CLAIMED" if claimed else "ALREADY", "gained": claimed,
            "message": f"领取 {claimed} credits" if claimed else "今日已领或无活动"}


def _hours_from_now(hours: int) -> str:
    """UTC 时间字符串，用于按 created_at>= 过滤今日账单。"""
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _checkin_xhx() -> dict[str, Any]:
    """小浣熊：读 bills 判断今日入账；GET setting_info 触发每日发放，再读 bills 比对。"""
    s = pm.xhx_session()
    h = {"Accept": "application/json", "Origin": "https://xiaohuanxiong.com",
         "Referer": "https://xiaohuanxiong.com/", "User-Agent": "office-raccoon/1.0.0",
         "X-Client-Platform": "desktop-windows", "X-Client-Version": "v1.0.0",
         "X-Raccoon-Language": "zh", "Authorization": f"Bearer {s['token']}"}

    def today_points() -> float:
        st, txt = _request("https://xiaohuanxiong.com/api/web/points/v1/bills?paging.limit=50&paging.offset=0", h)
        d = _json(txt)
        data = d.get("data") if isinstance(d.get("data"), (list, dict)) else d
        rows = data if isinstance(data, list) else (
            data.get("bills") or data.get("list") or data.get("items") or [] if isinstance(data, dict) else [])
        today = datetime.now(TZ_CN).date().isoformat()
        total = 0.0
        for r in rows:
            if not isinstance(r, dict) or float(r.get("points") or 0) <= 0:
                continue
            created = str(r.get("created_at") or "")
            if created[:10] == today:
                total += float(r["points"])
        return total

    try:
        before = today_points()
        # 触发每日发放（GET setting_info 是官方客户端触发点）
        st, txt = _request("https://xiaohuanxiong.com/api/web/office/v3/setting_info", h)
        after = today_points()
        if after > before:
            return {"ok": True, "status": "CLAIMED", "gained": after - before, "message": "领取成功"}
        if after > 0:
            return {"ok": True, "status": "ALREADY", "gained": 0, "message": f"今日已领 {after:.0f} 积分"}
        d = _json(txt)
        triggered = bool((d.get("data") or {}).get("point_grant_popups") or (d.get("data") or {}).get("point_grant_toast"))
        return {"ok": True, "status": "CLAIMED" if triggered else "ALREADY", "gained": 0,
                "message": "已触发每日发放" if triggered else "今日发放待到账"}
    except Exception as e:
        return {"ok": False, "status": "ERROR", "gained": 0, "message": str(e)[:200]}


def _checkin_trae() -> dict[str, Any]:
    """Trae：checkin_credits status/claim。"""
    s = pm.traework_session()
    h = {"Content-Type": "application/json", "Authorization": f"Cloud-IDE-JWT {s['token']}",
         "User-Agent": "TRAE-SOLO-CN/0.1.56"}
    if s.get("device_id"):
        h["x-device-id"] = s["device_id"]
    base = "https://api.trae.cn"
    st, txt = _request(base + "/trae/api/v2/ug/checkin_credits/status", h)
    payload = _json(txt)
    # 官方返回是平铺对象：{"checked_in":true,"credits":100,"extra_credits":100}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    credits = float(data.get("credits") or data.get("extra_credits") or 0)
    if data.get("checked_in") or data.get("did_checked_in") or data.get("claimed") or data.get("checkedIn"):
        return {"ok": True, "status": "ALREADY", "gained": 0, "message": f"今日已签到（今日额度 {credits:.0f}）"}
    st2, txt2 = _request(base + "/trae/api/v2/ug/checkin_credits/claim", h,
                         data=b"{}", method="POST")
    if st2 == 200:
        d = _json(txt2)
        d = d.get("data") if isinstance(d.get("data"), dict) else d
        return {"ok": True, "status": "CLAIMED", "gained": float(d.get("credit") or d.get("credits") or credits),
                "message": "签到成功"}
    return {"ok": False, "status": "ERROR", "gained": 0, "message": txt2[:150]}
