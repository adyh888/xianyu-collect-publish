#!/usr/bin/env python3
"""
闲鱼采集链路验证脚本（直连 mtop API · 零第三方依赖）

目的：验证「搜索采集 → 详情采集」能否用纯 HTTP 直连实现（服务器版的核心依赖），
     不依赖 Playwright。请求构造复刻自上游 common/services/xianyu_mtop.py::mtop_call，
     响应结构与搜索解析参考 xianyu-butler-desktop/backend/utils/item_search.py。

用法：
    python3 verify_collect.py search "iPhone 13 128g"          # 关键词搜索
    python3 verify_collect.py detail <商品ID>                   # 单商品详情
    python3 verify_collect.py chain "iPhone 13 128g"           # 搜索→取第一条→详情（完整链路）

可选：
    --cookie-file cookie.txt   登录 cookie（浏览器 F12 复制，或闲鱼管家后台导出）
                               不提供则匿名请求（大概率被要求登录，用于观察错误形态）
    --json out.json            结果另存 JSON
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path

MTOP_BASE = "https://h5api.m.goofish.com/h5/{api}/{version}/"
APP_KEY = "34839810"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
ORIGIN = "https://www.goofish.com"
REFERER = "https://www.goofish.com/"

TOKEN_EXPIRED_MARKERS = ("FAIL_SYS_TOKEN_EXOIRED", "FAIL_SYS_TOKEN_EXPIRED", "FAIL_SYS_TOKEN_EMPTY")
RISK_MARKERS = ("FAIL_SYS_USER_VALIDATE", "RGV587", "SESSION_EXPIRED", "FAIL_SYS_ILLEGAL_ACCESS", "punish", "哎哟喂", "挤爆")


def trans_cookies(cookie_str: str) -> dict:
    cookies = {}
    for pair in cookie_str.replace("\n", "").replace("\r", "").split(";"):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def generate_sign(t: str, token: str, data_val: str) -> str:
    msg = f"{token}&{t}&{APP_KEY}&{data_val}"
    return hashlib.md5(msg.encode("utf-8")).hexdigest()


class MtopError(Exception):
    def __init__(self, api: str, ret: list, res: dict | None = None):
        self.api = api
        self.ret = ret
        self.res = res
        super().__init__(f"{api}: {'; '.join(ret)}")


class MtopClient:
    """同步版 mtop 客户端：首调拿令牌 → 重签重试 → 合并 Set-Cookie。"""

    def __init__(self, cookie_str: str = "", ua: str = UA):
        self.cookies: dict = trans_cookies(cookie_str)
        self.ua = ua

    def _cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def _merge_set_cookies(self, resp) -> None:
        for header in resp.headers.get_all("Set-Cookie") or []:
            simple = SimpleCookie()
            try:
                simple.load(header.split(";", 1)[0])
            except Exception:
                continue
            for key, morsel in simple.items():
                if key in ("_m_h5_tk", "_m_h5_tk_enc", "cookie2", "sgcookie", "_tb_token_", "isg", "tfstk", "xlly_s"):
                    self.cookies[key] = morsel.value

    def call(self, api: str, payload: dict, version: str = "1.0", attempts: int = 3,
             referer: str = REFERER, origin: str = ORIGIN) -> dict:
        url = MTOP_BASE.format(api=api, version=version)
        last_ret = ["<no-response>"]
        last_res: dict | None = None
        for attempt in range(1, attempts + 1):
            token = self.cookies.get("_m_h5_tk", "").split("_")[0] if self.cookies.get("_m_h5_tk") else ""
            t = str(int(time.time() * 1000))
            data_val = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            params = {
                "jsv": "2.7.2", "appKey": APP_KEY, "t": t, "sign": generate_sign(t, token, data_val),
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
                    "Origin": origin, "Referer": referer, "User-Agent": self.ua,
                    "Cookie": self._cookie_header(),
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    self._merge_set_cookies(resp)
                    res = json.loads(resp.read().decode("utf-8", "replace"))
            except Exception as exc:
                print(f"    [attempt {attempt}] 请求异常: {exc}")
                last_ret = [f"请求异常: {exc}"]
                time.sleep(0.5)
                continue

            ret = [str(x) for x in (res.get("ret") or [])]
            last_ret = ret
            last_res = res
            print(f"    [attempt {attempt}] ret = {ret}")
            if any("SUCCESS" in x for x in ret):
                return res
            if any(m in x for x in ret for m in TOKEN_EXPIRED_MARKERS):
                continue  # 已合并新令牌，重签重试
            if any(m in x for x in ret for m in RISK_MARKERS):
                break  # 风控/需登录，重试无意义
            break
        raise MtopError(api, last_ret, last_res)


# ---------------------------------------------------------------- 采集解析

def parse_search_items(res: dict) -> list[dict]:
    items = []
    for row in (res.get("data") or {}).get("resultList") or []:
        node = (row.get("data") or {}).get("item") or {}
        main = node.get("main") or {}
        ex = main.get("exContent") or {}
        args = ((main.get("clickParam") or {}).get("args")) or {}
        price_parts = ex.get("price") or []
        price = "".join(p.get("text", "") for p in price_parts if isinstance(p, dict)) if isinstance(price_parts, list) else str(price_parts)
        items.append({
            "item_id": args.get("item_id") or "",
            "title": ex.get("title") or "",
            "price": price.replace("当前价", "").strip(),
            "area": ex.get("area") or "",
            "seller": ex.get("userNickName") or "",
            "picUrl": ex.get("picUrl") or "",
            "targetUrl": (main.get("targetUrl") or "").replace("fleamarket://", "https://www.goofish.com/"),
        })
    return [i for i in items if i["item_id"]]


def deep_find(obj, keys, default=None):
    """在嵌套结构里递归找第一个出现的 key（详情接口返回结构多变，宽泛解析）。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and v not in (None, "", [], {}):
                return v
        for v in obj.values():
            found = deep_find(v, keys, default)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = deep_find(v, keys, default)
            if found is not None:
                return found
    return default


def parse_detail(res: dict) -> dict:
    data = res.get("data") or {}
    item = data.get("itemDO") or data.get("item") or data
    images = []
    for node in item.get("imageInfos") or []:
        if not isinstance(node, dict):
            continue
        url = node.get("url") or ""
        if not url and node.get("livePhotoUrl"):
            url = node["livePhotoUrl"]
        if url:
            images.append(url if str(url).startswith("http") else f"https:{url}")
    skus = deep_find(item, {"skuList", "skuVOList", "multiSkuList"}) or []
    return {
        "title": item.get("title") or deep_find(data, {"title"}) or "",
        "price": deep_find(item, {"soldPrice", "price"}) or deep_find(data, {"soldPrice", "price"}) or "",
        "desc": str(item.get("desc") or deep_find(data, {"desc", "description"}) or "")[:200],
        "image_count": len(images),
        "first_image": images[0] if images else "",
        "sku_count": len(skus) if isinstance(skus, list) else 0,
        "top_keys": list(item.keys())[:12],
    }


DETAIL_APIS = (
    ("mtop.taobao.idle.pc.detail", "1.0", lambda iid: {"itemId": iid}),
    ("mtop.taobao.idle.pc.detail", "2.0", lambda iid: {"itemId": iid}),
    ("mtop.taobao.detail.getdetail", "6.0", lambda iid: {"itemNumId": iid}),
)


def search_keyword(client: MtopClient, keyword: str, page: int = 1) -> dict:
    res = client.call(
        "mtop.taobao.idlemtopsearch.pc.search",
        {
            "keyword": keyword, "fromFilter": False, "rowsPerPage": 30,
            "bizFrom": "main_search", "pageNumber": page, "sortType": "type",
            "extraFilterValue": "{}",
        },
    )
    items = parse_search_items(res)
    return {"api": "mtop.taobao.idlemtopsearch.pc.search", "count": len(items), "items": items}


def fetch_detail(client: MtopClient, item_id: str) -> dict:
    item_referer = f"https://www.goofish.com/item?id={item_id}"
    for api, version, build in DETAIL_APIS:
        try:
            res = client.call(api, build(item_id), version, referer=item_referer)
            detail = parse_detail(res)
            detail["api"] = api
            return detail
        except MtopError as exc:
            print(f"    详情接口 {api} 失败: {exc.ret}")
    raise MtopError("detail(all-candidates)", ["全部候选详情接口均失败"])


def explain_error(exc: MtopError) -> str:
    joined = "; ".join(exc.ret)
    if any(m in joined for m in ("FAIL_SYS_USER_VALIDATE", "RGV587", "punish", "哎哟喂")):
        return "触发登录/风控校验：需要提供已登录账号的 cookie（--cookie-file），匿名请求搜不到数据属预期。"
    if "SESSION_EXPIRED" in joined:
        return "cookie 已过期：重新扫码登录获取新 cookie。"
    if any(m in joined for m in TOKEN_EXPIRED_MARKERS):
        return "令牌未获取成功：脚本会自动用 Set-Cookie 刷新重试，若连续出现请检查网络/UA。"
    return "业务错误：看 ret 详情。"


def main() -> int:
    parser = argparse.ArgumentParser(description="闲鱼采集链路验证（直连 mtop）")
    parser.add_argument("command", choices=["search", "detail", "chain"])
    parser.add_argument("target", help="关键词（search/chain）或商品ID（detail）")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--cookie-file", default="")
    parser.add_argument("--json", default="", help="结果另存 JSON 的路径")
    args = parser.parse_args()

    cookie_str = ""
    if args.cookie_file:
        cookie_str = Path(args.cookie_file).read_text(encoding="utf-8").strip()
        print(f"[cookie] 已加载 {args.cookie_file}（{len(cookie_str)} 字符）")
    else:
        print("[cookie] 未提供，匿名请求（首次调用会自动获取 _m_h5_tk 令牌）")

    client = MtopClient(cookie_str)
    result = {"ts": int(time.time()), "mode": "anonymous" if not cookie_str else "logged-in"}

    try:
        if args.command == "search":
            print(f"[search] 关键词: {args.target} 第{args.page}页")
            result["search"] = search_keyword(client, args.target, args.page)
            for it in result["search"]["items"][:5]:
                print(f"  - {it['item_id']}  {it['price']:>10}  {it['title'][:40]}")
        elif args.command == "detail":
            print(f"[detail] 商品ID: {args.target}")
            result["detail"] = fetch_detail(client, args.target)
            print(json.dumps(result["detail"], ensure_ascii=False, indent=2))
        else:
            print(f"[chain] 完整链路: 搜索「{args.target}」→ 详情")
            result["search"] = search_keyword(client, args.target, 1)
            print(f"  搜索命中 {result['search']['count']} 条")
            if not result["search"]["items"]:
                raise MtopError("chain", ["搜索无结果，无法继续详情采集"])
            first = result["search"]["items"][0]
            print(f"  取第一条: {first['item_id']} {first['title'][:36]}")
            result["detail"] = fetch_detail(client, first["item_id"])
            print(json.dumps(result["detail"], ensure_ascii=False, indent=2))
        print("\n✅ 验证通过：直连 mtop API 采集链路可用")
        code = 0
    except MtopError as exc:
        print(f"\n❌ 验证中断：{exc}")
        print(f"   判读：{explain_error(exc)}")
        result["error_ret"] = exc.ret
        code = 2

    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[saved] {args.json}")
    return code


if __name__ == "__main__":
    sys.exit(main())
