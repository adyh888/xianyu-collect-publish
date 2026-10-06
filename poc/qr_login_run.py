#!/usr/bin/env python3
"""独立扫码登录 runner：复用闲鱼管家 backend 的 QRLoginManager 生成二维码，
轮询扫码状态，成功后把 cookie 保存到同目录 cookie.txt。"""
import asyncio
import base64
import sys
import time
from pathlib import Path

BACKEND = "/Users/liqian/Desktop/MyProject/xianyu-butler-desktop/backend"
sys.path.insert(0, BACKEND)

import qrcode

from utils.qr_login import QRLoginManager

HERE = Path(__file__).parent
OUT = HERE / "cookie.txt"


async def main() -> int:
    manager = QRLoginManager()
    info = await manager.generate_qr_code()
    if not info.get("success"):
        print("二维码生成失败:", info.get("message"))
        return 1

    sid = info["session_id"]
    qr_content = manager.sessions[sid].qr_content

    qr = qrcode.QRCode(border=2)
    qr.add_data(qr_content)
    png_path = HERE / "login_qr.png"
    qr.make_image().save(png_path)
    print(f"QR_PNG_READY: {png_path}")
    print("请用手机闲鱼 App 扫码登录（5 分钟内有效）...")

    verification_saved = False
    deadline = 300
    start = time.time()
    last = None
    while time.time() - start < deadline:
        await asyncio.sleep(2)
        status = manager.get_session_status(sid)
        s = status.get("status")
        if s != last:
            print(f"[{time.strftime('%H:%M:%S')}] status = {s}")
            last = s
        if s == "verification_required" and not verification_saved:
            vurl = status.get("verification_qr_code_url") or ""
            if vurl.startswith("data:image/png;base64,"):
                vpath = HERE / "verify_qr.png"
                vpath.write_bytes(base64.b64decode(vurl.split(",", 1)[1]))
                print(f"VERIFY_QR_PNG_READY: {vpath}（需要人脸验证时扫这张）")
                verification_saved = True
        if s == "success":
            OUT.write_text(status["cookies"], encoding="utf-8")
            print(f"LOGIN_OK unb={status.get('unb')} chars={len(status['cookies'])} saved={OUT}")
            return 0
        if s in ("expired", "cancelled"):
            print("LOGIN_FAILED status =", s)
            return 2
    print("LOGIN_TIMEOUT")
    return 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
