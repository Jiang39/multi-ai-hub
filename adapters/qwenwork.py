"""千问办公 QwenWork 适配器（已接协议）。

登录态：DPAPI+AES-GCM 解密 auth-v2.dat；chat 用 COSY 1.1.32 签名 + 完整 body。
实测：deepseek-flash / glm-5.3 / kimi-k3.1-pro 均可调用。
"""

from typing import Any

from core.adapter import PlatformAdapter
from core import protocols as pr
from core.models import Account, Credits, ModelLimit, SigninResult

PLATFORM = "qwenwork"


class QwenWorkAdapter(PlatformAdapter):
    platform = PLATFORM
    display_name = "千问办公"
    declared_models = tuple(pr.VERIFIED_MODELS[PLATFORM].keys())

    def discover_accounts(self) -> list[Account]:
        return [Account(platform=self.platform, uid=a["uid"], label=a["name"], source=a["source"])
                for a in pr.discover(self.platform)]

    def fetch_credits(self, account: Account) -> Credits:
        c = pr.credits(self.platform)
        return Credits(ok=c.get("ok", False), remain=c.get("remain"), total=c.get("total"),
                       detail=c.get("detail", ""), error=c.get("error", ""))

    def checkin(self, account: Account) -> SigninResult:
        return SigninResult(ok=False, status="UNSUPPORTED", gained=0,
                            message="QwenWork 每日积分由上游自动发放，签到接口待实测")

    def fetch_limits(self, accounts: list[Account]) -> list[ModelLimit]:
        return []

    def chat(self, account: Account, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        return pr.chat(self.platform, model, payload)

    def health(self) -> tuple[bool, str]:
        return self.protocol_health()
