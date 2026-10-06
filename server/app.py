"""服务器版壳：FastAPI + Web 管理页。

核心业务全部复用 xianyu_collect 核心包，本层只做 HTTP 编排：
- 账号会话加载 / 采集后回写 cookie（令牌刷新落盘）
- 单飞锁：同一时刻只允许一个采集任务（尊重账号频控）
- 风控熔断状态透出给前端
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from xianyu_collect.account import AccountStore
from xianyu_collect.collection import Collector
from xianyu_collect.mtop import MtopClient, MtopError
from xianyu_collect.risk import RiskBlocked, RiskGuard
from xianyu_collect.store import Store

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

app = FastAPI(title="闲鱼采集台")

_accounts = AccountStore(DATA)
_store = Store(DATA / "collect.db")
_guards: dict = {}
_collect_lock = threading.Lock()  # 单飞：全局一次只跑一个采集任务


def _guard(name: str) -> RiskGuard:
    if name not in _guards:
        _guards[name] = RiskGuard(name)
    return _guards[name]


def _collect(account_name: str, keyword: str, pages: int, top: int) -> dict:
    """搜索 pages 页 + 前 top 条详情；结束后把刷新过的 cookie 回写会话。"""
    return _run_collect(account_name, lambda c: _do_search(c, keyword, pages, top))


def _do_search(collector: Collector, keyword: str, pages: int, top: int) -> dict:
    total = 0
    for page in range(1, max(1, pages) + 1):
        total += collector.search(keyword, page=page)
    details = 0
    for row in _store.list_items(limit=max(0, top), keyword=keyword):
        try:
            collector.detail(row["item_id"])
            details += 1
        except MtopError as exc:
            if exc.kind == "risk":
                break  # 熔断生效，停止本轮
    return {"items": total, "details": details}


def _collect_shop(account_name: str, user_id: str, pages: int) -> dict:
    return _run_collect(account_name, lambda c: {"items": c.seller_items(user_id, max_pages=max(1, pages)), "details": 0})


def _run_collect(account_name: str, job) -> dict:
    """采集通用壳：加载账号 → 执行 → cookie 回写 → 风控归一化返回。"""
    account = _accounts.load(account_name)
    if not account:
        raise HTTPException(404, f"账号 {account_name} 不存在")
    guard = _guard(account_name)
    guard.acquire()  # 冷却期内直接拒绝

    client = MtopClient(account.cookies, ua=account.ua)
    collector = Collector(client, _store, guard, log=lambda *_: None)
    started = time.time()
    try:
        result = job(collector)
        account.cookies = client.cookies
        _accounts.save(account)
        return {"ok": True, "seconds": round(time.time() - started, 1), **result}
    except MtopError as exc:
        account.cookies = client.cookies
        _accounts.save(account)
        return {"ok": False, "error": str(exc), "kind": exc.kind}
    except RiskBlocked as exc:
        return {"ok": False, "error": str(exc), "kind": "risk"}


class CollectReq(BaseModel):
    keyword: str
    pages: int = 1
    top: int = 0
    account: str = "default"


class ShopReq(BaseModel):
    user_id: str
    pages: int = 5
    account: str = "default"


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/status")
def status():
    with _store._lock:
        items = _store._conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        details = _store._conn.execute("SELECT COUNT(*) FROM item_details").fetchone()[0]
    return {
        "accounts": _accounts.names(),
        "counts": {"items": items, "details": details},
        "risk": {
            name: {"blocked": g.is_blocked(), "remaining": round(g.remaining)}
            for name, g in _guards.items()
        },
    }


@app.post("/api/collect")
def collect(req: CollectReq):
    if not req.keyword.strip():
        raise HTTPException(400, "关键词不能为空")
    return _guarded_collect(lambda: _collect(req.account, req.keyword.strip(), req.pages, req.top))


@app.post("/api/collect-shop")
def collect_shop(req: ShopReq):
    if not req.user_id.strip().isdigit():
        raise HTTPException(400, "卖家 userId 应为数字")
    return _guarded_collect(lambda: _collect_shop(req.account, req.user_id.strip(), req.pages))


def _guarded_collect(job):
    if not _collect_lock.acquire(blocking=False):
        raise HTTPException(429, "已有采集任务在跑，稍等片刻")
    try:
        return job()
    finally:
        _collect_lock.release()


@app.get("/api/items")
def items(limit: int = 30, keyword: str = ""):
    return _store.list_items(limit=min(limit, 200), keyword=keyword)


@app.get("/api/items/{item_id}")
def item_detail(item_id: str):
    with _store._lock:
        row = _store._conn.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(404, "未采集该商品")
    detail = _store.get_detail(item_id)
    return {"item": dict(row), "detail": detail}
