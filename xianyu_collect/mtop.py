"""mtop 直连客户端（纯标准库）。

请求构造经 2026-10-06 实测验证：签名 md5(token&t&appKey&data)、
首调自动拿令牌重签、Set-Cookie 整体回填。UA 由 Account 层绑定传入。
"""
from __future__ import annotations

import hashlib
import json
import random
import time
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie

APP_KEY = "34839810"
MTOP_BASE = "https://h5api.m.goofish.com/h5/{api}/{version}/"
DEFAULT_ORIGIN = "https://www.goofish.com"
DEFAULT_REFERER = "https://www.goofish.com/"

# 令牌过期（自动重签重试）
TOKEN_MARKERS = ("FAIL_SYS_TOKEN_EXOIRED", "FAIL_SYS_TOKEN_EXPIRED", "FAIL_SYS_TOKEN_EMPTY")
# 风控/需验证（不重试，交 RiskGuard 熔断）
RISK_MARKERS = (
    "FAIL_SYS_USER_VALIDATE", "RGV587", "punish", "哎哟喂", "挤爆",
    "FAIL_SYS_ILLEGAL_ACCESS", "WUA_IS_MACHINE", "FAIL_BIZ_WUA_IS_MACHINE",
)
# 会话失效（需重新登录）
SESSION_MARKERS = ("FAIL_SYS_SESSION_EXPIRED",)

ErrorKind = str  # "token" | "risk" | "session" | "biz" | "network"


def trans_cookies(cookie_str: str) -> dict:
    cookies: dict = {}
    for pair in cookie_str.replace("\n", "").replace("\r", "").split(";"):
        pair = pair.strip()
        if "=" in pair:
            k, val = pair.split("=", 1)
            cookies[k.strip()] = val.strip()
    return cookies


def generate_sign(t: str, token: str, data_val: str, app_key: str = APP_KEY) -> str:
    msg = f"{token}&{t}&{app_key}&{data_val}"
    return hashlib.md5(msg.encode("utf-8")).hexdigest()


def classify_ret(ret: list) -> ErrorKind:
    joined = "; ".join(ret)
    if any(m in joined for m in TOKEN_MARKERS):
        return "token"
    if any(m in joined for m in RISK_MARKERS):
        return "risk"
    if any(m in joined for m in SESSION_MARKERS):
        return "session"
    return "biz"


class MtopError(Exception):
    def __init__(self, api: str, ret: list, res: dict | None = None, kind: ErrorKind = "biz"):
        self.api = api
        self.ret = ret
        self.res = res
        self.kind = kind
        super().__init__(f"{api}[{kind}]: {'; '.join(ret)}")

    @property
    def punish_url(self) -> str:
        data = (self.res or {}).get("data") or {}
        return str(data.get("url") or "") if isinstance(data, dict) else ""


class MtopClient:
    """同步 mtop 客户端：内置频控 + 令牌自动刷新 + Cookie 整体回填。"""

    def __init__(
        self,
        cookies: dict | str,
        ua: str,
        min_interval: float = 1.5,
        origin: str = DEFAULT_ORIGIN,
        referer: str = DEFAULT_REFERER,
    ):
        self.cookies: dict = trans_cookies(cookies) if isinstance(cookies, str) else dict(cookies)
        self.ua = ua
        self.min_interval = min_interval
        self.origin = origin
        self.referer = referer
        self._last_request_at = 0.0

    # ---------------------------------------------------------------- 基础

    @property
    def token(self) -> str:
        val = self.cookies.get("_m_h5_tk", "")
        return val.split("_")[0] if val else ""

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def _pace(self) -> None:
        """频控：账号级最小间隔 + 抖动，从第一行代码带上（风控教训）。"""
        wait = self.min_interval + random.uniform(0, 0.6) - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _merge_set_cookies(self, resp) -> None:
        for header in resp.headers.get_all("Set-Cookie") or []:
            first = header.split(";", 1)[0]
            if "=" not in first:
                continue
            k, val = first.split("=", 1)
            k, val = k.strip(), val.strip()
            if val:
                self.cookies[k] = val

    # ---------------------------------------------------------------- 调用

    def call(
        self,
        api: str,
        payload: dict,
        version: str = "1.0",
        referer: str | None = None,
        origin: str | None = None,
        attempts: int = 3,
    ) -> dict:
        url = MTOP_BASE.format(api=api, version=version)
        headers_origin = origin or self.origin
        headers_referer = referer or self.referer
        last_ret: list = ["<no-response>"]
        last_res: dict | None = None

        for attempt in range(1, attempts + 1):
            self._pace()
            t = str(int(time.time() * 1000))
            data_val = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            params = {
                "jsv": "2.7.2", "appKey": APP_KEY, "t": t,
                "sign": generate_sign(t, self.token, data_val),
                "v": version, "type": "originaljson", "accountSite": "xianyu",
                "dataType": "json", "timeout": "20000", "api": api,
                "sessionOption": "AutoLoginOnly", "spm_cnt": "a21ybx.item.0.0",
            }
            body = urllib.parse.urlencode({"data": data_val}).encode()
            req = urllib.request.Request(
                f"{url}?{urllib.parse.urlencode(params)}", data=body, method="POST",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": headers_origin,
                    "Referer": headers_referer,
                    "User-Agent": self.ua,
                    "Cookie": self.cookie_header(),
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    self._merge_set_cookies(resp)
                    res = json.loads(resp.read().decode("utf-8", "replace"))
            except Exception as exc:
                last_ret = [f"network:{exc}"]
                last_res = None
                continue

            ret = [str(x) for x in (res.get("ret") or [])]
            last_ret, last_res = ret, res
            kind = classify_ret(ret)
            if "SUCCESS" in "; ".join(ret):
                return res
            if kind == "token":
                continue  # 已合并新令牌，重签重试
            raise MtopError(api, ret, res, kind)  # risk/session/biz 不重试

        raise MtopError(api, last_ret, last_res, classify_ret(last_ret))
