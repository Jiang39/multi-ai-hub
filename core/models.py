"""跨厂商统一的数据对象。

设计原则：
- 只放「所有平台都能表达」的字段，平台特有信息塞 raw。
- 所有时间统一为毫秒时间戳（与 WorkBuddy 网关 ratelimit.json 一致），
  展示字符串由适配器给出，面板不做二次时区换算。
- 不在这里放令牌。凭据只存在于适配器 / 本机登录态文件，接口层不暴露。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Account:
    """一个平台账号在统一层里的身份。uid 是平台内唯一 id（不是令牌）。"""

    platform: str                 # qoder-intl | qwenwork | loomy | xiaohuanxiong
    uid: str                      # 平台内唯一 id，用于别名 key / 限流矩阵行
    label: str = ""               # 默认展示名（平台昵称或脱敏标识）
    uin: str = ""                 # 平台展示用的账号标识（可选）
    source: str = ""              # 登录态来源文件路径（仅文件名/路径，不含内容）
    enabled: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.uid}"


@dataclass
class Credits:
    """积分/额度快照。平台没有的字段留 None，不要填 0 冒充。"""

    remain: float | None = None
    total: float | None = None
    used: float | None = None
    refreshed_at: int | None = None   # ms
    detail: str = ""                  # 钱包分段/有效期等说明
    ok: bool = True
    error: str = ""


@dataclass
class SigninResult:
    """一次签到动作的结果。幂等：今天已领也算 ok，gained=0。"""

    ok: bool
    status: str = ""        # CLAIMED | ALREADY | NO_CAMPAIGN | ERROR | NEED_AUTH | UNSUPPORTED
    gained: float = 0.0
    message: str = ""
    at: int | None = None   # ms
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelLimit:
    """账号 × 模型的限流状态，对应 WorkBuddy 网关 ratelimit.json 的 models 项。"""

    platform: str
    uid: str
    model: str
    limited: bool
    until: int | None = None      # ms；limited=False 时忽略
    local: str = ""               # UTC+8 展示串，面板直接用，不二次换算
    source: str = ""              # live | estimated | static
    raw: dict[str, Any] = field(default_factory=dict)  # 平台附带信息（如账号展示名）

    @property
    def key(self) -> str:
        return f"{self.platform}|{self.uid}|{self.model}"


@dataclass
class UsageRecord:
    """网关调用统计的一条聚合值。"""

    platform: str
    uid: str
    model: str
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    credit: float = 0.0


def credits_to_dict(c: Credits) -> dict[str, Any]:
    return {
        "remain": c.remain,
        "total": c.total,
        "used": c.used,
        "refreshedAt": c.refreshed_at,
        "detail": c.detail,
        "ok": c.ok,
        "error": c.error,
    }


def account_to_dict(a: Account) -> dict[str, Any]:
    return {
        "platform": a.platform,
        "uid": a.uid,
        "key": a.key,
        "label": a.label,
        "uin": a.uin,
        "source": a.source,
        "enabled": a.enabled,
    }
