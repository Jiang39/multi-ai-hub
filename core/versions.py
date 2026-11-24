"""客户端版本漂移监测。

目的不是"自动跟随版本"——声明性版本头服务端通常宽容，签名协议版本不能乱改——
而是把版本从"藏在代码里"变成"可观测"：status 显示【协议适配版本 vs 本机实际版本】，
升级客户端后能提前发现漂移，配合 hub.py probe 主动验证是否仍可用。

版本来源都是本机安装目录的标准文件（只读，不联网）：
- Qoder: resources/build-manifest.json → productVersion
- Trae:  resources/app/product.json → appVersion
- Loomy: resources/app.asar.unpacked/package.json → version
- QwenWork / 小浣熊: 安装路径不固定，找不到就如实标注"未知"

协议侧适配的版本值集中声明在这里，与 protocols.py 的请求头保持一致；
未来客户端升级导致某个头必须改时，只需改这里的常量 + protocols.py 对应一行。
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path
from typing import Any

from .adapter import read_json

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config" / "versions.json"

# 协议层适配的版本（当前请求头实际使用的值）。COSy 版本参与签名，改动需同步 probe_models。
ADAPTED: dict[str, dict[str, str]] = {
    "qoder-intl": {"client": "0.5.x", "cosy": "1.1.38"},
    "qwenwork": {"client": "1.0.4", "cosy": "1.1.32"},
    "loomy": {"client": "0.9.38"},
    "xiaohuanxiong": {"client": "v1.0.0"},
    "traework": {"client": "0.1.56", "build": "20260820"},
    "workbuddy": {},  # 原生协议层，不依赖声明性版本头
}

# 本机版本文件探测规则：(候选安装根目录 glob, 相对版本文件, 版本字段)
_PROBES: dict[str, list[tuple[str, str, str]]] = {
    "qoder-intl": [
        (r"C:\Program Files\Qoder", "resources/build-manifest.json", "productVersion"),
        (r"%LOCALAPPDATA%\Programs\Qoder", "resources/build-manifest.json", "productVersion"),
        (r"D:\software\Qoder", "resources/build-manifest.json", "productVersion"),
    ],
    "traework": [
        (r"C:\Program Files\Trae CN", "resources/app/product.json", "appVersion"),
        (r"%LOCALAPPDATA%\Programs\Trae CN", "resources/app/product.json", "appVersion"),
        (r"D:\software\Trae CN", "resources/app/product.json", "appVersion"),
    ],
    "loomy": [
        (r"%PUBLIC%\Loomy\*", "resources/app.asar.unpacked/package.json", "version"),
        (r"D:\software\loomy", "resources/app.asar.unpacked/package.json", "version"),
    ],
}


def _expand(p: str) -> str:
    return (p.replace("%LOCALAPPDATA%", os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
             .replace("%PUBLIC%", os.environ.get("PUBLIC", r"C:\Users\Public")))


def load_overrides() -> dict[str, dict[str, str]]:
    """从 config/versions.json 读取用户自定义适配版本（可选，缺失时用 ADAPTED 默认）。"""
    data = read_json(CONFIG_FILE, {})
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for platform, vals in data.items():
        if isinstance(vals, dict):
            out[str(platform)] = {str(k): str(v) for k, v in vals.items()}
    return out


def adapted(platform: str) -> dict[str, str]:
    overrides = load_overrides()
    return {**ADAPTED.get(platform, {}), **overrides.get(platform, {})}


def installed(platform: str) -> dict[str, str]:
    """只读探测本机客户端实际版本；找不到返回空 dict（上层显示"未知"）。"""
    overrides = load_overrides().get(platform, {})
    # 用户可在 versions.json 里用 _install_dirs 覆盖安装目录（glob 或绝对路径）
    extra_dirs = overrides.get("_install_dirs") or []
    rules = list(_PROBES.get(platform, []))
    if extra_dirs:
        # 对自定义目录，用该平台第一条规则的相对路径和字段
        if rules:
            rel, field = rules[0][1], rules[0][2]
            for d in extra_dirs:
                rules.insert(0, (str(d), rel, field))
    for root_pattern, rel, field in rules:
        for root in sorted(glob.glob(_expand(root_pattern))):
            f = Path(root) / rel
            try:
                d = json.loads(f.read_text(encoding="utf-8-sig"))
                v = d.get(field)
                if v is not None:
                    return {"version": str(v), "source": f"{Path(root).name}/{rel}"}
            except (OSError, ValueError):
                continue
    return {}


def drift_report(platforms: list[str]) -> list[dict[str, Any]]:
    """给 status 用：每个平台的适配版本 / 本机版本 / 是否漂移。"""
    out: list[dict[str, Any]] = []
    for p in platforms:
        ad = adapted(p)
        inst = installed(p)
        if not ad:
            continue
        actual = inst.get("version", "")
        expected = ad.get("client", "")
        # 版本体系不同（如 Trae 客户端 3.x vs 请求头 0.1.x）不自动判漂移，
        # 只并列展示让用户/ probe 判断；0.5.x 这类前缀视为匹配。
        prefix_ok = expected.endswith("x") and actual.startswith(expected[:-1])
        different_scheme = bool(ad.get("build")) and actual and not actual.startswith(expected.split(".")[0])
        drifted = bool(actual and expected and actual != expected and not prefix_ok and not different_scheme)
        out.append({
            "platform": p,
            "expected": expected,
            "actual": actual or "未知",
            "source": inst.get("source", ""),
            "cosy": ad.get("cosy", ""),
            "drifted": drifted,
            "note": "版本体系不同，需 probe 判断" if different_scheme else "",
        })
    return out
