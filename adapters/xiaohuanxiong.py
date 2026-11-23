"""商汤小浣熊适配器（已接协议）。

登录态明文 auth.json；chat 标准 OpenAI /chat/completions（非流式）；
额度 /points/v1/balance；签到 /points/v1/sign-in。
实测：sn-deepseek-v4-1-flash-1017 / sn-glm-5-3 / sn-kimi-k3-1-pro 全通。
"""

from typing import Any

from core.adapter import PlatformAdapter
from core import protocols as pr
from core.models import Account, Credits, ModelLimit, SigninResult

PLATFORM = "xiaohuanxiong"


class XiaohuanxiongAdapter(PlatformAdapter):
    platform = PLATFORM
    display_name = "商汤小浣熊"
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
        return []

    def chat(self, account: Account, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        return pr.chat(self.platform, model, payload)

    def health(self) -> tuple[bool, str]:
        return self.protocol_health()
