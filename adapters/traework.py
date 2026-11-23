"""TraeWork 国内版适配器（已接协议）。

登录态：storage.json 里 iCubeAuthInfo 用 tc AES 解密；chat 走 llm_utils_chat SSE。
实测：deepseek-v4.1-flash / glm-5.3 / kimi-k3 可调用
      （kimi-k3.1-pro 套餐受限 code 1005，按用户规则回退 K3）。
签到：checkin_credits status/claim。
"""

from typing import Any

from core.adapter import PlatformAdapter
from core import protocols as pr
from core.models import Account, Credits, ModelLimit, SigninResult

PLATFORM = "traework"


class TraeWorkAdapter(PlatformAdapter):
    platform = PLATFORM
    display_name = "TraeWork 国内版"
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
