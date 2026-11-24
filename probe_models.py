#!/usr/bin/env python3
"""只读探测三家账号的模型权限：Qoder 国际版 / Loomy / 小浣熊。

只调各家的「模型列表」接口，不发聊天请求、不签到、不写任何状态文件。
令牌从本机登录态即时解密/读取，仅在内存里用于本次请求，绝不打印。

输出示例：
  [Qoder 国际版] uid=xxxx  HTTP 200  12 个模型
    deepseek: DeepSeek-V4-Pro, DeepSeek-Flash   ✓
    glm:      GLM-5.3                          ✓
    kimi:     Kimi-K3                          ✓

用法：python probe_models.py [qoder|loomy|xhx]（不传则三家都查）
"""

from __future__ import annotations

import base64
import ctypes
import glob
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent

TARGETS = ("deepseek", "glm", "kimi")
TIMEOUT = 20

# ----------------------------------------------------------------- DPAPI（Qoder 登录态解密）
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


class _AeadInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong), ("dwInfoVersion", ctypes.c_ulong),
        ("pbNonce", ctypes.POINTER(ctypes.c_ubyte)), ("cbNonce", ctypes.c_ulong),
        ("pbAuthData", ctypes.POINTER(ctypes.c_ubyte)), ("cbAuthData", ctypes.c_ulong),
        ("pbTag", ctypes.POINTER(ctypes.c_ubyte)), ("cbTag", ctypes.c_ulong),
        ("pbMacContext", ctypes.POINTER(ctypes.c_ubyte)), ("cbMacContext", ctypes.c_ulong),
        ("cbAAD", ctypes.c_ulong), ("cbData", ctypes.c_ulonglong), ("dwFlags", ctypes.c_ulong),
    ]


def _dpapi_unprotect(data: bytes) -> bytes:
    src = _DataBlob(len(data), ctypes.create_string_buffer(data, len(data)))
    dst = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(src), None, None, None, None, 0x01, ctypes.byref(dst)
    ):
        raise RuntimeError("DPAPI 解不开密钥（需同一 Windows 用户桌面会话）")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def _decrypt_gcm_cng(key: bytes, nonce: bytes, blob: bytes) -> bytes:
    bcrypt = ctypes.WinDLL("bcrypt.dll")
    ct, tag = blob[:-16], blob[-16:]

    def chk(res: int, what: str) -> None:
        if res != 0:
            raise RuntimeError(f"CNG {what} 失败 NTSTATUS=0x{res & 0xFFFFFFFF:08x}")

    h_alg = ctypes.c_void_p()
    h_key = ctypes.c_void_p()
    try:
        chk(bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0), "OpenAlg")
        mode = "ChainingModeGCM"
        chk(bcrypt.BCryptSetProperty(h_alg, "ChainingMode", mode, (len(mode) + 1) * 2, 0), "SetProperty")
        kbuf = ctypes.create_string_buffer(key, len(key))
        chk(bcrypt.BCryptGenerateSymmetricKey(h_alg, ctypes.byref(h_key), None, 0, kbuf, len(key), 0), "GenKey")
        nbuf = (ctypes.c_ubyte * len(nonce)).from_buffer_copy(nonce)
        tbuf = (ctypes.c_ubyte * len(tag)).from_buffer_copy(tag)
        info = _AeadInfo()
        info.cbSize = ctypes.sizeof(_AeadInfo)
        info.dwInfoVersion = 1
        info.pbNonce = ctypes.cast(nbuf, ctypes.POINTER(ctypes.c_ubyte))
        info.cbNonce = len(nonce)
        info.pbTag = ctypes.cast(tbuf, ctypes.POINTER(ctypes.c_ubyte))
        info.cbTag = len(tag)
        ibuf = ctypes.create_string_buffer(ct, len(ct))
        obuf = ctypes.create_string_buffer(len(ct))
        done = ctypes.c_ulong()
        chk(bcrypt.BCryptDecrypt(h_key, ibuf, len(ct), ctypes.byref(info), None, 0,
                                 obuf, len(ct), ctypes.byref(done), 0), "Decrypt")
        return obuf.raw[:done.value]
    finally:
        if h_key:
            bcrypt.BCryptDestroyKey(h_key)
        if h_alg:
            bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)


def qoder_session() -> dict[str, Any]:
    appdata = Path(os.environ.get("APPDATA", ""))
    roots = sorted(glob.glob(str(appdata / "com.qoder.app.stable*")), key=os.path.getmtime, reverse=True)
    if not roots:
        raise RuntimeError("未找到 Qoder 国际版登录目录 com.qoder.app.stable")
    root = Path(roots[0])
    raw = (root / "auth.v1.dat").read_bytes()
    if raw[:3] != b"v10":
        raise RuntimeError("auth.v1.dat 格式未知（不是 v10 信封）")
    state = json.loads((root / "Local State").read_text(encoding="utf-8"))
    crypt = state.get("os_crypt")
    if not isinstance(crypt, dict):
        raise RuntimeError("Local State 缺少 os_crypt")
    key_blob = base64.b64decode(crypt.get("encrypted_key") or "")
    if key_blob[:5] != b"DPAPI":
        raise RuntimeError("os_crypt.encrypted_key 结构变了")
    key = _dpapi_unprotect(key_blob[5:])
    plain = _decrypt_gcm_cng(key, raw[3:15], raw[15:])
    sess = json.loads(plain.decode("utf-8"))
    if not sess.get("token"):
        raise RuntimeError("解密成功但没有 token")
    user = sess.get("user") if isinstance(sess.get("user"), dict) else {}
    machine_path = root / "auth.machine-id"
    machine_id = machine_path.read_text(encoding="utf-8").strip() if machine_path.exists() else ""
    return {
        "token": sess["token"],
        "uid": str(user.get("id") or sess.get("uid") or sess.get("userId") or ""),
        "name": str(user.get("name") or sess.get("nickname") or sess.get("name") or ""),
        "email": str(user.get("email") or ""),
        "machine_id": machine_id,
        "root": str(root),
    }


def loomy_session() -> dict[str, Any]:
    base = Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Loomy"
    files = sorted(base.glob("*/userData/auth-session.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise RuntimeError("未找到 Loomy auth-session.json")
    doc = json.loads(files[0].read_text(encoding="utf-8-sig"))
    session = str(doc.get("session") or "")
    if not session:
        raise RuntimeError("Loomy session 为空")
    return {
        "token": session,
        "uid": str(doc.get("userid") or ""),
        "name": str(doc.get("phone") or doc.get("userid") or "Loomy"),
        "source": str(files[0]),
    }


def xhx_session() -> dict[str, Any]:
    p = Path.home() / ".office-raccoon" / "agent" / "config" / "auth.json"
    if not p.exists():
        raise RuntimeError("未找到小浣熊 auth.json")
    doc = json.loads(p.read_text(encoding="utf-8-sig"))
    token = str(doc.get("access_token") or doc.get("accessToken") or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:]
    if not token:
        raise RuntimeError("小浣熊 access_token 为空")
    return {
        "token": token,
        "uid": str(doc.get("office_identity") or ""),
        "name": str(doc.get("name") or doc.get("nickname") or "小浣熊"),
        "source": str(p),
    }


# ----------------------------------------------------------------- HTTP GET 工具
def http_get(url: str, headers: dict[str, str], label: str) -> tuple[int, Any]:
    req = Request(url, headers=headers, method="GET")
    try:
        with urlopen(req, timeout=TIMEOUT) as res:
            body = res.read()
            return res.status, json.loads(body.decode("utf-8", "replace"))
    except HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return e.code, {"_raw": body[:300]}
    except (URLError, ValueError) as e:
        raise RuntimeError(f"{label} 请求失败: {e}") from e


# ----------------------------------------------------------------- Qoder COSY 签名（按 CN 版通用算法尝试）
QODER_RSA_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----"""


def _cosy_auth(sess: dict[str, Any], url: str, body: str = "") -> dict[str, str]:
    """按官方 COSY v1 构造只读请求头：AES-CBC 加密身份 + RSA 加密密钥 + MD5 签名。"""
    import base64 as b64
    import hashlib
    import time
    from urllib.parse import urlparse

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    aes_key = uuid.uuid4().hex[:16]
    payload = {
        "uid": sess["uid"], "aid": "", "name": sess["name"],
        "email": sess["email"], "security_oauth_token": sess["token"],
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    padder = PKCS7(128).padder()
    padded = padder.update(raw) + padder.finalize()
    enc = Cipher(algorithms.AES(aes_key.encode("utf-8")), modes.CBC(aes_key[:16].encode("utf-8"))).encryptor()
    info = b64.b64encode(enc.update(padded) + enc.finalize()).decode("ascii")
    pub = serialization.load_pem_public_key(QODER_RSA_PEM.encode("ascii"))
    key_enc = b64.b64encode(pub.encrypt(aes_key.encode("utf-8"), padding.PKCS1v15())).decode("ascii")

    header = {
        "version": "v1", "requestId": uuid.uuid4().hex, "info": info,
        "cosyVersion": "1.1.38", "ideVersion": "",
    }
    encoded = b64.b64encode(json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode()).decode("ascii")
    path = urlparse(url).path or url
    if "?" in path:
        path = path.split("?", 1)[0]
    if path.startswith("/algo"):
        path = path[5:]
    timestamp = int(time.time())
    sign_str = f"{encoded}\n{key_enc}\n{timestamp}\n{body}\n{path}"
    digest = hashlib.md5(sign_str.encode()).hexdigest()
    machine = sess.get("machine_id") or "buddy2api"
    trace = uuid.uuid4().hex
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer COSY.{encoded}.{digest}",
        "Cosy-Key": key_enc,
        "Cosy-User": sess["uid"],
        "Cosy-Date": str(timestamp),
        "Cosy-Version": "1.1.38",
        "Cosy-Machineid": machine,
        "Cosy-Machinetoken": machine,
        "Cosy-Machinetype": "5",
        "Cosy-Machineos": "x86_64_win32",
        "Cosy-Clienttype": "5",
        "Cosy-Clientip": "127.0.0.1",
        "Cosy-Sigpath": path,
        "Cosy-Data-Policy": "disagree",
        "Login-Version": "v2",
        "X-Request-Id": header["requestId"],
        "User-Agent": "Qoder",
    }


def qoder_headers(sess: dict[str, Any], url: str) -> dict[str, str]:
    return _cosy_auth(sess, url)


# ----------------------------------------------------------------- QwenWork
def qwenwork_session() -> dict[str, Any]:
    import glob as _glob
    appdata = Path(os.environ.get("APPDATA", ""))
    candidates = sorted(_glob.glob(str(appdata / "QwenWorkCN*")))
    if not candidates:
        raise RuntimeError(f"未找到 QwenWork 登录目录：{appdata}\\QwenWorkCN*（请先在 QwenWork 客户端登录）")
    root = Path(candidates[0])
    key_file = root / "Local State"
    crypt = json.loads(key_file.read_text(encoding="utf-8"))["os_crypt"]
    key = _dpapi_unprotect(base64.b64decode(crypt["encrypted_key"])[5:])
    auth_file = root / "auth-v2.dat"
    raw = auth_file.read_bytes()
    if raw[:3] != b"v10":
        raise RuntimeError("QwenWork auth-v2.dat 格式未知（不是 v10 信封）")
    sess = json.loads(_decrypt_gcm_cng(key, raw[3:15], raw[15:]).decode("utf-8"))
    user = sess.get("user") if isinstance(sess.get("user"), dict) else {}
    token = str(sess.get("token") or "")
    if not token:
        raise RuntimeError("QwenWork 会话里没有 token")
    return {
        "token": token,
        "uid": str(user.get("id") or ""),
        "name": str(user.get("name") or user.get("username") or ""),
        "email": str(user.get("email") or ""),
        "machine_id": str(sess.get("loginDeviceId") or ""),
    }


def qwenwork_headers(sess: dict[str, Any], url: str) -> dict[str, str]:
    headers = _cosy_auth(sess, url)
    # QwenWork 客户端头与 COSY 版本和 Qoder 不同
    req_id = headers.get("X-Request-Id", uuid.uuid4().hex)
    headers.update({
        "User-Agent": "qoderwork/1.0.4",
        "X-QwenWork-Version": "1.0.4",
        "X-QwenWork-Release-Version": "1.0.4-26090412",
        "X-QwenWork-Build": "26090412",
        "X-QwenWork-Platform": "win32",
        "X-QwenWork-Arch": "x64",
        "X-QwenWork-Channel": "stable",
        "Cosy-Version": "1.1.32",
        "Cosy-ClientType": "6",
        "Cosy-Business-Product": "qoder_work",
        "Cosy-Business-Type": "agent",
        "Cosy-Scene": "qwork",
        "Cosy-MachineOS": "x86_64_win32",
        "Cosy-Data-Policy": "disagree",
        "X-Model-Key": "pro",
        "X-Model-Source": "system",
        "x-model-key": "pro",
        "x-internal-model-key": "pro",
        "x-model-source": "system",
        "X-Request-Id": req_id,
    })
    # _cosy_auth 里把 1.1.38 写进了 Authorization 和 Cosy-Date 的签名串，
    # QwenWork 用 1.1.32；这里重算一遍签名（复用逻辑，改版本）
    return _qwen_cosy(sess, url, req_id, headers)


def _qwen_cosy(sess: dict[str, Any], url: str, req_id: str, base: dict[str, str]) -> dict[str, str]:
    """QwenWork 用 1.1.32 版本的 COSY 签名，复用与 Qoder 相同的 AES+RSA+MD5 算法。"""
    import base64 as b64
    import hashlib
    import time
    from urllib.parse import urlparse

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    aes_key = uuid.uuid4().hex[:16]
    payload = {
        "uid": sess["uid"], "aid": "", "name": sess["name"],
        "email": sess["email"], "security_oauth_token": sess["token"],
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    padder = PKCS7(128).padder()
    padded = padder.update(raw) + padder.finalize()
    enc = Cipher(algorithms.AES(aes_key.encode()), modes.CBC(aes_key[:16].encode())).encryptor()
    info = b64.b64encode(enc.update(padded) + enc.finalize()).decode()
    pub = serialization.load_pem_public_key(QODER_RSA_PEM.encode())
    key_enc = b64.b64encode(pub.encrypt(aes_key.encode(), padding.PKCS1v15())).decode()
    header = {"version": "v1", "requestId": req_id, "info": info,
              "cosyVersion": "1.1.32", "ideVersion": "1.0.4"}
    encoded = b64.b64encode(json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode()).decode()
    path = urlparse(url).path.split("?", 1)[0]
    timestamp = int(time.time())
    sign_str = f"{encoded}\n{key_enc}\n{timestamp}\n\n{path}"
    digest = hashlib.md5(sign_str.encode()).hexdigest()
    base.update({
        "Authorization": f"Bearer COSY.{encoded}.{digest}",
        "Cosy-Key": key_enc,
        "Cosy-User": sess["uid"],
        "Cosy-Date": str(timestamp),
        "Cosy-Sigpath": path,
        "Cosy-Machineid": sess.get("machine_id") or "",
        "Cosy-Machinetoken": sess.get("machine_id") or "",
        "Cosy-Machinetype": "5",
        "Login-Version": "v2",
    })
    return base


def parse_models(payload: Any, platform: str) -> list[str]:
    """按平台取模型名。探测目的是核对 deepseek/glm/kimi，优先取展示名
    （含厂商字样）；QwenWork 的展示名是中文档位，才退回 key。
    """
    rows: list[Any] = []
    if platform == "qwenwork":
        # 千问：data.qwork[]（SCENE=qwork），顶层 chat 是空数组，不能误取
        data = payload.get("data") if isinstance(payload, dict) else None
        scene = (data or payload).get("qwork") if isinstance((data or payload), dict) else None
        rows = scene if isinstance(scene, list) else []
    elif platform == "qoder":
        # Qoder：顶层 {"chat": [...]}，enable 是 UI 开关不代表权限
        rows = payload.get("chat", []) if isinstance(payload, dict) else []
    elif platform == "xhx":
        # 小浣熊：data.categories[].models[]
        data = payload.get("data") if isinstance(payload, dict) else None
        cats = data.get("categories", []) if isinstance(data, dict) else []
        for cat in cats:
            if isinstance(cat, dict) and isinstance(cat.get("models"), list):
                rows.extend(cat["models"])
    else:  # loomy：data[]
        data = payload.get("data") if isinstance(payload, dict) else payload
        rows = data if isinstance(data, list) else []

    out: list[str] = []
    for r in rows:
        if isinstance(r, str):
            out.append(r)
            continue
        if not isinstance(r, dict):
            continue
        if platform == "qwenwork":
            if r.get("enable") is False:
                continue
            # 展示名（DeepSeek-V4.1-Flash 等）优先，中文档位名兜底用 key
            name = str(r.get("display_name") or "")
            key = str(r.get("key") or "")
            out.append(name if any(t in name.lower() for t in ("deepseek", "glm", "kimi")) else (key or name))
        elif platform == "qoder":
            name = str(r.get("display_name") or r.get("name") or r.get("key") or "")
            if name:
                out.append(name)
        elif platform == "xhx":
            if r.get("visible") is False:
                continue
            name = str(r.get("name") or r.get("model_name") or "")
            if name:
                out.append(name)
        else:  # loomy
            mid = str(r.get("id") or "")
            if mid:
                out.append(mid)
    return [m for m in out if m]


def report(platform: str, uid: str, status: int, models: list[str], detail: str = "") -> None:
    print(f"[{platform}] uid={uid or '?'}  HTTP {status}  模型 {len(models)} 个{detail}")
    for target in TARGETS:
        hits = [m for m in models if target in m.lower()]
        flag = "✓" if hits else "✗"
        print(f"  {target:9s} {flag} {', '.join(hits) if hits else '未发现'}")


def probe_qoder() -> None:
    sess = qoder_session()
    url = "https://api3.qoder.sh/algo/api/v2/model/list?Encode=1"
    status, payload = http_get(url, qoder_headers(sess, url), "Qoder 模型列表")
    models = parse_models(payload, "qoder")
    hint = ""
    if status != 200:
        raw = payload.get("_raw", "")
        hint = f"  （响应片段：{raw[:120]}）"
    report("Qoder 国际版", sess["uid"], status, models, hint)


def probe_loomy() -> None:
    sess = loomy_session()
    token, trace, span = sess["token"], uuid.uuid4().hex, uuid.uuid4().hex[:16]
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "token": token,
        "traceparent": f"00-{trace}-{span}-01",
        "loomy-version": "0.9.38",
        "X-Loomy-Request-Purpose": "chat.message",
    }
    status, payload = http_get("https://loomyad.xunfei.cn/api/v1/models", headers, "Loomy 模型列表")
    report("Loomy", sess["uid"], status, parse_models(payload, "loomy"))


def probe_xhx() -> None:
    sess = xhx_session()
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
    status, payload = http_get("https://xiaohuanxiong.com/api/web/llm/v2/model_catalog", headers, "小浣熊模型目录")
    report("小浣熊", sess["uid"], status, parse_models(payload, "xhx"))


def probe_qwenwork() -> None:
    sess = qwenwork_session()
    url = "https://gateway.qwenwork.cn/api/v2/model/list"
    status, payload = http_get(url, qwenwork_headers(sess, url), "QwenWork 模型列表")
    models = parse_models(payload, "qwenwork")
    hint = ""
    if status != 200:
        raw = payload.get("_raw", "")
        hint = f"  （响应片段：{raw[:120]}）"
    report("QwenWork", sess["uid"], status, models, hint)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    probes = {
        "qoder": probe_qoder,
        "qwenwork": probe_qwenwork,
        "loomy": probe_loomy,
        "xhx": probe_xhx,
    }
    if which not in ("all", *probes):
        print(f"未知参数：{which}（可选 all/qoder/qwenwork/loomy/xhx）")
        return 2
    print(f"只读模型权限探测  {datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')}  （不发聊天、不签到）\n")
    for name, fn in probes.items():
        if which in ("all", name):
            try:
                fn()
            except Exception as e:
                print(f"[{name}] 探测失败：{e}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# ----------------------------------------------------------------- TraeWork CN
_URE = bytes([82,9,106,213,48,54,165,56,191,64,163,158,129,243,215,251,124,227,57,130,155,47,255,135,52,142,67,68,196,222,233,203,84,123,148,50,166,194,35,61,238,76,149,11,66,250,195,78,8,46,161,102,40,217,36,178,118,91,162,73,109,139,209,37])
_DRE = bytes([31,221,168,51,136,7,199,49,177,18,16,89,39,128,236,95,96,81,127,169,25,181,74,13,45,229,122,159,147,201,156,239,160,224,59,77,174,42,245,176,200,235,187,60,131,83,153,97,23,43,4,126,186,119,214,38,225,105,20,99,85,33,12,125])
_TRAE_SALT = bytes(a ^ b for a, b in zip(_URE, _DRE))
_TRAE_MAGIC = b"tc\x05\x10\x00\x00"


def _trae_decrypt(value: str) -> str:
    import hashlib as _hl
    from cryptography.hazmat.primitives.ciphers import Cipher as _C, algorithms as _A, modes as _M
    from cryptography.hazmat.primitives.padding import PKCS7 as _P
    blob = base64.b64decode(value)
    if not blob.startswith(_TRAE_MAGIC):
        raise RuntimeError("Trae blob 不是 tc AES 格式")
    key_mat = blob[6:38]
    cipher = blob[38:]
    mixed = _hl.sha512(key_mat).digest() + _TRAE_SALT
    derived = _hl.sha512(mixed).digest()
    aes_key, iv = derived[:16], derived[16:32]
    dec = _C(_A.AES(aes_key), _M.CBC(iv)).decryptor()
    padded = dec.update(cipher) + dec.finalize()
    _unpadder = _P(128).unpadder()
    plain = _unpadder.update(padded) + _unpadder.finalize()
    checksum, body = plain[:64], plain[64:]
    if _hl.sha512(body).digest() != checksum:
        raise RuntimeError("Trae blob checksum 失败")
    return body.decode("utf-8")


def traework_session() -> dict[str, Any]:
    p = Path(os.environ.get("APPDATA", "")) / "Trae CN" / "User" / "globalStorage" / "storage.json"
    if not p.exists():
        p = Path(os.environ.get("APPDATA", "")) / "TRAE SOLO CN" / "User" / "globalStorage" / "storage.json"
    if not p.exists():
        raise RuntimeError("未找到 Trae storage.json")
    store = json.loads(p.read_text(encoding="utf-8"))
    doc = json.loads(_trae_decrypt(store["iCubeAuthInfo://icube.cloudide"]))
    account = doc.get("account") if isinstance(doc.get("account"), dict) else {}
    token = str(doc.get("token") or doc.get("access_token") or "")
    if not token:
        raise RuntimeError("Trae 会话里没有 token")
    device_id = ""
    for k, v in store.items():
        if str(k).startswith("iCubeAuthInfo://icube-dc:"):
            device_id = str(k).split("icube-dc:", 1)[1]
            break
    return {
        "token": token,
        "uid": str(doc.get("userId") or account.get("userId") or ""),
        "name": str(account.get("username") or doc.get("username") or "Trae"),
        "device_id": device_id,
        "host": str(doc.get("host") or "https://api.trae.cn"),
    }
