"""错误报文脱敏：唯一实现，供网关原生层与服务层共用。

错误文本可能含本机用户目录、bearer token、`token: xxx` 之类敏感信息；
本地面板结合 DNS rebinding 可被外部站点读取，因此回给客户端前统一过滤。
"""

from __future__ import annotations

import re
from typing import Any

_USERDIR_RE = re.compile(
    r"[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][^\\/\s\"']+", re.I)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}")
_TOKEN_FIELD_RE = re.compile(r"(?i)(token[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9._\-]{12,}")


def safe_error(text: Any, limit: int = 200) -> str:
    s = str(text or "")
    s = _USERDIR_RE.sub("<USERDIR>", s)
    s = _BEARER_RE.sub(r"\1<REDACTED>", s)
    s = _TOKEN_FIELD_RE.sub(r"\1<REDACTED>", s)
    return s[:limit]
