#!/usr/bin/env python3
"""x5sec 风控验证流程 v2（patchright + 系统 Chrome 单环境方案）：

1. patchright 启动系统正式版 Chrome（真实指纹，管家的隐身方案）
2. 注入登录 cookie，取浏览器真实 UA
3. 直连搜索（预热令牌）→ 直连详情 → 命中风控拿 punish 链接
4. 同一个浏览器当场打开 punish 页 → 人工拖滑块 → x5sec 落地
5. 导出浏览器整套 cookie → 同 UA 立刻重试直连详情
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import verify_collect as v

HERE = Path(__file__).parent
ITEM_ID = sys.argv[1] if len(sys.argv) > 1 else "1090049960477"

LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--start-maximized",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--lang=zh-CN",
    "--disable-blink-features=AutomationControlled",
    "--no-default-browser-check",
    "--disable-popup-blocking",
    "--disable-search-engine-choice-screen",
    "--password-store=basic",
    "--use-mock-keychain",
]


def cookie_pairs(cookie_str: str) -> list:
    return [
        {"name": k, "value": val, "domain": ".goofish.com", "path": "/"}
        for k, val in v.trans_cookies(cookie_str).items()
    ]


async def run() -> int:
    try:
        from patchright.async_api import async_playwright
        engine = "patchright"
    except ImportError:
        from playwright.async_api import async_playwright
        engine = "playwright"
    print(f"浏览器引擎: {engine} + 系统 Chrome")

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            str(HERE / ".chrome_profile"),
            channel="chrome",
            headless=False,
            no_viewport=True,
            args=LAUNCH_ARGS,
        )
        page = await context.new_page()
        await context.add_cookies(cookie_pairs((HERE / "cookie.txt").read_text(encoding="utf-8").strip()))
        ua = await page.evaluate("navigator.userAgent")
        (HERE / "ua.txt").write_text(ua, encoding="utf-8")
        print("浏览器真实 UA（已存档 ua.txt）:", ua[:80])

        ok = False
        try:
            # 1) 搜索预热（同 UA）
            client = v.MtopClient((HERE / "cookie.txt").read_text(encoding="utf-8").strip(), ua=ua)
            client.call("mtop.taobao.idlemtopsearch.pc.search", {
                "keyword": "iPhone 13", "fromFilter": False, "rowsPerPage": 30,
                "bizFrom": "main_search", "pageNumber": 1, "sortType": "type",
                "extraFilterValue": "{}",
            })
            print("搜索通道 OK")

            # 2) 直连详情 → 预期命中风控
            referer = f"https://www.goofish.com/item?id={ITEM_ID}"
            punish_url = ""
            try:
                direct_res = client.call("mtop.taobao.idle.pc.detail", {"itemId": ITEM_ID}, "1.0", referer=referer)
                print("详情直接成功（未触发风控）")
                ok = True
                (HERE / "detail_raw.json").write_text(
                    json.dumps(direct_res, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(v.parse_detail(direct_res), ensure_ascii=False, indent=2))
            except v.MtopError as exc:
                data = (exc.res or {}).get("data") or {}
                punish_url = str(data.get("url") or "")
                print("详情触发风控，punish:", punish_url[:90])

            # 3) 同一浏览器当场解题
            if not ok and punish_url:
                print(">>> 请在弹出的 Chrome 窗口里拖动滑块（最长等 3 分钟）<<<")
                await page.goto(punish_url, timeout=45000)
                x5sec = None
                deadline = time.time() + 180
                while time.time() < deadline:
                    await asyncio.sleep(2)
                    for c in await context.cookies():
                        if c["name"] == "x5sec":
                            x5sec = c["value"]
                            break
                    if x5sec:
                        break
                if not x5sec:
                    print("未拿到 x5sec（滑块未通过或超时）")
                    return 3
                print("✅ 拿到 x5sec:", x5sec[:36], "...")

                # 4) 导出整套 cookie → 同 UA 立刻重试
                merged = v.trans_cookies((HERE / "cookie.txt").read_text(encoding="utf-8").strip())
                for c in await context.cookies():
                    if c["domain"].endswith("goofish.com"):
                        merged[c["name"]] = c["value"]
                cookie_str = "; ".join(f"{k}={val}" for k, val in merged.items())
                (HERE / "cookie.txt").write_text(cookie_str, encoding="utf-8")

                client2 = v.MtopClient(cookie_str, ua=ua)
                res = client2.call("mtop.taobao.idle.pc.detail", {"itemId": ITEM_ID}, "1.0", referer=referer)
                detail = v.parse_detail(res)
                detail["api"] = "mtop.taobao.idle.pc.detail + x5sec(系统Chrome)"
                print(json.dumps(detail, ensure_ascii=False, indent=2))
                ok = True
        finally:
            await context.close()

    if ok:
        print("\n✅ 直连详情验证通过（patchright + 系统 Chrome + x5sec）")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
