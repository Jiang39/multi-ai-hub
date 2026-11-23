"""WorkBuddy 原生协议层（从 8789 TypeScript 网关移植到 Python）。

让 8899 网关不再透传 8789，而是直接：
- 读 pool-*.info 账号池（sticky 路由 / 429 账号级 + 模型级冷却 / token 刷新）
- Anthropic /v1/messages 请求 → WorkBuddy 上游 OpenAI body（system 身份清洗）
- 上游 SSE → Anthropic 事件流（有状态翻译器，支持 tool_calls）

设计：纯标准库（cryptography 仅 token 刷新时按需），不打印令牌。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
POOL_DIR = ROOT / "runtime"
GW_CONFIG = ROOT / "config" / "workbuddy-gateway.json"
RL_FILE = POOL_DIR / "ratelimit.json"
RESCAN_TTL_MS = 60_000

# from anthropic.ts
EFFORT_MAP = {"low": "low", "medium": "high", "high": "high", "xhigh": "high", "max": "max"}
DEFAULT_MAX_TOKENS = 8192


def _load_cfg() -> dict[str, Any]:
    try:
        return json.loads(GW_CONFIG.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def now_ms() -> int:
    return int(time.time() * 1000)


def _signin_name(uid: str) -> str:
    """从签到快照读 uid→中文名（主号/新号/三号/四号），读不到返回空。"""
    snap = ROOT / "runtime" / "signin" / "accounts.json"
    try:
        d = json.loads(snap.read_text(encoding="utf-8-sig"))
        for a in d.get("accounts") or []:
            if str(a.get("uid")) == uid and a.get("name"):
                return str(a["name"])
    except (OSError, ValueError):
        pass
    return ""


# ---------------------------------------------------------------- 账号池
@dataclass
class Credential:
    uid: str
    uin: str
    nickname: str
    access_token: str
    refresh_token: str | None
    expires_at: int | None
    refresh_expires_at: int | None
    domain: str | None
    enterprise_id: str | None


class AccountPool:
    def __init__(self) -> None:
        self.credentials: list[Credential] = []
        self.cooldowns: dict[str, int] = {}
        self.refreshed: dict[str, str] = {}
        self.cursor = 0
        self.scanned_at = 0
        self.lock = threading.RLock()

    def _scan(self, force: bool = False) -> None:
        if not force and now_ms() - self.scanned_at < RESCAN_TTL_MS:
            return
        cfg = _load_cfg()
        files = []
        for f in (cfg.get("accounts") or {}).get("extraFiles") or []:
            p = Path(str(f))
            files.append(str(p if p.is_absolute() else ROOT / p))
        for d in (cfg.get("accounts") or {}).get("dirs") or []:
            p = Path(str(d).replace("%APPDATA%", str(Path.home() / "AppData" / "Roaming"))
                     .replace("%LOCALAPPDATA%", str(Path.home() / "AppData" / "Local")))
            if p.exists():
                files.extend(str(x) for x in p.glob("*.info"))
        by_uid: dict[str, Credential] = {}
        for f in files:
            try:
                data = json.loads(Path(f).read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            auth = data.get("auth") or {}
            acct = data.get("account") or {}
            token = auth.get("accessToken")
            if isinstance(token, dict) or not isinstance(token, str) or not token.strip():
                continue  # $wbEncrypted 加密包装不可用
            uid = str(acct.get("uid") or auth.get("uid") or "")
            if not uid:
                continue
            exp = auth.get("expiresAt")
            nickname = acct.get("nickname")
            # pool 里 nickname 常是 $wbEncrypted 信封，不可读；用签到快照的 uid→中文名
            if isinstance(nickname, dict) or (isinstance(nickname, str) and "wbEncrypted" in nickname):
                nickname = _signin_name(uid)
            cred = Credential(
                uid=uid, uin=str(acct.get("uin") or ""),
                nickname=str(nickname or ""),
                access_token=token.strip(),
                refresh_token=auth.get("refreshToken") if isinstance(auth.get("refreshToken"), str) else None,
                expires_at=int(exp) if isinstance(exp, (int, float)) else None,
                refresh_expires_at=int(auth["refreshExpiresAt"]) if isinstance(auth.get("refreshExpiresAt"), (int, float)) else None,
                domain=auth.get("domain"), enterprise_id=acct.get("enterpriseId"),
            )
            if cred.refresh_expires_at and cred.refresh_expires_at < now_ms():
                continue
            old = by_uid.get(uid)
            if not old or self._score(cred) > self._score(old):
                by_uid[uid] = cred
        self.credentials = list(by_uid.values())
        self.scanned_at = now_ms()

    @staticmethod
    def _score(c: Credential) -> float:
        return (1e15 if c.refresh_token else 0) + (c.expires_at or 0)

    def is_cooling(self, uid: str) -> bool:
        until = self.cooldowns.get(uid)
        if until is None:
            return False
        if now_ms() >= until:
            self.cooldowns.pop(uid, None)
            return False
        return True

    def mark_limited(self, uid: str, until_ms: int | None = None, ms: int = 60_000) -> None:
        cap = now_ms() + 24 * 3600_000
        until = min(max(until_ms if until_ms is not None else now_ms() + max(1000, ms), now_ms() + 1000), cap)
        self.cooldowns[uid] = max(until, self.cooldowns.get(uid, 0))

    def token_for(self, c: Credential) -> str:
        return self.refreshed.get(c.uid, c.access_token)

    def pick(self, preferred: str | None = None, session_key: str | None = None) -> Credential | None:
        with self.lock:
            self._scan()
            lst = self.credentials
            if not lst:
                return None
            cfg = _load_cfg()
            if preferred:
                needle = preferred.strip().lower()
                for c in lst:
                    if c.uid.lower() == needle or c.uin.lower() == needle or c.nickname.lower() == needle:
                        return c
            if cfg.get("stickyRouting") and session_key:
                want = lst[int.from_bytes(hashlib.sha256(session_key.encode()).digest()[:8], "big") % len(lst)]
                if not self.is_cooling(want.uid):
                    return want
                start = lst.index(want)
                for i in range(1, len(lst)):
                    cand = lst[(start + i) % len(lst)]
                    if not self.is_cooling(cand.uid):
                        return cand
                return want
            for _ in range(len(lst)):
                cand = lst[self.cursor % len(lst)]
                self.cursor = (self.cursor + 1) % len(lst)
                if not self.is_cooling(cand.uid):
                    return cand
            return lst[self.cursor % len(lst)]

    @staticmethod
    def label(c: Credential) -> str:
        return c.nickname or c.uin or c.uid[:8]

    def refresh(self, c: Credential) -> Credential | None:
        """token 刷新（本进程内覆盖层）；token 还有 26+ 天，极少触发。"""
        if not c.refresh_token:
            return None
        cfg = _load_cfg()
        up = cfg.get("upstream") or {}
        url = str(up.get("baseUrl", "")).rstrip("/") + str(up.get("refreshPath", ""))
        try:
            req = Request(url, data=b"", method="POST", headers={
                "content-type": "application/json",
                "x-refresh-token": c.refresh_token,
                "x-auth-refresh-source": "plugin",
            })
            with urlopen(req, timeout=30) as res:
                d = json.loads(res.read().decode("utf-8", "replace"))
        except Exception:
            return None
        token = d.get("accessToken") or d.get("access_token")
        if not token:
            return None
        self.refreshed[c.uid] = token
        return Credential(**{**c.__dict__, "access_token": token,
                             "refresh_token": d.get("refreshToken", c.refresh_token)})


POOL = AccountPool()


# ---------------------------------------------------------------- 模型级冷却
class ModelCooldowns:
    def __init__(self) -> None:
        self.items: dict[str, int] = {}
        self.lock = threading.RLock()

    @staticmethod
    def key(uin: str, model: str) -> str:
        return f"{uin}|{model}"

    def mark(self, uin: str, model: str, until_ms: int) -> None:
        with self.lock:
            k = self.key(uin, model)
            self.items[k] = max(until_ms, self.items.get(k, 0))
            self.write()

    def clear(self, uin: str, model: str) -> None:
        with self.lock:
            if self.items.pop(self.key(uin, model), None) is not None:
                self.write()

    def limited(self, uin: str, model: str) -> int:
        return self.items.get(self.key(uin, model), 0) if now_ms() < self.items.get(self.key(uin, model), 0) else 0

    def write(self) -> None:
        try:
            accounts = {c.uin or c.uid: {"until": u, "local": _local(u)} for uid_c, u in () for c in ()}
            # 账号级冷却
            accounts = {}
            for uid, until in POOL.cooldowns.items():
                c = next((x for x in POOL.credentials if x.uid == uid), None)
                key = (c.uin or c.uid) if c else uid
                accounts[key] = {"until": until, "local": _local(until)}
            models = {k: {"until": u, "local": _local(u)} for k, u in self.items.items() if u > now_ms()}
            RL_FILE.parent.mkdir(parents=True, exist_ok=True)
            RL_FILE.write_text(json.dumps({"accounts": accounts, "models": models},
                                          ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass


MODEL_COOLDOWNS = ModelCooldowns()


def _local(ms: int) -> str:
    from datetime import datetime, timedelta, timezone
    return datetime.fromtimestamp(ms / 1000, timezone(timedelta(hours=8))).strftime("%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 请求翻译
def _as_record(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _strip_billing(text: str) -> str:
    return re.sub(r"^x-anthropic-billing-header:.*$", "", text, flags=re.M)


def sanitize_system_identity(text: str, real_model: str = "") -> str:
    alias = (f" You are {real_model}, a large language model. If the user asks which model or version you are, "
             f"answer truthfully with exactly: {real_model}. Never claim to be a Claude model or made by Anthropic."
             if real_model else "")
    out = text
    out = out.replace("You are Claude Code, Anthropic's official CLI for Claude, running within the Claude Agent SDK.",
                      f"You are an AI coding assistant.{alias}")
    out = out.replace("You are a Claude agent, built on Anthropic's Claude Agent SDK.",
                      f"You are an AI coding assistant.{alias}")
    out = out.replace("Anthropic's official CLI for Claude", "an AI coding assistant")
    out = out.replace("the Claude Agent SDK", "an agent runtime")
    out = out.replace("anthropics/claude-code", "open-source/assistant-cli")
    out = out.replace("You are an AI agent powered by DeepSeek Harness.", "You are an AI agent.")
    if re.search(r"Claude Code", out, re.I) and re.search(r"Anthropic", out, re.I):
        out = re.sub(r"Claude Code", "this assistant", out)
    return out


def resolve_model(requested: str, cfg: dict[str, Any]) -> str:
    mp = (cfg.get("models") or {}).get("map") or {}
    if requested in mp:
        return str(mp[requested])
    best = ""
    for k in mp:
        if requested.lower().startswith(str(k).lower()) and len(k) > len(best):
            best = k
    if best:
        return str(mp[best])
    if re.match(r"^claude", requested, re.I):
        return str((cfg.get("models") or {}).get("default") or "deepseek-v4.1-flash")
    return requested


def model_candidates(requested: str, cfg: dict[str, Any]) -> list[str]:
    primary = resolve_model(requested, cfg)
    out = [primary]
    if not cfg.get("modelFallback"):
        return out
    for m in (cfg.get("models") or {}).get("fallbackOrder") or []:
        if m and m not in out:
            out.append(m)
    default = (cfg.get("models") or {}).get("default")
    if default and default not in out:
        out.append(default)
    return out


def resolve_effort(req: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    asked = (req.get("output_config") or {}).get("effort")
    if not asked and (req.get("thinking") or {}).get("type") == "disabled":
        asked = "low"
    chosen = cfg.get("reasoningEffort") if cfg.get("forceReasoningEffort") else (asked or cfg.get("reasoningEffort"))
    return EFFORT_MAP.get(str(chosen or "").lower())


def _system_messages(system: Any, real_model: str) -> list[dict[str, Any]]:
    if isinstance(system, str):
        text = system
    elif isinstance(system, list):
        text = "\n\n".join(b.get("text", "") for b in system if _as_record(b).get("type") == "text")
    else:
        text = ""
    text = sanitize_system_identity(_strip_billing(text), real_model)
    return [{"role": "system", "content": text}] if text.strip() else []


def _image_part(block: dict[str, Any]) -> dict[str, Any] | None:
    src = _as_record(block.get("source"))
    if src.get("type") == "base64" and isinstance(src.get("data"), str):
        media = src.get("media_type") or "image/png"
        return {"type": "image_url", "image_url": {"url": f"data:{media};base64,{src['data']}"}}
    if src.get("type") == "url" and isinstance(src.get("url"), str):
        return {"type": "image_url", "image_url": {"url": src["url"]}}
    return None


def _tool_result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(x if isinstance(x, str) else (_as_record(x).get("text") or "")
                         for x in content if isinstance(x, str) or _as_record(x).get("type") == "text")
    elif content is not None:
        text = json.dumps(content, ensure_ascii=False)
    else:
        text = ""
    return f"Error: {text}" if block.get("is_error") is True else text


def _user_messages(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    if not isinstance(content, list):
        return [{"role": "user", "content": ""}]
    tools: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []
    for raw in content:
        b = _as_record(raw)
        if b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str):
            tools.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": _tool_result_text(b)})
        elif b.get("type") == "text" and isinstance(b.get("text"), str):
            parts.append({"type": "text", "text": b["text"]})
        elif b.get("type") == "image":
            part = _image_part(b)
            if part:
                parts.append(part)
    out = list(tools)
    if parts:
        if all(p["type"] == "text" for p in parts):
            out.append({"role": "user", "content": "\n".join(str(p["text"]) for p in parts)})
        else:
            out.append({"role": "user", "content": parts})
    return out or [{"role": "user", "content": ""}]


def _assistant_message(content: Any) -> dict[str, Any]:
    if isinstance(content, str):
        return {"role": "assistant", "content": content}
    texts: list[str] = []
    calls: list[dict[str, Any]] = []
    for raw in content if isinstance(content, list) else []:
        b = _as_record(raw)
        if b.get("type") == "text" and isinstance(b.get("text"), str):
            texts.append(b["text"])
        elif b.get("type") == "tool_use" and isinstance(b.get("name"), str):
            calls.append({"id": b.get("id") or f"toolu_{len(calls)}", "type": "function",
                          "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}})
    msg: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) or None}
    if calls:
        msg["tool_calls"] = calls
    return msg


def build_upstream_body(req: dict[str, Any], cfg: dict[str, Any], model: str) -> dict[str, Any]:
    messages = _system_messages(req.get("system"), model)
    for m in req.get("messages") or []:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant":
            messages.append(_assistant_message(m.get("content")))
        else:
            messages.extend(_user_messages(m.get("content")))
    requested_max = req.get("max_tokens") if isinstance(req.get("max_tokens"), int) else DEFAULT_MAX_TOKENS
    body: dict[str, Any] = {
        "model": model, "messages": messages, "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": max(requested_max, int(cfg.get("minMaxTokens") or 0)),
    }
    effort = resolve_effort(req, cfg)
    if effort:
        body["reasoning_effort"] = effort
    if isinstance(req.get("temperature"), (int, float)):
        body["temperature"] = req["temperature"]
    if isinstance(req.get("top_p"), (int, float)):
        body["top_p"] = req["top_p"]
    if isinstance(req.get("stop_sequences"), list) and req["stop_sequences"]:
        body["stop"] = req["stop_sequences"]
    if (req.get("tool_choice") or {}).get("type") != "none":
        tools = [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description") or "",
            "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
            for t in (req.get("tools") or []) if isinstance(t, dict) and isinstance(t.get("name"), str)]
        if tools:
            body["tools"] = tools
            choice = (req.get("tool_choice") or {}).get("type")
            if choice == "any":
                body["tool_choice"] = "required"
            elif choice == "tool":
                name = (req.get("tool_choice") or {}).get("name")
                body["tool_choice"] = {"type": "function", "function": {"name": name}} if name else "auto"
            elif choice == "auto":
                body["tool_choice"] = "auto"
    return body


def session_key(req: dict[str, Any]) -> str | None:
    meta = req.get("metadata")
    if isinstance(meta, dict) and isinstance(meta.get("user_id"), str) and meta["user_id"].strip():
        return "uid:" + meta["user_id"].strip()
    msgs = req.get("messages")
    first = msgs[0] if isinstance(msgs, list) and msgs else None
    if not isinstance(first, dict):
        return None
    c = first.get("content")
    text = c if isinstance(c, str) else "\n".join(
        str(_as_record(b).get("text") or "") for b in c if isinstance(c, list)) if isinstance(c, list) else ""
    return "msg:" + text.strip()[:4096] if text.strip() else None


# ---------------------------------------------------------------- 上游调用
def _upstream_headers(cred: Credential, token: str) -> dict[str, str]:
    h = {"authorization": f"Bearer {token}", "content-type": "application/json",
         "accept": "text/event-stream", "user-agent": "WorkBuddy", "x-user-id": cred.uid}
    if cred.enterprise_id:
        h["x-enterprise-id"] = cred.enterprise_id
        h["x-tenant-id"] = cred.enterprise_id
    if cred.domain:
        h["x-domain"] = cred.domain
    return h


def forward_chat(cfg: dict[str, Any], cred: Credential, token: str, body: dict[str, Any]) -> tuple[int, Any]:
    up = cfg.get("upstream") or {}
    url = str(up.get("baseUrl", "")).rstrip("/") + str(up.get("chatPath", ""))
    req = Request(url, data=json.dumps(body, ensure_ascii=False).encode(),
                  headers=_upstream_headers(cred, token), method="POST")
    try:
        with urlopen(req, timeout=int(up.get("timeoutMs") or 300000) / 1000) as res:
            return res.status, res.read().decode("utf-8", "replace")
    except HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)[:300]


def parse_rate_limit_reset(msg: str) -> tuple[int, str] | None:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})\s*UTC\+8", msg)
    if not m:
        return None
    from datetime import datetime, timezone, timedelta
    try:
        dt = datetime(*[int(x) for x in m.groups()], tzinfo=timezone(timedelta(hours=8)))
        return int(dt.timestamp() * 1000), dt.strftime("%m-%d %H:%M:%S")
    except ValueError:
        return None


# ---------------------------------------------------------------- Anthropic 流式翻译
def sse_events(raw: str) -> list[dict[str, Any]]:
    out = []
    for line in raw.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if s.startswith("data:"):
            d = s[5:].strip()
            if d and d != "[DONE]":
                try:
                    out.append(json.loads(d))
                except ValueError:
                    pass
    return out


def format_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def map_stop(reason: str | None) -> str:
    return {"length": "max_tokens", "tool_calls": "tool_use", "function_call": "tool_use"}.get(reason or "", "end_turn")


class AnthropicTranslator:
    def __init__(self, model: str, input_tokens: int = 0) -> None:
        self.message_id = "msg_" + secrets.token_hex(12)
        self.model = model
        self.input_tokens = input_tokens
        self.started = False
        self.next_index = 0
        self.open_index: int | None = None
        self.open_type: str | None = None
        self.pending: dict[int, dict[str, Any]] = {}
        self.stop_reason: str | None = None
        self.output_tokens = 0
        self.reported_input: int | None = None

    def _started(self, out: list[dict[str, Any]]) -> None:
        if self.started:
            return
        self.started = True
        out.append(("message_start", {"type": "message_start", "message": {
            "id": self.message_id, "type": "message", "role": "assistant", "model": self.model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": self.input_tokens, "output_tokens": 0}}}))

    def _close(self, out: list) -> None:
        if self.open_index is not None:
            out.append(("content_block_stop", {"type": "content_block_stop", "index": self.open_index}))
            self.open_index = self.open_type = None

    def _open(self, out: list, block: dict[str, Any]) -> int:
        idx = self.next_index
        self.next_index += 1
        self.open_index = idx
        self.open_type = str(block.get("type"))
        out.append(("content_block_start", {"type": "content_block_start", "index": idx, "content_block": block}))
        return idx

    def _text(self, out: list, text: str) -> None:
        if self.open_type != "text":
            self._close(out)
            self._open(out, {"type": "text", "text": ""})
        out.append(("content_block_delta", {"type": "content_block_delta", "index": self.open_index,
                                            "delta": {"type": "text_delta", "text": text}}))

    def _open_tool(self, out: list, e: dict[str, Any]) -> None:
        self._close(out)
        idx = self.next_index
        e["blockIndex"] = self._open(out, {"type": "tool_use", "id": e.get("id") or f"toolu_{idx}",
                                            "name": e.get("name") or "", "input": {}})
        if e.get("args"):
            out.append(("content_block_delta", {"type": "content_block_delta", "index": e["blockIndex"],
                                                "delta": {"type": "input_json_delta", "partial_json": e["args"]}}))
            e["args"] = ""

    def _tool_delta(self, out: list, call: dict[str, Any]) -> None:
        key = call.get("index") or 0
        e = self.pending.setdefault(key, {"id": None, "name": None, "args": "", "blockIndex": None})
        if call.get("id"):
            e["id"] = call["id"]
        fn = call.get("function") or {}
        if fn.get("name"):
            e["name"] = (e.get("name") or "") + fn["name"]
        frag = fn.get("arguments") or ""
        if e["blockIndex"] is None:
            e["args"] += frag
            if e.get("name"):
                self._open_tool(out, e)
            return
        if frag:
            out.append(("content_block_delta", {"type": "content_block_delta", "index": e["blockIndex"],
                                                "delta": {"type": "input_json_delta", "partial_json": frag}}))

    def push(self, chunk: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        if isinstance(chunk.get("model"), str) and chunk["model"]:
            self.model = chunk["model"]
        u = chunk.get("usage")
        if isinstance(u, dict):
            if isinstance(u.get("prompt_tokens"), int):
                self.reported_input = u["prompt_tokens"]
            if isinstance(u.get("completion_tokens"), int):
                self.output_tokens = u["completion_tokens"]
        for ch in chunk.get("choices") or []:
            delta = ch.get("delta") or {}
            text = delta.get("content")
            if isinstance(text, str) and text:
                self._started(out)
                self._text(out, text)
            for call in delta.get("tool_calls") or []:
                self._started(out)
                self._tool_delta(out, _as_record(call))
            if ch.get("finish_reason"):
                self.stop_reason = ch["finish_reason"]
        return out

    def finish(self) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        self._started(out)
        for e in self.pending.values():
            if e.get("blockIndex") is None and e.get("name"):
                self._open_tool(out, e)
        self._close(out)
        out.append(("message_delta", {"type": "message_delta",
                                      "delta": {"stop_reason": map_stop(self.stop_reason), "stop_sequence": None},
                                      "usage": {"output_tokens": self.output_tokens,
                                                "input_tokens": self.reported_input or self.input_tokens}}))
        out.append(("message_stop", {"type": "message_stop"}))
        return out


def events_to_message(events: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    message: dict[str, Any] = {}
    blocks: dict[int, dict[str, Any]] = {}
    raw_args: dict[int, str] = {}
    for event, data in events:
        d = data
        if event == "message_start":
            message = dict(d.get("message") or {})
        elif event == "content_block_start":
            blocks[d["index"]] = dict(d.get("content_block") or {})
        elif event == "content_block_delta":
            block = blocks.get(d["index"])
            if not block:
                continue
            delta = d.get("delta") or {}
            if delta.get("type") == "text_delta":
                block["text"] = str(block.get("text") or "") + str(delta.get("text") or "")
            elif delta.get("type") == "input_json_delta":
                raw_args[d["index"]] = raw_args.get(d["index"], "") + str(delta.get("partial_json") or "")
        elif event == "message_delta":
            message["stop_reason"] = (d.get("delta") or {}).get("stop_reason")
            message["stop_sequence"] = (d.get("delta") or {}).get("stop_sequence")
            message["usage"] = {**(message.get("usage") or {}), **(d.get("usage") or {})}
    for i, block in blocks.items():
        if block.get("type") == "tool_use":
            try:
                block["input"] = json.loads(raw_args.get(i) or "{}")
            except ValueError:
                block["input"] = {}
    message["content"] = [blocks[i] for i in sorted(blocks)]
    message.setdefault("type", "message")
    message.setdefault("role", "assistant")
    return message


def estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value or "", ensure_ascii=False)
    wide = sum(1 for c in text if ord(c) > 0x2e80)
    return (wide * 3 + (len(text) - wide)) // 4 + (1 if (wide * 3 + (len(text) - wide)) % 4 else 0)

# ---------------------------------------------------------------- 日志（兼容原面板用量统计）
LOG_FILE = POOL_DIR / "gateway.log"

# 日志轮转/清理：与旧 TS 网关（src/log.ts）和 wb-credits.py 的口径一致。
# 面板统计只读 gateway.log / .1 / .2，所以最多保留这 3 份，不写 .3。
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_KEEP = 3
LOG_CHECK_INTERVAL_SEC = 3600       # 定时检查周期：1 小时
LOG_MAX_AGE_DAYS = 14               # 轮转文件超过 14 天自动删除


class LogManager:
    """gateway.log 按大小轮转 + 定时清理。以 LOG_FILE 为唯一目标。

    - 写日志时检查大小，超过 LOG_MAX_BYTES 就 gateway.log→.1→.2，最多 LOG_KEEP 份
      （wb-credits.py 只读 .log/.1/.2，所以不产生 .3）。
    - 后台守护线程每 LOG_CHECK_INTERVAL_SEC 检查一次：超龄的轮转文件删除；
      若主日志在无写入时已超限（例如别的进程写的），也补一次轮转。
    任何失败都静默降级，不影响网关主流程。
    """

    def __init__(self, file: Path = LOG_FILE, max_bytes: int = LOG_MAX_BYTES,
                 keep: int = LOG_KEEP, interval: int = LOG_CHECK_INTERVAL_SEC,
                 max_age_days: int = LOG_MAX_AGE_DAYS) -> None:
        self.file = file
        self.max_bytes = max_bytes
        self.keep = keep
        self.interval = interval
        self.max_age_days = max_age_days
        self.lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def rotate_if_needed(self) -> None:
        try:
            with self.lock:
                # 在锁内复核大小，避免并发两次调用重复轮转
                if not self.file.exists() or self.file.stat().st_size < self.max_bytes:
                    return
                # 从最老一份往前挪：.2 删除，.1→.2，主→.1
                for i in range(self.keep - 1, 0, -1):
                    src = Path(f"{self.file}.{i}")
                    if i == self.keep - 1:  # 最老一份直接删
                        if src.exists():
                            src.unlink()
                        continue
                    dst = Path(f"{self.file}.{i + 1}")
                    if src.exists():
                        try:
                            src.replace(dst)
                        except OSError:
                            pass
                self.file.replace(f"{self.file}.1")
        except OSError:
            pass

    def clean_expired(self) -> None:
        """删除超过 max_age_days 的轮转文件（绝不删主日志）。"""
        import time as _t
        cutoff = _t.time() - self.max_age_days * 86400
        for i in range(1, self.keep):
            p = Path(f"{self.file}.{i}")
            try:
                if p.exists() and p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass

    def _loop(self) -> None:
        while True:
            time.sleep(self.interval)
            try:
                self.clean_expired()
                self.rotate_if_needed()
            except Exception:
                pass

    def start(self) -> None:
        """幂等启动定时清理线程（daemon，随进程退出）。"""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="log-cleanup", daemon=True)
        self._thread.start()

    def append(self, line: str) -> None:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(time.time()*1000)%1000:03d}Z"
        try:
            self.rotate_if_needed()
            with self.lock:
                with self.file.open("a", encoding="utf-8") as f:
                    f.write(f"[{ts}] {line}\n")
        except OSError:
            pass


LOG_MANAGER = LogManager()


def _log(line: str) -> None:
    LOG_MANAGER.append(line)


def client_status(status: int) -> int:
    if status in (400, 401, 403, 404, 413, 422, 429):
        return status
    return 500


FALLBACK_STATUSES = {400, 403, 404, 413, 422, 429, 500, 502, 503, 504}


def _alias_to_real() -> dict[str, str]:
    """桌面端动态发现的假名 claude-xxx-n → 真实模型（config.json models.curated）。"""
    cfg = _load_cfg()
    out: dict[str, str] = {}
    for item in (cfg.get("models") or {}).get("curated") or []:
        rid = item.get("real") or item.get("id")
        iid = item.get("id")
        if rid and iid:
            out[str(iid)] = str(rid)
            # claude-sonnet-1/2 之类变体剥掉尾部数字
            m = re.fullmatch(r"(.+)-(\d+)", str(iid))
            if m:
                out.setdefault(m.group(1), str(rid))
    return out


def chat_sync(model: str, payload: dict[str, Any],
              preferred: str | None = None) -> dict[str, Any]:
    """OpenAI 格式 payload → WorkBuddy 原生上游 → OpenAI 格式响应。

    供 WorkBuddy 适配器使用，彻底不依赖已停掉的 8789 网关。
    复用本模块的账号池/请求翻译/冷却/换号能力，只把 Anthropic 事件流聚合回
    OpenAI 的 message 结构（工具调用、usage 一并保留）。
    """
    cfg = _load_cfg()
    cred = POOL.pick(preferred, session_key(payload))
    if not cred:
        return {"error": {"message": "本机没有可用的 WorkBuddy 账号", "type": "overloaded_error"}}
    real = resolve_model(model, cfg)
    req: dict[str, Any] = {
        "model": real,
        "system": "",
        "messages": [],
        "max_tokens": payload.get("max_tokens") or DEFAULT_MAX_TOKENS,
    }
    # OpenAI messages → 原生 body（复用 build_upstream_body 的 _system/_user/_assistant 翻译）
    msgs = payload.get("messages") or []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            req["system"] = (req["system"] + "\n" + str(content or "")).strip()
        elif role == "assistant":
            req["messages"].append({"role": "assistant", "content": content})
        elif role == "tool":
            req["messages"].append({"role": "user", "content": "tool result: " + str(content or "")})
        else:
            req["messages"].append({"role": "user", "content": content})
    body = build_upstream_body(req, cfg, real)
    # Anthropic tools → OpenAI function（build_upstream_body 只认 input_schema，这里补一层）
    oa_tools = payload.get("tools") or []
    if oa_tools and "tools" not in body:
        body["tools"] = oa_tools
        if payload.get("tool_choice") is not None:
            body["tool_choice"] = payload["tool_choice"]
    started = now_ms()
    status, res = forward_chat(cfg, cred, POOL.token_for(cred), body)
    if status == 401 and POOL.refresh(cred):
        cred = POOL.refresh(cred)  # type: ignore[assignment]
        status, res = forward_chat(cfg, cred, POOL.token_for(cred), body)
    if status != 200:
        if status == 429:
            reset = parse_rate_limit_reset(res if isinstance(res, str) else "")
            MODEL_COOLDOWNS.mark(cred.uin or cred.uid, real,
                                 reset[0] if reset else now_ms() + int(cfg.get("rateLimitCooldownMs") or 60000))
            POOL.mark_limited(cred.uid, reset[0] if reset else None)
        _log(f"← {real} 失败 HTTP {status}")
        return {"error": {"message": (res if isinstance(res, str) else str(res))[:300],
                          "type": "upstream_error", "status": status}}
    try:
        events = [(e.get("event", ""), e) for e in sse_events(res if isinstance(res, str) else "")]
        amsg = events_to_message(events)
    except Exception as e:
        return {"error": {"message": f"响应解析失败：{str(e)[:150]}", "type": "upstream_error"}}
    blocks = amsg.get("content") or []
    text = "".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    tool_calls = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "tool_use":
            tool_calls.append({"id": str(b.get("id") or ""), "type": "function", "function": {
                "name": str(b.get("name") or ""),
                "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}})
    u = amsg.get("usage") or {}
    inp = int(u.get("input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    _log_usage(real, started, {"prompt": inp, "completion": out})
    return {
        "id": str(amsg.get("id") or "chatcmpl-" + uuid.uuid4().hex[:12]),
        "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if tool_calls else (
                         "length" if amsg.get("stop_reason") == "max_tokens" else "stop")}],
        "usage": {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out},
    }


def handle_messages(req: dict[str, Any], preferred: str | None = None
                    ) -> tuple[int, Any, bool, dict[str, Any]]:
    """原生处理 Anthropic /v1/messages。

    返回 (http_status, payload, is_stream, meta)。
    is_stream=True 时 payload 是生成器，产出 SSE 字符串块。
    """
    cfg = _load_cfg()
    msgs = req.get("messages")
    if not isinstance(msgs, list):
        return 400, {"type": "error", "error": {"type": "invalid_request", "message": "缺少 messages 数组"}}, False, {}

    default_model = str((cfg.get("models") or {}).get("default") or "deepseek-v4.1-flash")
    requested_raw = req.get("model") if isinstance(req.get("model"), str) and req.get("model") else default_model
    requested = re.sub(r"\[1m\]$", "", requested_raw, flags=re.I)
    requested = _alias_to_real().get(requested, requested)
    wants_stream = req.get("stream") is True
    meta: dict[str, Any] = {}

    cred = POOL.pick(preferred, session_key(req))
    if not cred:
        return 503, {"type": "error", "error": {"type": "overloaded_error", "message": "本机没有可用的 WorkBuddy 账号"}}, False, meta
    route = "pin" if preferred else ("sticky" if cfg.get("stickyRouting") else "rotate")
    effort_in = (req.get("output_config") or {}).get("effort") or \
        ("low" if (req.get("thinking") or {}).get("type") == "disabled" else "-")
    effort_out = resolve_effort(req, cfg) or "off"
    _log(f"→ {'流式' if wants_stream else '非流式'} model={requested} account={POOL.label(cred)} route={route} effort={effort_in}→{effort_out}")
    meta["account"] = POOL.label(cred)

    candidates = model_candidates(requested, cfg)
    last_status, last_message, used_model = 0, "", candidates[0]

    for model in candidates:
        used_model = model
        body = build_upstream_body(req, cfg, model)
        started = now_ms()
        status, res = forward_chat(cfg, cred, POOL.token_for(cred), body)
        token_expired = False

        if status == 401:
            token_expired = True
            refreshed = POOL.refresh(cred)
            if refreshed:
                cred = refreshed
                _log(f"   {POOL.label(cred)} 令牌已刷新，重试")
                status, res = forward_chat(cfg, cred, POOL.token_for(cred), body)

        if status == 200:
            MODEL_COOLDOWNS.clear(cred.uin or cred.uid, model)
            meta.update(used_model=model, started=started)
            return _serve_success(res, model, req, wants_stream, meta)

        last_status = status
        last_message = res if isinstance(res, str) else ""
        if status == 429:
            reset = parse_rate_limit_reset(last_message)
            until = reset[0] if reset else now_ms() + int(cfg.get("rateLimitCooldownMs") or 60000)
            MODEL_COOLDOWNS.mark(cred.uin or cred.uid, model, until)
        if cfg.get("stripSystemOnReject") and status == 400 and "11128" in last_message:
            no_sys = {**body, "messages": [m for m in body["messages"] if m.get("role") != "system"]}
            _log(f"   {model} 命中渠道校验(11128)，丢弃 system 重试")
            status, res = forward_chat(cfg, cred, POOL.token_for(cred), no_sys)
            last_status = status
            last_message = res if isinstance(res, str) else ""
            if status == 200:
                MODEL_COOLDOWNS.clear(cred.uin or cred.uid, model)
                meta.update(used_model=model, started=started)
                return _serve_success(res, model, req, wants_stream, meta)

        if token_expired and status == 401:
            return 401, {"type": "error", "error": {"type": "authentication_error",
                        "message": "账号登录态已失效且刷新失败，请在 WorkBuddy 桌面端重新登录"}}, False, meta

        if cfg.get("retryOnRateLimit") and status == 429 and not preferred:
            tried = {cred.uid}
            cur = cred
            while status == 429 and len(tried) < max(len(POOL.credentials), 1):
                reset = parse_rate_limit_reset(last_message)
                if reset:
                    POOL.mark_limited(cur.uid, reset[0])
                else:
                    POOL.mark_limited(cur.uid, ms=int(cfg.get("rateLimitCooldownMs") or 60000))
                MODEL_COOLDOWNS.mark(cur.uin or cur.uid, model,
                                     reset[0] if reset else now_ms() + int(cfg.get("rateLimitCooldownMs") or 60000))
                nxt = POOL.pick(None, session_key(req))
                if not nxt or nxt.uid in tried:
                    break
                _log(f"   {POOL.label(cur)} 限流(429)，换 {POOL.label(nxt)} 重试")
                cur = nxt
                tried.add(nxt.uid)
                attempt_start = now_ms()
                status, res = forward_chat(cfg, cur, POOL.token_for(cur), body)
                last_status = status
                last_message = res if isinstance(res, str) else ""
                if status == 401:
                    refreshed = POOL.refresh(cur)
                    if refreshed:
                        cur = refreshed
                        status, res = forward_chat(cfg, cur, POOL.token_for(cur), body)
                        last_status = status
                        last_message = res if isinstance(res, str) else ""
                if status == 200:
                    MODEL_COOLDOWNS.clear(cur.uin or cur.uid, model)
                    meta.update(used_model=model, started=attempt_start, account=POOL.label(cur))
                    return _serve_success(res, model, req, wants_stream, meta)
            if status == 429:
                return 429, {"type": "error", "error": {"type": "rate_limit_error",
                            "message": "所有账号都在限流中，等一会儿再试"}}, False, meta

        if not cfg.get("modelFallback") or status not in FALLBACK_STATUSES:
            _log(f"   {model} 失败 HTTP {status}，放弃")
            return client_status(status), {"type": "error", "error": {"type": "api_error",
                        "message": (last_message[:500] or f"上游 HTTP {status}")}}, False, meta
        _log(f"   {model} 不可用（HTTP {status}），尝试下一个模型")

    return client_status(last_status), {"type": "error", "error": {"type": "api_error",
                "message": f"所有候选模型均不可用，最后一个错误：{last_message[:300] or '未知'}"}}, False, meta


def _serve_success(upstream: Any, model: str, req: dict[str, Any], wants_stream: bool,
                   meta: dict[str, Any]) -> tuple[int, Any, bool, dict[str, Any]]:
    """把上游 SSE 包装成 Anthropic 流或聚合响应。"""
    input_est = estimate_tokens(req.get("messages")) + estimate_tokens(req.get("system")) + estimate_tokens(req.get("tools"))
    translator = AnthropicTranslator(model, input_est)
    usage: dict[str, Any] = {}

    def events() -> Any:
        """产出 (event, data) 元组，供流式序列化与非流式聚合共用。"""
        for data in sse_events(_read_all(upstream)):
            if isinstance(data.get("usage"), dict):
                u = data["usage"]
                for src, dst in (("prompt_tokens", "prompt"), ("completion_tokens", "completion"),
                                 ("prompt_cache_hit_tokens", "cached"), ("credit", "credit")):
                    if isinstance(u.get(src), (int, float)):
                        usage[dst] = u[src]
            for ev, d in translator.push(data):
                yield ev, d
        for ev, d in translator.finish():
            yield ev, d
        _log_usage(model, meta.get("started", now_ms()), usage)

    if wants_stream:
        return 200, (format_sse(ev, d) for ev, d in events()), True, meta
    return 200, events_to_message(list(events())), False, meta


def _read_all(upstream: Any) -> str:
    if isinstance(upstream, str):
        return upstream
    try:
        return upstream.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _log_usage(model: str, started: int, usage: dict[str, Any]) -> None:
    ms = now_ms() - started
    out = usage.get("completion", "?")
    inp = usage.get("prompt", "?")
    cached = usage.get("cached", 0)
    rate = f" {round(out*1000/ms)}tok/s" if isinstance(out, (int, float)) and ms > 0 else ""
    _log(f"← {model} in={inp} out={out} cache={cached} credit={usage.get('credit', 0)} {ms}ms{rate}")
