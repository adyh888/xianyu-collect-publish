#!/usr/bin/env python3
"""详情采集·浏览器通道：加载登录 cookie 打开商品页，拦截页面自己发出的详情接口响应。
用法：python3 detail_browser.py <商品ID>"""
import asyncio
import json
import os
import sys
from pathlib import Path

BROWSERS = "/Users/liqian/Desktop/MyProject/xianyu-butler-desktop/build-output/browsers"
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", BROWSERS)

from playwright.async_api import async_playwright

HERE = Path(__file__).parent
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DETAIL_HINTS = ("mtop.taobao.idle.pc.detail", "mtop.taobao.idlemtopdetail", "idle.detail")


def cookie_pairs(cookie_str: str) -> list:
    cookies = []
    for pair in cookie_str.replace("\n", "").split(";"):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            cookies.append({"name": k.strip(), "value": v.strip(), "domain": ".goofish.com", "path": "/"})
    return cookies


async def main(item_id: str) -> int:
    cookie_str = (HERE / "cookie.txt").read_text(encoding="utf-8").strip()
    captured: list[dict] = []

    async with async_playwright() as pw:
        headed = os.environ.get("HEADED") == "1"
        context = await pw.chromium.launch_persistent_context(
            str(HERE / ".browser_profile"),
            headless=not headed,
            user_agent=UA,
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            args=["--no-sandbox", "--disable-dev-shm-usage", "--lang=zh-CN", "--disable-blink-features=AutomationControlled"],
        )
        await context.add_cookies(cookie_pairs(cookie_str))
        page = await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        async def on_response(resp):
            if any(h in resp.url for h in DETAIL_HINTS):
                try:
                    captured.append(await resp.json())
                except Exception:
                    pass

        page.on("response", on_response)

        url = f"https://www.goofish.com/item?id={item_id}"
        print(f"打开 {url}")
        try:
            await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            await page.mouse.move(640, 400)
            await page.mouse.wheel(0, 600)
            await page.wait_for_timeout(6000)
            await page.mouse.wheel(0, 400)
            await page.wait_for_timeout(6000)
            title = await page.title()
            print("页面标题:", title)
        except Exception as exc:
            print("页面加载异常:", exc)

        await context.close()

    if not captured:
        print("未捕获到详情接口响应（可能触发滑块，需走管家远程验证通道）")
        return 2

    payload = captured[0]
    ret = payload.get("ret")
    print("ret:", ret)
    data = payload.get("data") or {}

    def deep_find(obj, keys):
        if isinstance(obj, dict):
            for k, val in obj.items():
                if k in keys and val not in (None, "", [], {}):
                    return val
            for val in obj.values():
                found = deep_find(val, keys)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for val in obj:
                found = deep_find(val, keys)
                if found is not None:
                    return found
        return None

    images = deep_find(data, {"imageInfos"}) or []
    result = {
        "ret": ret,
        "title": deep_find(data, {"title"}),
        "price": deep_find(data, {"soldPrice", "price"}),
        "desc": str(deep_find(data, {"desc", "description"}) or "")[:200],
        "image_count": len(images) if isinstance(images, list) else 0,
        "top_keys": list(data.keys())[:15],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    (HERE / "detail_result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[saved] detail_result.json")
    return 0 if ret and "SUCCESS" in str(ret) else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1])))
