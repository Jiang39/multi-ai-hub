"""Qoder 国际版适配器（已接协议）。

登录态：DPAPI+AES-GCM 解密 auth.v1.dat；chat 用 COSY 签名 + 自定义表 body 编码。
启动以来实测：DeepSeek-Flash / GLM-5.3 / Kimi-K3.1-Pro 均可调用。
"""

from core.adapter import PlatformAdapter
from core import protocols as pr
from core.models import Account, Credits, ModelLimit, SigninResult, UsageRecord
from typing import Any

PLATFORM = "qoder-intl"


class QoderIntlAdapter(PlatformAdapter):
    platform = PLATFORM
    display_name = "Qoder 国际版"
    declared_models = tuple(pr.VERIFIED_MODELS[PLATFORM].keys())

    def discover_accounts(self) -> list[Account]:
        return [Account(platform=self.platform, uid=a["uid"], label=a["name"], source=a["source"])
                for a in pr.discover(self.platform)]

    def fetch_credits(self, account: Account) -> Credits:
        c = pr.credits(self.platform)
        return Credits(ok=c.get("ok", False), remain=c.get("remain"), total=c.get("total"),
                       detail=c.get("detail", ""), error=c.get("error", ""))

    def checkin(self, account: Account) -> SigninResult:
        r = pr.checkin(self.platform)
        return SigninResult(ok=r.get("ok", False), status=r.get("status", ""),
                            gained=r.get("gained", 0), message=r.get("message", ""))

    def fetch_limits(self, accounts: list[Account]) -> list[ModelLimit]:
        return []  # 网关未接入前无实时限流数据

    def chat(self, account: Account, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        return pr.chat(self.platform, model, payload)

    def health(self) -> tuple[bool, str]:
        return self.protocol_health()
