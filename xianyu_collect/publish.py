"""直连发布通道（移植自上游 xianyu_direct_publisher，生产已验证，同步化改造）。

链路：采集详情 → 图片转存闲鱼图片空间 → 构造 inputJson 载荷 →
      mtop.idle.pc.backend.idleitem.publish（鱼小铺卖家工作台）。

实测铁律：
1. 发布接口 origin/referer 必须是 seller.goofish.com（www 会判会话过期）；
2. 图片下载必须带浏览器头且 Accept 不声明 webp（图床按 Accept 协商格式，会拿到
   与后缀不符的字节）；
3. 闲鱼同一商品只允许一组规格带规格图；
4. itemAddrDTO 需要高德 POI（divisionId/gps/poiId/poiName）。
"""
from __future__ import annotations

import json
import re
import struct
import time
import urllib.parse
import urllib.request
import uuid
from typing import Any

from .mtop import MtopClient, MtopError

PUBLISH_API = "mtop.idle.pc.backend.idleitem.publish"
SELLER_ORIGIN = "https://seller.goofish.com"
SELLER_REFERER = "https://seller.goofish.com/?site=COMMONPRO"
UPLOAD_URL = "https://stream-upload.goofish.com/api/upload.api?floderId=0&appkey=fleamarket&_input_charset=utf-8"
MAX_IMAGE_COUNT = 9  # 平台单商品主图上限

AMAP_INPUTTIPS_URL = "https://restapi.amap.com/v3/assistant/inputtips"
AMAP_WEB_KEY = "c9b68d4ce9a2a97f22a4a439404488ca"  # 卖家工作台网页 SDK key（抓包）

IMAGE_DOWNLOAD_HEADERS = {
    "Accept": "image/jpeg,image/png,image/*;q=0.8,*/*;q=0.5",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.goofish.com/",
}
RETRYABLE_STATUS = {420, 429, 500, 502, 503, 504}


class PublishError(RuntimeError):
    pass


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _price_in_cent(value: Any, field: str) -> str:
    try:
        cents = round(float(value) * 100)
    except (TypeError, ValueError) as exc:
        raise PublishError(f"{field}格式不正确") from exc
    if cents <= 0:
        raise PublishError(f"{field}必须大于0")
    return str(cents)


# ---------------------------------------------------------------- 媒体

def sniff_dimensions(content: bytes) -> tuple:
    """JPEG/PNG/GIF 尺寸嗅探（平台载荷需要宽高，纯标准库实现）。"""
    if content[:8] == b"\x89PNG\r\n\x1a\n" and len(content) > 24:
        w, h = struct.unpack(">II", content[16:24])
        return w, h
    if content[:3] == b"GIF":
        w, h = struct.unpack("<HH", content[6:10])
        return w, h
    if content[:2] == b"\xff\xd8":  # JPEG：扫 SOF 段
        i = 2
        while i + 9 < len(content):
            if content[i] != 0xFF:
                i += 1
                continue
            marker = content[i + 1]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", content[i + 5:i + 9])
                return w, h
            seg_len = struct.unpack(">H", content[i + 2:i + 4])[0]
            i += 2 + seg_len
    return 800, 800  # 无法解析时的兜底（服务端 pix 通常会覆盖）


def download_image(url: str, ua: str, retries=(1.0, 3.0)) -> tuple:
    """下载原图（图床对无浏览器头请求返回 420）。"""
    headers = dict(IMAGE_DOWNLOAD_HEADERS, **{"User-Agent": ua})
    last_status = 0
    for attempt in range(len(retries) + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status == 200:
                    content = resp.read()
                    if content:
                        ctype = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip()
                        return content, ctype
                last_status = resp.status
        except urllib.error.HTTPError as exc:
            last_status = exc.code
        if last_status not in RETRYABLE_STATUS or attempt >= len(retries):
            break
        time.sleep(retries[attempt])
    raise PublishError(f"图片下载失败 HTTP {last_status}: {url[:80]}")


def upload_image_content(content: bytes, cookie: str, ua: str, suffix: str = ".jpg") -> dict:
    """上传图片字节到闲鱼图片空间，返回 imageInfoDOList 元素。"""
    filename = f"publish_api_{uuid.uuid4().hex}{suffix}"
    boundary = f"----xycp{uuid.uuid4().hex}"
    parts = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {'image/png' if suffix == '.png' else 'image/jpeg'}\r\n\r\n".encode(),
        content,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    req = urllib.request.Request(
        UPLOAD_URL, data=body, method="POST",
        headers={
            "Accept": "*/*",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Cookie": cookie,
            "Origin": SELLER_ORIGIN,
            "Referer": SELLER_REFERER,
            "User-Agent": ua,
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        if "<!DOCTYPE" in raw[:200] or "<html" in raw[:200].lower():
            raise PublishError("图片上传失败：登录态已失效（返回登录页），请重新扫码登录")
        raise PublishError(f"图片上传返回异常: {raw[:120]}")
    uploaded = result.get("object") if isinstance(result, dict) else None
    if not isinstance(uploaded, dict) or not uploaded.get("url") or result.get("success") is not True:
        raise PublishError(f"图片上传失败: {json.dumps(result, ensure_ascii=False)[:150]}")
    w, h = sniff_dimensions(content)
    pix = str(uploaded.get("pix") or f"{w}x{h}")
    try:
        pw_, ph_ = (int(x) for x in pix.lower().split("x", 1))
    except (TypeError, ValueError):
        pw_, ph_ = w, h
    return {
        "extraInfo": {"isH": "false", "isT": "false", "raw": "false"},
        "isQrCode": False,
        "url": str(uploaded["url"]),
        "heightSize": ph_,
        "widthSize": pw_,
        "major": False,
        "type": 0,
        "status": "done",
    }


def transfer_image(source_url: str, cookie: str, ua: str) -> dict:
    """下载源图 → 上传闲鱼图片空间 → 返回载荷元素。"""
    content, ctype = download_image(source_url, ua)
    suffix = ".png" if "png" in ctype.lower() else ".jpg"
    item = upload_image_content(content, cookie, ua, suffix)
    return item


# ---------------------------------------------------------------- 地址

AMAP_PARAMS = {
    "s": "rsv3", "key": AMAP_WEB_KEY, "platform": "JS",
    "logversion": "2.0", "sdkversion": "2.0",
    "appname": SELLER_ORIGIN, "citylimit": "false", "datatype": "all",
}
AMAP_HEADERS = {
    "Origin": SELLER_ORIGIN, "Referer": f"{SELLER_ORIGIN}/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
}


def _amap_request(keywords: str, city: str = "全国") -> dict:
    params = dict(AMAP_PARAMS, keywords=keywords, city=city or "全国")
    req = urllib.request.Request(
        f"{AMAP_INPUTTIPS_URL}?{urllib.parse.urlencode(params)}", headers=AMAP_HEADERS,
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read().decode("utf-8", "replace"))
    if str(result.get("status")) != "1":
        raise PublishError(f"高德接口失败: {result.get('info')}")
    return result


def amap_tips(keywords: str, city: str = "全国") -> list:
    """地址关键词 → 候选 POI 列表（前端选择器用）。"""
    result = _amap_request(keywords, city)
    tips = []
    for t in result.get("tips") or []:
        if not isinstance(t, dict) or not _text(t.get("name")):
            continue
        tips.append({
            "id": _text(t.get("id")),
            "name": _text(t.get("name")),
            "district": _text(t.get("district")),
            "adcode": _text(t.get("adcode")),
            "location": _text(t.get("location")),
            "address": _text(t.get("address")),
            "text": " ".join(x for x in (_text(t.get("name")), _text(t.get("district")), _text(t.get("address"))) if x),
        })
    return [t for t in tips if t["id"] and t["adcode"] and t["location"]]


def tip_to_addr_dto(tip: dict) -> dict:
    """选中的候选 → itemAddrDTO（gps 为 纬度,经度）。"""
    lng, lat = tip["location"].split(",")[:2]
    return {
        "divisionId": tip["adcode"],
        "gps": f"{float(lat):.6f},{float(lng):.6f}",
        "poiId": tip["id"],
        "poiName": tip["name"],
    }


def resolve_address(address_text: str) -> dict:
    """文本地址 → 首个候选 → itemAddrDTO（无选择器场景的兜底）。"""
    tips = amap_tips(address_text)
    if not tips:
        raise PublishError(f"地址未解析到有效 POI: {address_text}")
    return tip_to_addr_dto(tips[0])


# ---------------------------------------------------------------- 载荷

def build_item_text(title: str, description: str) -> dict:
    return {"desc": description, "title": title, "titleDescSeparate": True}


def build_sku_payload(specifications: list, sku_rows: list) -> tuple:
    """规格定义 + SKU 行 → (itemProperties, itemSkuList, 规格图来源, is_multi)。

    specifications: [{"name": "颜色", "values": [{"name": "黑", "image": "http..."}, ...]}, ...]
    sku_rows:       [{"specs": {"颜色": "黑"}, "price": 15.5, "stock": 60}, ...]
    """
    if not specifications and not sku_rows:
        return [], [], [], False
    if not specifications:
        raise PublishError("有 SKU 行但缺规格定义")
    if not sku_rows:
        raise PublishError("请填写每个 SKU 的价格和库存")

    item_properties = []
    property_image_sources = []
    spec_names = []
    for spec in specifications:
        name = _text(spec.get("name"))
        values = [(_text(v.get("name")) if isinstance(v, dict) else _text(v)) for v in spec.get("values") or []]
        values = [v for v in values if v]
        if not name or not values:
            raise PublishError(f"规格定义不完整: {spec}")
        spec_names.append(name)
        has_image = False
        property_values = []
        for v in spec.get("values") or []:
            vname = _text(v.get("name")) if isinstance(v, dict) else _text(v)
            if not vname:
                continue
            property_values.append({"propertyValue": vname})
            image = _text(v.get("image")) if isinstance(v, dict) else ""
            if image:
                has_image = True
                property_image_sources.append({"property_name": name, "property_value": vname, "source": image})
        item_properties.append({
            "propertyName": name,
            "supportImage": has_image,
            "propertyValues": property_values,
        })
    # 闲鱼只允许一组规格带图
    image_groups = {s["property_name"] for s in property_image_sources}
    if len(image_groups) > 1:
        keep = sorted(image_groups)[0]
        for prop in item_properties:
            prop["supportImage"] = prop["propertyName"] == keep

    item_sku_list = []
    seen = set()
    for idx, row in enumerate(sku_rows, 1):
        specs = row.get("specs") or {}
        property_list = []
        combo = []
        for name in spec_names:
            vname = _text(specs.get(name))
            if not vname:
                raise PublishError(f"第 {idx} 个 SKU 缺少规格 {name}")
            combo.append(vname)
            property_list.append({"propertyText": name, "valueText": vname})
        key = tuple(combo)
        if key in seen:
            raise PublishError(f"第 {idx} 个 SKU 规格组合重复")
        seen.add(key)
        stock = int(row.get("stock") or 0)
        item_sku_list.append({
            "priceInCent": _price_in_cent(row.get("price"), f"第 {idx} 个 SKU 售价"),
            "quantity": max(0, min(stock, 999999)),
            "propertyList": property_list,
        })
    return item_properties, item_sku_list, property_image_sources, True


def build_category_label(item_data: dict) -> dict:
    cat_name = _text(item_data.get("platform_category_name"))
    channel_id = _text(item_data.get("platform_channel_category_id"))
    return {
        "channelCateName": cat_name,
        "valueId": None, "channelCateId": channel_id, "valueName": None,
        "tbCatId": _text(item_data.get("platform_tb_category_id")),
        "subPropertyId": None, "labelType": "common", "subValueId": None,
        "labelId": None, "propertyName": "分类", "isUserClick": "1", "isUserCancel": None,
        "from": "newPublishChoice", "propertyId": "-10000", "labelFrom": "newPublish",
        "text": cat_name,
        "properties": f"-10000##分类:{channel_id}##{cat_name}",
    }


def build_payload(item_data: dict, account_id: str, image_items: list,
                  property_image_list: list) -> dict:
    """纯载荷构造（媒体已上传完毕的最终形态）。"""
    title = _text(item_data.get("title"))
    description = _text(item_data.get("description"))
    if not title or not description:
        raise PublishError("标题和描述不能为空")
    if not image_items:
        raise PublishError("至少一张商品图片")

    item_properties, item_sku_list, _, is_multi = build_sku_payload(
        item_data.get("specifications") or [], item_data.get("sku_rows") or [])

    shipping = _text(item_data.get("shipping_method")) or "free"
    if shipping == "free":
        post_fee = {"canFreeShipping": True, "supportFreight": True, "onlyTakeSelf": False}
    elif shipping == "fixed":
        postage = float(item_data.get("postage") or 0)
        post_fee = {"canFreeShipping": False, "supportFreight": True, "onlyTakeSelf": False,
                    "postPriceInCent": str(round(postage * 100)), "templateId": "0"}
    else:
        raise PublishError(f"暂不支持的发货方式: {shipping}")

    payload = {
        "freebies": False,
        "itemTypeStr": "b",
        "quantity": "1" if is_multi else str(max(1, min(int(item_data.get("quantity") or 1), 999999))),
        "simpleItem": "true",
        "imageInfoDOList": image_items,
        "itemTextDTO": build_item_text(title, description),
        "itemLabelExtList": [build_category_label(item_data)],
        "itemPostFeeDTO": post_fee,
        "itemAddrDTO": item_data.get("address_dto") or {},
        "itemSkuList": item_sku_list,
        "defaultPrice": False,
        "itemPriceDTO": (
            {} if is_multi else {
                "origPriceInCent": _price_in_cent(
                    item_data.get("original_price") or item_data.get("price"), "原价"),
                "priceInCent": _price_in_cent(item_data.get("price"), "售价"),
            }
        ),
        "uniqueCode": f"{int(time.time() * 1000)}{account_id[-4:]}",
        "sourceId": "pcBackendPublish",
        "bizcode": "pcMainPublish",
        "publishScene": "pcBackendPublish",
        "userRightsProtocols": [
            {"enable": False, "serviceCode": "FAST_DELIVERY_48_HOUR"},
            {"enable": False, "serviceCode": "FAST_DELIVERY_24_HOUR"},
            {"enable": False, "serviceCode": "NONCONFORMITY_FREE_REFUND"},
        ],
    }
    if is_multi:
        payload["itemProperties"] = item_properties
    if property_image_list:
        payload["propertyImageList"] = property_image_list
    payload["itemCatDTO"] = {
        "catId": _text(item_data.get("platform_category_id")),
        "catName": _text(item_data.get("platform_category_name")),
        "channelCatId": _text(item_data.get("platform_channel_category_id")),
        "leafId": _text(item_data.get("platform_leaf_id")),
        "tbCatId": _text(item_data.get("platform_tb_category_id")),
    }
    return payload


# ---------------------------------------------------------------- 采集详情 → 发布表单

def detail_from_store_row(row: dict) -> dict:
    """把 SQLite item_details 行转换为 clone_form_from_detail 需要的详情结构。"""
    return {
        "item_id": row.get("item_id") or "",
        "title": row.get("title") or "",
        "price": row.get("price"),
        "desc": row.get("descr") or "",
        "images": row.get("image_urls") or [],
        "skus": row.get("skus") or [],
        "quantity": row.get("quantity"),
        "raw_data": json.loads(row.get("raw_json") or "{}"),
    }


def clone_form_from_detail(detail: dict, markup_pct: float = 0.0) -> dict:
    """把采集到的商品详情映射为发布表单（含加价率），媒体上传前一步。"""
    raw = detail.get("raw_data") or {}
    item = raw.get("itemDO") or {}
    cat = item.get("itemCatDTO") or {}
    skus = detail.get("skus") or []

    def adjusted(price):
        try:
            return round(float(price) * (1 + markup_pct / 100.0), 2)
        except (TypeError, ValueError):
            return 0

    form = {
        "title": detail.get("title") or "",
        "description": detail.get("desc") or "",
        "images": (detail.get("images") or [])[:MAX_IMAGE_COUNT],
        "platform_category_id": str(cat.get("catId") or item.get("categoryId") or ""),
        "platform_category_name": "",
        "platform_channel_category_id": str(cat.get("channelCatId") or ""),
        "platform_leaf_id": str(cat.get("leafId") or ""),
        "platform_tb_category_id": str(cat.get("tbCatId") or ""),
        "shipping_method": "free",
    }
    if skus:
        # 规格组：按属性名聚合（实测通常一组"颜色"），规格图取首个 SKU 图
        groups: dict = {}
        for sku in skus:
            for p in sku.get("props") or []:
                g = groups.setdefault(p["name"], {})
                if p["value"] not in g:
                    g[p["value"]] = sku.get("image") or ""
        form["specifications"] = [
            {"name": name, "values": [{"name": v, "image": img} for v, img in vals.items()]}
            for name, vals in groups.items()
        ]
        form["sku_rows"] = [
            {
                "specs": {p["name"]: p["value"] for p in sku.get("props") or []},
                "price": adjusted(sku.get("price")),
                "stock": min(int(sku.get("quantity") or 0), 99),
            }
            for sku in skus
        ]
    else:
        form["price"] = adjusted(detail.get("price"))
        form["original_price"] = adjusted(raw.get("itemDO", {}).get("originalPrice") or detail.get("price"))
        form["quantity"] = max(1, min(int(detail.get("quantity") or 1), 999))
    return form


# ---------------------------------------------------------------- 发布调用

def _find_item_reference(value: Any) -> tuple:
    """递归提取发布响应中的商品 ID/链接（移植上游）。"""
    item_id = None
    item_url = None
    if isinstance(value, dict):
        for key, current in value.items():
            k = key.lower()
            if k in {"itemid", "item_id", "idleitemid"} and current is not None:
                item_id = _text(current) or item_id
            elif k in {"itemurl", "item_url", "url"} and isinstance(current, str):
                m = re.search(r"[?&]id=(\d+)", current) or re.search(r"/item/(\d+)", current)
                item_id = item_id or (m.group(1) if m else None)
                if "goofish.com" in current and (m or "/item" in current):
                    item_url = current
            nid, nurl = _find_item_reference(current)
            item_id = item_id or nid
            item_url = item_url or nurl
    elif isinstance(value, list):
        for current in value:
            nid, nurl = _find_item_reference(current)
            item_id = item_id or nid
            item_url = item_url or nurl
    return item_id, item_url


def publish_item(client: MtopClient, payload: dict) -> dict:
    """调用鱼小铺发布接口，返回 {item_id, item_url}。"""
    try:
        res = client.call(
            PUBLISH_API,
            {"inputJson": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
            referer=SELLER_REFERER, origin=SELLER_ORIGIN,
        )
    except MtopError as exc:
        return {"success": False, "message": str(exc), "kind": exc.kind}
    item_id, item_url = _find_item_reference(res)
    if item_id:
        item_url = f"https://www.goofish.com/item?id={item_id}"
    return {"success": True, "item_id": item_id, "item_url": item_url}


def clone_item(client: MtopClient, detail: dict, account_id: str,
               markup_pct: float = 0.0, address_text: str = "", address_dto: dict | None = None,
               log=print) -> dict:
    """完整克隆链路：表单映射 → 图片转存 → 载荷 → 发布。

    address_dto: 地址选择器选中的候选（含 location/adcode/id）或已转换的 itemAddrDTO；
                 提供时跳过文本解析。
    """
    form = clone_form_from_detail(detail, markup_pct)
    if address_dto:
        if not address_dto.get("gps") and address_dto.get("location"):
            address_dto = tip_to_addr_dto(address_dto)
        form["address_dto"] = address_dto
        log(f"地址: {address_dto.get('poiName')}")
    elif address_text:
        form["address_dto"] = resolve_address(address_text)
        log(f"地址已解析: {form['address_dto']['poiName']}")

    cookie = client.cookie_header()
    image_items = []
    for i, src in enumerate(form["images"], 1):
        item = transfer_image(src, cookie, client.ua)
        item["major"] = i == 1
        image_items.append(item)
        log(f"图片转存 {i}/{len(form['images'])}")

    props, _, sources, is_multi = build_sku_payload(form.get("specifications") or [], form.get("sku_rows") or [])
    property_image_list = []
    if is_multi:
        img_by_pair = {}
        for source in sources:
            img = transfer_image(source["source"], cookie, client.ua)
            property_image_list.append({
                "property": {"propertyText": source["property_name"], "valueText": source["property_value"]},
                "url": img["url"],
            })
            img_by_pair[(source["property_name"], source["property_value"])] = img["url"]
        for prop in props:
            for pv in prop.get("propertyValues") or []:
                url = img_by_pair.get((prop["propertyName"], pv.get("propertyValue")))
                if url:
                    pv["propertyValueImg"] = {"url": url}

    payload = build_payload(form, account_id, image_items, property_image_list)
    log("载荷构造完成，提交发布…")
    return publish_item(client, payload)
