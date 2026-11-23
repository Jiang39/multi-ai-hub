#!/usr/bin/env python3
"""最小聊天实测：确认模型是否真的可调用（会消耗微量积分）。

对每个模型发一句 "hi"，max_tokens=1，stream=false，只记录 HTTP 状态与
返回文本/错误码，不打印令牌。这是「目录可见」之外的「实际可用」验证。

用法：
    python probe_chat.py loomy        # 只测 Loomy
    python probe_chat.py xhx          # 只测小浣熊
    python probe_chat.py all          # 测所有已实现的平台
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import probe_models as pm

TIMEOUT = 30
PROMPT = "hi"


def post_json(url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
    headers = {**headers, "Content-Type": "application/json"}
    req = Request(url, headers=headers, data=json.dumps(body).encode("utf-8"), method="POST")
    try:
        with urlopen(req, timeout=TIMEOUT) as res:
            raw = res.read().decode("utf-8", "replace")
            return res.status, _safe_json(raw)
    except HTTPError as e:
        return e.code, _safe_json(e.read().decode("utf-8", "replace"))
    except Exception as e:
        return 0, {"_error": str(e)[:200]}


def _safe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return {"_raw": text[:300]}


def outcome(status: int, data: Any) -> str:
    if status == 200:
        if isinstance(data, dict):
            choices = data.get("choices") or []
            if choices:
                return "OK（有回复）"
            if data.get("error"):
                return f"业务错误：{str(data['error'])[:120]}"
        return "OK（无 choices，需人工看）"
    if isinstance(data, dict):
        msg = data.get("error") or data.get("message") or data.get("msg") or data
        return f"HTTP {status}: {str(msg)[:160]}"
    return f"HTTP {status}"


def test_loomy() -> None:
    """Loomy：标准 OpenAI /v1/chat/completions，模型 id 直接用。"""
    sess = pm.loomy_session()
    token, trace = sess["token"], uuid.uuid4().hex
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "token": token,
        "traceparent": f"00-{trace}-{trace[:16]}-01",
        "loomy-version": "0.9.38",
        "X-Loomy-Request-Purpose": "chat.message",
    }
    models = ["DeepSeek-V4.1-Flash", "deepseek-v4-flash-1017", "GLM-5.3-Flash"]
    print("=== Loomy ===")
    for m in models:
        try:
            st, data = post_json("https://loomyad.xunfei.cn/api/v1/chat/completions", headers,
                                 {"model": m, "messages": [{"role": "user", "content": PROMPT}],
                                  "max_tokens": 1, "stream": False})
            print(f"  {m:28s} {outcome(st, data)}")
        except Exception as e:
            print(f"  {m:28s} 异常：{e}")


def test_xhx() -> None:
    """小浣熊：标准 OpenAI /chat/completions，模型名带 sn- 前缀。"""
    sess = pm.xhx_session()
    headers = {
        "Accept": "application/json",
        "Origin": "https://xiaohuanxiong.com",
        "Referer": "https://xiaohuanxiong.com/",
        "User-Agent": "office-raccoon/1.0.0",
        "X-Client-Platform": "desktop-windows",
        "X-Client-Version": "v1.0.0",
        "X-Raccoon-Language": "zh",
        "Authorization": f"Bearer {sess['token']}",
    }
    models = ["sn-deepseek-v4-1-flash-1017", "sn-glm-5-3", "sn-kimi-k3-1-pro"]
    print("=== 小浣熊 ===")
    for m in models:
        try:
            st, data = post_json("https://xiaohuanxiong.com/api/web/llm/v2/chat/completions", headers,
                                 {"model": m, "messages": [{"role": "user", "content": PROMPT}],
                                  "max_tokens": 1, "stream": False})
            print(f"  {m:28s} {outcome(st, data)}")
        except Exception as e:
            print(f"  {m:28s} 异常：{e}")


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    which = sys.argv[1] if len(sys.argv) > 1 else "loomy"
    print(f"最小聊天实测（max_tokens=1，微量消耗）  "
          f"{datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')}\n")
    if which in ("all", "loomy"):
        try:
            test_loomy()
        except Exception as e:
            print(f"Loomy 失败：{e}")
        print()
    if which in ("all", "xhx"):
        try:
            test_xhx()
        except Exception as e:
            print(f"小浣熊失败：{e}")
        print()
    if which in ("all", "qoder"):
        try:
            test_qoder()
        except Exception as e:
            print(f"Qoder 失败：{e}")
        print()
    if which in ("all", "qwenwork"):
        try:
            test_qwenwork()
        except Exception as e:
            print(f"QwenWork 失败：{e}")
        print()
    if which in ("all", "trae"):
        try:
            test_trae()
        except Exception as e:
            print(f"TraeWork 失败：{e}")
        print()
    return 0



# ---------------------------------------------------------------- Qoder / QwenWork（COSY 签名 + 完整 body）
import base64 as _b64
import hashlib as _hashlib
import time as _time

_STD = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_CUSTOM = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"
_TABLE = {ord(s): ord(d) for s, d in zip(_STD, _CUSTOM)}
_TABLE[ord("=")] = ord("$")


def _encode_qoder(body: bytes) -> bytes:
    t = _b64.b64encode(body)
    n = len(t)
    third = n // 3
    ordered = t[n - third:] + t[third:n - third] + t[:third]
    return bytes(_TABLE.get(x, x) for x in ordered)


def _sse_outcome(status: int, raw: str) -> str:
    if status >= 400:
        return f"HTTP {status}: {raw[:200]}"
    # SSE 流：逐块解析（event: 行 + data: 行），识别各家错误与正文
    current_event = ""
    for line in raw.splitlines():
        if line.startswith("event:"):
            current_event = line[6:].strip()
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
        # Trae：event:error + data:{code,message,extra}
        if current_event == "error" or ev.get("error"):
            err = ev.get("error")
            code = ev.get("code") or (err.get("code") if isinstance(err, dict) else None)
            msg = ev.get("message") or ""
            extra = ev.get("extra") or ""
            return f"业务错误 code={code} {str(msg)[:80]} {str(extra)[:60]}"
        # Qoder/QwenWork：正文包在 body 字段
        inner = ev.get("body")
        if isinstance(inner, dict):
            inner = inner.get("content") or inner.get("body") or inner
        if isinstance(inner, str) and inner.strip():
            return "OK（有回复流）"
        # Trae：event:output + data:{response,reasoning_content}
        if current_event == "output" and (ev.get("response") or ev.get("reasoning_content")):
            return "OK（有回复流）"
    return f"HTTP {status}（无文本，需人工看）"[:200]


def _post_raw(url: str, headers: dict[str, str], data: bytes) -> tuple[int, str]:
    req = Request(url, headers=headers, data=data, method="POST")
    try:
        with urlopen(req, timeout=TIMEOUT) as res:
            return res.status, res.read().decode("utf-8", "replace")
    except HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)[:200]


def _qoder_body(key: str) -> dict[str, Any]:
    rid = uuid.uuid4().hex
    return {
        "request_id": rid, "request_set_id": rid, "chat_record_id": rid,
        "session_id": rid, "stream": True, "chat_task": "FREE_INPUT",
        "is_reply": True, "is_retry": False, "source": 1, "version": "3",
        "session_type": "qodercli", "agent_id": "agent_common", "task_id": "common",
        "system": "", "messages": [{"role": "user", "content": PROMPT}], "tools": [],
        "parameters": {"max_tokens": 1},
        "chat_context": {"text": PROMPT, "features": [], "chatPrompt": "",
                         "extra": {"context": [], "modelConfig": {"key": key, "is_reasoning": False},
                                   "originalContent": PROMPT}},
        "model_config": {"key": key, "source": "system", "format": "openai", "is_reasoning": False},
        "business": {"product": "cli", "version": "1.0.0", "type": "agent", "stage": "start",
                     "id": rid, "name": PROMPT[:30], "begin_at": int(_time.time() * 1000)},
    }


def test_qoder() -> None:
    """Qoder 国际版：COSY 签名 + 自定义表编码 body，key 用 upstream key。"""
    sess = pm.qoder_session()
    url = "https://api3.qoder.sh/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
    models = [("DeepSeek-Flash", "dfmodel"), ("GLM-5.3", "gmodel"),
              ("Kimi-K3.1-Pro", "kmodel_latest")]
    print("=== Qoder 国际版 ===")
    for name, key in models:
        try:
            body = _qoder_body(key)
            raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            encoded = _encode_qoder(raw)
            headers = pm._cosy_auth(sess, url, encoded.decode("ascii"))
            headers["Accept"] = "text/event-stream"
            st, text = _post_raw(url, headers, encoded)
            print(f"  {name:20s} {_sse_outcome(st, text)}")
        except Exception as e:
            print(f"  {name:20s} 异常：{str(e)[:160]}")


def _qwen_body(key: str) -> dict[str, Any]:
    rid = uuid.uuid4().hex
    return {
        "request_id": rid, "request_set_id": rid, "chat_record_id": rid,
        "session_id": rid, "stream": True, "chat_task": "FREE_INPUT",
        "chat_context": {"text": PROMPT, "features": [],
                         "extra": {"context": [],
                                   "modelConfig": {"key": key, "is_reasoning": False},
                                   "originalContent": PROMPT},
                         "chatPrompt": "", "imageUrls": None},
        "is_reply": True, "is_retry": False, "source": 1, "version": "3",
        "agent_id": "agent_common", "task_id": "common", "session_type": "qoder_work",
        "aliyun_user_type": "",
        "model_config": {"key": key, "display_name": key, "model": "", "format": "openai",
                         "is_vl": False, "is_reasoning": False, "api_key": "", "url": "",
                         "source": "system", "max_input_tokens": 1000000},
        "system": "", "messages": [{"role": "user", "content": PROMPT}], "tools": [],
        "parameters": {"max_tokens": 1},
        "business": {"product": "qoder_work", "type": "agent", "version": "1",
                     "feature_switches": {}},
    }


def test_qwenwork() -> None:
    """QwenWork：COSY 签名(body 参与) + 完整 body。"""
    sess = pm.qwenwork_session()
    url = "https://gateway.qwenwork.cn/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common"
    models = ["deepseek-flash", "glm-5.3", "kimi-k3.1-pro"]
    print("=== QwenWork ===")
    for key in models:
        try:
            rid = uuid.uuid4().hex
            body = _qwen_body(key)
            raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            # 用带 body 的 COSY 签名（复用 _cosy_auth，版本 1.1.38 → 这里近似；若失败再调 1.1.32）
            headers = pm._cosy_auth(sess, url, raw)
            headers.update({
                "User-Agent": "qoderwork/1.0.4",
                "X-QwenWork-Version": "1.0.4",
                "X-QwenWork-Release-Version": "1.0.4-26090412",
                "X-QwenWork-Build": "26090412",
                "X-QwenWork-Platform": "win32", "X-QwenWork-Arch": "x64",
                "X-QwenWork-Channel": "stable", "Cosy-Version": "1.1.32",
                "Cosy-ClientType": "6", "Cosy-Business-Product": "qoder_work",
                "Cosy-Business-Type": "agent", "Cosy-Scene": "qwork",
                "Cosy-MachineOS": "x86_64_win32", "Cosy-Data-Policy": "disagree",
                "X-Model-Key": key, "x-model-key": key,
                "Accept": "text/event-stream",
            })
            st, text = _post_raw(url, headers, raw.encode("utf-8"))
            print(f"  {key:20s} {_sse_outcome(st, text)}")
        except Exception as e:
            print(f"  {key:20s} 异常：{str(e)[:160]}")

def _trae_body(model: str) -> dict[str, Any]:
    return {
        "messages": [{"role": "user", "content": [{"type": "text", "text": PROMPT}]}],
        "function": "solo_work_lite",
        "stream": True,
        "config_name": model,
        "model": model,
        "max_tokens": 1,
    }


def test_trae() -> None:
    """TraeWork CN：llm_utils_chat SSE，header 用 Cloud-IDE-JWT。"""
    sess = pm.traework_session()
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": f"Cloud-IDE-JWT {sess['token']}",
        "X-Cloudide-Token": sess["token"],
        "X-Ide-Token": sess["token"],
        "X-App-Id": "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8",
        "X-App-Version": "default",
        "X-Ide-Version": "0.1.56",
        "X-Ide-Version-Code": "20260820",
        "X-App-Version-Code": "20260820",
        "X-Ide-Version-Type": "stable",
        "X-Device-Type": "windows",
        "Request-Traffic-Type": "prod",
        "User-Agent": "TRAE-SOLO-CN/0.1.56",
    }
    if sess.get("device_id"):
        headers["x-device-id"] = sess["device_id"]
    url = "https://trae-api-cn.mchost.guru/api/agent/v3/llm_utils_chat"
    models = ["deepseek-v4.1-flash", "glm-5.3", "kimi-k3.1-pro"]
    print("=== TraeWork 国内版 ===")
    for m in models:
        try:
            st, text = _post_raw(url, headers, json.dumps(_trae_body(m), ensure_ascii=False).encode("utf-8"))
            print(f"  {m:24s} {_sse_outcome(st, text)}")
        except Exception as e:
            print(f"  {m:24s} 异常：{str(e)[:160]}")


if __name__ == "__main__":
    raise SystemExit(main())
