"""平台适配器接口。

每个平台（qoder-intl / qwenwork / loomy / xiaohuanxiong）实现这五个方法。
未接协议的适配器继承 StubAdapter 即可：框架能发现、能显示、能跳过，
但不会伪造签到成功或额度数字。

关于凭据：discover_accounts() 只读本机登录态文件并提取非敏感标识，
绝不返回令牌；令牌由适配器内部在 chat() / checkin() 时读取，不出适配器边界。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterator

from .models import Account, Credits, ModelLimit, SigninResult, UsageRecord

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


class PlatformAdapter(ABC):
    platform: str = ""
    display_name: str = ""
    # 该平台对外暴露的模型清单（源码/文档确认）。实际可用性以账号套餐为准。
    declared_models: tuple[str, ...] = ()

    # --- 必须实现 ---------------------------------------------------------
    @abstractmethod
    def discover_accounts(self) -> list[Account]:
        """从本机登录态发现账号；无登录态返回 []，不抛异常。"""

    @abstractmethod
    def fetch_credits(self, account: Account) -> Credits:
        """读取该账号积分/额度。平台无此接口时返回 ok=False 并说明原因。"""

    @abstractmethod
    def checkin(self, account: Account) -> SigninResult:
        """签到/领取每日积分。重复签到必须幂等（返回 ALREADY）。"""

    @abstractmethod
    def fetch_limits(self, accounts: list[Account]) -> list[ModelLimit]:
        """读取账号×模型限流状态。无实时接口时返回 []。"""

    @abstractmethod
    def chat(self, account: Account, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        """把统一请求转成平台请求并调用上游，返回 OpenAI 兼容响应 dict。"""

    # --- 可选实现 ---------------------------------------------------------
    def usage(self, accounts: list[Account]) -> list[UsageRecord]:
        return []

    def health(self) -> tuple[bool, str]:
        """检查登录态是否就绪；返回 (可用, 说明)。"""
        accts = self.discover_accounts()
        if not accts:
            return False, "未发现本机登录态"
        return True, f"发现 {len(accts)} 个账号"

    def protocol_health(self) -> tuple[bool, str]:
        """新平台共用：登录态可读性 + 治理层临时停用状态（零网络）。"""
        from core import protocols as pr
        from core.governance import GOVERNANCE

        ok, detail = pr.probe(self.platform)
        if not ok:
            return False, detail
        accts = self.discover_accounts()
        disabled = [a for a in accts if GOVERNANCE.is_disabled(self.platform, a.uid)]
        base = f"已登录：{accts[0].label}" if accts else "登录态可读"
        if disabled:
            return False, f"{base}；{len(disabled)} 个账号临时停用（连续失败）"
        return True, base


class StubAdapter(PlatformAdapter):
    """协议待补的适配器：框架可枚举，但所有动作明确返回 UNSUPPORTED。

    这样面板能展示「4 家平台已接入、协议待补」，不会把未实现的平台
    误显示成额度 0 或签到失败。
    """

    stub_reason: str = "协议待补：需安装客户端并实测登录态与接口后实现"

    def discover_accounts(self) -> list[Account]:
        return []

    def fetch_credits(self, account: Account) -> Credits:
        return Credits(ok=False, error=self.stub_reason)

    def checkin(self, account: Account) -> SigninResult:
        return SigninResult(ok=False, status="UNSUPPORTED", message=self.stub_reason)

    def fetch_limits(self, accounts: list[Account]) -> list[ModelLimit]:
        return []

    def chat(self, account: Account, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"error": {"message": self.stub_reason, "type": "unsupported"}}

    def health(self) -> tuple[bool, str]:
        return False, self.stub_reason


def load_registry() -> dict[str, PlatformAdapter]:
    """加载所有适配器实例。导入失败不拖垮整个框架。"""
    from adapters import loomy, qoder_intl, qwenwork, traework, workbuddy, xiaohuanxiong

    registry: dict[str, PlatformAdapter] = {}
    for module in (qoder_intl, qwenwork, loomy, xiaohuanxiong, traework, workbuddy):
        for attr in dir(module):
            cls = getattr(module, attr)
            if isinstance(cls, type) and issubclass(cls, PlatformAdapter) and cls is not PlatformAdapter:
                if cls is StubAdapter:
                    continue
                inst = cls()
                if inst.platform:
                    registry[inst.platform] = inst
    return registry


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default
