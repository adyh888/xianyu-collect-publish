"""采集服务：搜索 / 详情 / 链路编排。解析逻辑与 POC 验证版一致。"""
from __future__ import annotations

import re
import urllib.parse
from datetime import datetime

from .mtop import MtopClient, MtopError
from .risk import RiskGuard
from .store import Store

SEARCH_API = "mtop.taobao.idlemtopsearch.pc.search"
DETAIL_API = "mtop.taobao.idle.pc.detail"

SEARCH_PAYLOAD = {
    "keyword": "", "fromFilter": False, "rowsPerPage": 30,
    "bizFrom": "main_search", "pageNumber": 1, "sortType": "type",
    "extraFilterValue": "{}",
}

_WANT_RE = re.compile(r"(\d+(?:\.\d+)?(?:万)?)\s*人想要")


def _safe(d, *keys, default=""):
    for k in keys:
        try:
            d = d[k]
        except (KeyError, TypeError, IndexError):
            return default
    return d


def _extract_want_count(tags_content: str) -> int:
    m = _WANT_RE.search(tags_content or "")
    if not m:
        return 0
    num = m.group(1)
    try:
        return int(float(num.replace("万", "")) * (10000 if "万" in num else 1))
    except ValueError:
        return 0


def parse_search_items(res: dict) -> list:
    items = []
    for row in (res.get("data") or {}).get("resultList") or []:
        node = (row.get("data") or {}).get("item") or {}
        main = node.get("main") or {}
        ex = main.get("exContent") or {}
        args = ((main.get("clickParam") or {}).get("args")) or {}
        price_parts = ex.get("price") or []
        price = ("".join(p.get("text", "") for p in price_parts if isinstance(p, dict))
                 if isinstance(price_parts, list) else str(price_parts))
        want = 0
        for tag_data in (ex.get("fishTags") or {}).values():
            if not isinstance(tag_data, dict):
                continue
            for tag in tag_data.get("tagList") or []:
                content = (tag.get("data") or {}).get("content", "")
                if "人想要" in content:
                    want = _extract_want_count(content)
                    break
            if want:
                break
        publish_time = ""
        ts = str(args.get("publishTime") or "")
        if ts.isdigit():
            try:
                publish_time = datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d %H:%M")
            except (ValueError, OSError):
                pass
        item_id = str(args.get("item_id") or "")
        if not item_id:
            continue
        pic = ex.get("picUrl") or ""
        items.append({
            "item_id": item_id,
            "title": ex.get("title") or "",
            "price": price.replace("当前价", "").replace("¥", "").strip(),
            "pic_url": pic if pic.startswith("http") else (f"https:{pic}" if pic else ""),
            "area": ex.get("area") or "",
            "seller": ex.get("userNickName") or "",
            "want_count": want,
            "publish_time": publish_time,
            "item_url": (main.get("targetUrl") or "").replace("fleamarket://", "https://www.goofish.com/"),
            "raw_data": row,
        })
    return items


def parse_detail(res: dict) -> dict:
    data = res.get("data") or {}
    item = data.get("itemDO") or data.get("item") or data
    images = []
    for node in item.get("imageInfos") or []:
        if not isinstance(node, dict):
            continue
        url = node.get("url") or node.get("livePhotoUrl") or ""
        if url:
            images.append(url if str(url).startswith("http") else f"https:{url}")
    skus = _deep_find(item, {"skuList", "skuVOList", "multiSkuList"}) or []
    return {
        "item_id": str(item.get("itemId") or ""),
        "title": item.get("title") or "",
        "price": str(item.get("soldPrice") or ""),
        "desc": str(item.get("desc") or ""),
        "images": images,
        "skus": skus if isinstance(skus, list) else [],
        "quantity": item.get("quantity"),
        "raw_data": data,
    }


def _deep_find(obj, keys):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and v not in (None, "", [], {}):
                return v
        for v in obj.values():
            found = _deep_find(v, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find(v, keys)
            if found is not None:
                return found
    return None


class Collector:
    """采集编排：频控/风控熔断由 MtopClient + RiskGuard 承担。"""

    def __init__(self, client: MtopClient, store: Store, guard: RiskGuard, log=print):
        self.client = client
        self.store = store
        self.guard = guard
        self.log = log

    def search(self, keyword: str, page: int = 1) -> int:
        self.guard.acquire()
        payload = dict(SEARCH_PAYLOAD, keyword=keyword, pageNumber=page)
        try:
            referer_kw = urllib.parse.quote(keyword)
            res = self.client.call(SEARCH_API, payload, referer=f"https://www.goofish.com/search?keyword={referer_kw}")
            items = parse_search_items(res)
            n = self.store.upsert_items(items, keyword=keyword)
            self.guard.reset()
            self.store.log("search", keyword, True, n)
            return n
        except MtopError as exc:
            if exc.kind == "risk":
                self.guard.trip(exc.punish_url[:120] or "; ".join(exc.ret))
            self.store.log("search", keyword, False, 0, str(exc))
            raise

    def detail(self, item_id: str) -> dict:
        self.guard.acquire()
        referer = f"https://www.goofish.com/item?id={item_id}"
        try:
            res = self.client.call(DETAIL_API, {"itemId": item_id}, referer=referer)
            detail = parse_detail(res)
            self.store.upsert_detail(item_id, detail)
            self.guard.reset()
            self.store.log("detail", item_id, True, 1)
            return detail
        except MtopError as exc:
            if exc.kind == "risk":
                self.guard.trip(exc.punish_url[:120] or "; ".join(exc.ret))
            self.store.log("detail", item_id, False, 0, str(exc))
            raise

    def chain(self, keyword: str, top: int = 1) -> list:
        """搜索 → 取前 top 个商品逐个采详情，返回详情列表。"""
        self.search(keyword)
        rows = self.store.list_items(limit=top, keyword=keyword)
        details = []
        for row in rows:
            try:
                details.append(self.detail(row["item_id"]))
            except MtopError as exc:
                self.log(f"详情失败 {row['item_id']}: {exc}")
        return details
