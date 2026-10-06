#!/usr/bin/env python3
"""x5sec 风控验证流程：
1. 直连调详情接口 → 触发风控拿 punish 链接
2. 有头浏览器打开 punish 链接 → 人工拖滑块 → 浏览器种下 x5sec cookie
3. 把 x5sec 合并进直连客户端 cookie → 重试详情接口（预期 SUCCESS）
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import verify_collect as v

HERE = Path(__file__).parent
BROWSERS = "/Users/liqian/Desktop/MyProject/xianyu-butler-desktop/build-output/browsers"
ITEM_ID = sys.argv[1] if len(sys.argv) > 1 else "1090049960477"


async def harvest_x5sec(punish_url: str) -> str | None:
    """解题浏览器与直连客户端同 UA；解完后回填浏览器整套 cookie 状态。"""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            str(HERE / ".browser_profile"),
            headless=False,
            user_agent=v.UA,
            viewport={"width": 520, "height": 640},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"],
        )
        await context.clear_cookies()  # 清掉 profile 残留的旧 x5sec，避免误判
        await context.add_cookies([
            {"name": k, "value": val, "domain": ".goofish.com", "path": "/"}
            for k, val in v.trans_cookies((HERE / "cookie.txt").read_text(encoding="utf-8").strip()).items()
        ])
        page = await context.new_page()
        await page.goto(punish_url, timeout=45000)
        print("已打开验证页，请在弹出的窗口里完成滑块验证（最长等 3 分钟）...")

        x5sec = None
        deadline = time.time() + 180
        while time.time() < deadline:
            await asyncio.sleep(2)
            for c in await context.cookies():
                if c["name"] == "x5sec":
                    x5sec = c["value"]
                    break
            if x5sec:
                # 回填浏览器整套 cookie 状态（含新指纹 isg/tfstk 等）
                merged = v.trans_cookies((HERE / "cookie.txt").read_text(encoding="utf-8").strip())
                for c in await context.cookies():
                    if c["domain"].endswith("goofish.com"):
                        merged[c["name"]] = c["value"]
                (HERE / "cookie.txt").write_text(
                    "; ".join(f"{k}={val}" for k, val in merged.items()), encoding="utf-8")
                print(f"浏览器 cookie 已整体回填（{len(merged)} 字段）")
                break
        await context.close()
    return x5sec


def main() -> int:
    client = v.MtopClient((HERE / "cookie.txt").read_text(encoding="utf-8").strip())
    client.call("mtop.taobao.idlemtopsearch.pc.search", {
        "keyword": "iPhone 13", "fromFilter": False, "rowsPerPage": 30,
        "bizFrom": "main_search", "pageNumber": 1, "sortType": "type",
        "extraFilterValue": "{}",
    })
    print("搜索通道 OK")

    referer = f"https://www.goofish.com/item?id={ITEM_ID}"
    punish_url = ""
    try:
        client.call("mtop.taobao.idle.pc.detail", {"itemId": ITEM_ID}, "1.0", referer=referer)
        print("详情直接成功（未触发风控）")
        return 0
    except v.MtopError as exc:
        data = (exc.res or {}).get("data") or {}
        punish_url = str(data.get("url") or "")
        print("详情触发风控，punish:", punish_url[:100])
        if not punish_url:
            return 2

    # x5sec 短时效，强制重新解题；harvest 已把浏览器整套 cookie 回填到 cookie.txt
    stale = HERE / "x5sec.txt"
    if stale.exists():
        stale.unlink()
    x5sec = asyncio.run(harvest_x5sec(punish_url))
    if not x5sec:
        print("未拿到 x5sec")
        return 3
    print("✅ 拿到 x5sec:", x5sec[:40], "...")

    client2 = v.MtopClient((HERE / "cookie.txt").read_text(encoding="utf-8").strip())
    res = client2.call("mtop.taobao.idle.pc.detail", {"itemId": ITEM_ID}, "1.0", referer=referer)
    detail = v.parse_detail(res)
    detail["api"] = "mtop.taobao.idle.pc.detail + x5sec"
    print(json.dumps(detail, ensure_ascii=False, indent=2))
    print("\n✅ 直连详情验证通过（x5sec 通道）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
