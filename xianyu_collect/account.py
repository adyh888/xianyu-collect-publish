"""账号会话管理：cookie + UA 绑定持久化。

UA 纪律（实测教训）：账号的 UA 在首次成功采集或扫码后的浏览器环境中确定，
存档后永久复用，版本号都不能重写。默认 UA 取自本机已验证的真实值。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .mtop import trans_cookies

# 本机（macOS 系统Chrome）已验证可用的 UA；新账号应绑定各自的真实 UA
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)


@dataclass
class Account:
    name: str
    cookies: dict = field(default_factory=dict)
    ua: str = DEFAULT_UA
    unb: str = ""
    updated_at: float = 0.0

    @classmethod
    def from_cookie_string(cls, name: str, cookie_str: str, ua: str = DEFAULT_UA) -> "Account":
        return cls(name=name, cookies=trans_cookies(cookie_str), ua=ua, updated_at=time.time())

    def cookie_string(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "cookies": self.cookies,
            "ua": self.ua,
            "unb": self.unb,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Account":
        return cls(
            name=data["name"],
            cookies=dict(data.get("cookies") or {}),
            ua=data.get("ua") or DEFAULT_UA,
            unb=data.get("unb") or "",
            updated_at=data.get("updated_at") or 0.0,
        )


class AccountStore:
    """每个账号一个 session.json（含登录态），放在 data/accounts/<name>/ 下。"""

    def __init__(self, data_root: Path | str):
        self.root = Path(data_root) / "accounts"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self.root / name / "session.json"

    def save(self, account: Account) -> Path:
        path = self._path(account.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(account.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def load(self, name: str) -> Account | None:
        path = self._path(name)
        if not path.exists():
            return None
        return Account.from_json(json.loads(path.read_text(encoding="utf-8")))

    def names(self) -> list:
        return sorted(p.parent.name for p in self.root.glob("*/session.json"))
