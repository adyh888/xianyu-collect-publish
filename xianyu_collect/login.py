"""自包含扫码登录（httpx + qrcode，可选项依赖）。

流程移植自闲鱼管家 utils/qr_login.py（已实测）：
h5 种令牌 cookie → mini_login.htm 取登录表单 → generate.do 出二维码
→ query.do 轮询（NEW/SCANED/CONFIRMED）→ CONFIRMED 响应 Cookie 即登录态。
账号遇到人脸验证（iframeRedirect）时提示改用管家端登录。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import time
from pathlib import Path
from typing import Callable

import httpx
import qrcode

PASSPORT = "https://passport.goofish.com"
API_MINI_LOGIN = f"{PASSPORT}/mini_login.htm"
API_GENERATE_QR = f"{PASSPORT}/newlogin/qrcode/generate.do"
API_QUERY = f"{PASSPORT}/newlogin/qrcode/query.do"
API_H5TK = "https://h5api.m.goofish.com/h5/mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get/1.0/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://passport.goofish.com/",
    "Origin": "https://passport.goofish.com",
}


class QRLoginResult:
    def __init__(self, cookies: dict, unb: str):
        self.cookies = cookies
        self.unb = unb


async def qr_login(
    qr_png_path: Path | str | None = None,
    log: Callable = print,
    timeout: float = 300.0,
) -> QRLoginResult:
    timeout_cfg = httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=60.0)
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_cfg) as client:
        # 1) 种令牌 cookie
        await client.get(API_H5TK, headers=HEADERS)

        # 2) 登录表单参数（mini_login 页面内嵌 viewData.loginFormData）
        mini_params = {
            "lang": "zh_cn", "appName": "xianyu", "appEntrance": "web",
            "styleType": "vertical", "bizParams": "", "notLoadSsoView": "false",
            "notKeepLogin": "false", "isMobile": "false", "qrCodeFirst": "false",
            "stie": 77, "rnd": random.random(),
        }
        resp = await client.get(API_MINI_LOGIN, params=mini_params, headers=HEADERS)
        m = re.search(r"window\.viewData\s*=\s*(\{.*?\});", resp.text)
        if not m:
            raise RuntimeError("mini_login 页面未找到 viewData，接口可能已变化")
        form = json.loads(m.group(1)).get("loginFormData") or {}
        if not form:
            raise RuntimeError("viewData 中无 loginFormData")
        form["umidTag"] = "SERVER"

        # 3) 生成二维码
        resp = await client.get(API_GENERATE_QR, params=form, headers=HEADERS)
        payload = resp.json()
        if not payload.get("content", {}).get("success"):
            raise RuntimeError(f"生成二维码失败: {json.dumps(payload, ensure_ascii=False)[:200]}")
        data = payload["content"]["data"]
        form["t"], form["ck"] = data["t"], data["ck"]
        qr_content = data["codeContent"]

        qr = qrcode.QRCode(border=2)
        qr.add_data(qr_content)
        if qr_png_path:
            qr.make_image().save(qr_png_path)
            log(f"QR_PNG_READY: {qr_png_path}")
        qr.print_ascii(invert=True)
        log("请用手机闲鱼 App 扫码登录（5 分钟内有效）...")

        # 4) 轮询扫码状态
        deadline = time.time() + timeout
        last_status = ""
        while time.time() < deadline:
            await asyncio.sleep(1.2)
            resp = await client.post(API_QUERY, data=form, headers=HEADERS)
            body = resp.json().get("content", {}).get("data", {}) or {}
            status = body.get("qrCodeStatus") or ""
            if status != last_status:
                log(f"[{time.strftime('%H:%M:%S')}] status = {status or 'EMPTY'}")
                last_status = status

            if status == "CONFIRMED":
                cookies = {k: v for k, v in resp.cookies.items()}
                unb = cookies.get("unb", "")
                if body.get("iframeRedirect") and not unb:
                    log("⚠️ 账号触发人脸/短信验证：请在闲鱼管家 App 端完成登录后重试，或用管家后台导出 cookie")
                    continue
                return QRLoginResult(cookies=cookies, unb=unb)
            if status == "EXPIRED":
                raise RuntimeError("二维码已过期，请重新运行 login")
        raise RuntimeError("扫码超时")
