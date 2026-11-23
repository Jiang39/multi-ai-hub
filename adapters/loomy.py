"""讯飞 Loomy 适配器（已接协议）。

登录态明文 auth-session.json；chat 走标准 OpenAI /chat/completions（非流式更稳）。
实测：DeepSeek-V4.1-Flash / GLM-5.3-Flash 可调用；无 Kimi（用户已确认接受）。
"""

from typing import Any

from core.adapter import PlatformAdapter
from core import protocols as pr
from core.models import Account, Credits, ModelLimit, SigninResult

PLATFORM = "loomy"


class LoomyAdapter(PlatformAdapter):
    platform = PLATFORM
    display_name = "讯飞 Loomy"
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
                            message="Loomy 每日额度由上游自动发放，无需签到")

    def fetch_limits(self, accounts: list[Account]) -> list[ModelLimit]:
        return []

    def chat(self, account: Account, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        return pr.chat(self.platform, model, payload)

    def health(self) -> tuple[bool, str]:
        return self.protocol_health()
